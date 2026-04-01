import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator

from dotenv import load_dotenv

load_dotenv()

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from agentforce_client import AgentforceClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("agentforce-mcp")

mcp = FastMCP(
    "agentforce-mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ---------------------------------------------------------------------------
# Auth middleware – checks for a Bearer token matching the MCP_API_KEY env var.
# If MCP_API_KEY is not set, authentication is disabled (open access).
# ---------------------------------------------------------------------------
class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip auth for health check and CORS preflight requests
        if request.url.path == "/health" or request.method == "OPTIONS":
            return await call_next(request)
        api_key = os.environ.get("MCP_API_KEY")
        if api_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header != f"Bearer {api_key}":
                return JSONResponse(
                    {"error": "Unauthorized"}, status_code=401
                )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Agentforce client (singleton)
# ---------------------------------------------------------------------------
_client: AgentforceClient | None = None


def get_agentforce_client() -> AgentforceClient:
    """Create or return the singleton AgentforceClient from environment variables."""
    global _client
    if _client is not None:
        return _client

    required = ("SF_MY_DOMAIN_URL", "SF_CONSUMER_KEY", "SF_CONSUMER_SECRET", "SF_AGENT_ID")
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Agentforce not configured. Missing env vars: {', '.join(missing)}"
        )

    bypass_user = os.environ.get("SF_BYPASS_USER", "true").lower() == "true"

    _client = AgentforceClient(
        my_domain_url=os.environ["SF_MY_DOMAIN_URL"],
        consumer_key=os.environ["SF_CONSUMER_KEY"],
        consumer_secret=os.environ["SF_CONSUMER_SECRET"],
        agent_id=os.environ["SF_AGENT_ID"],
        bypass_user=bypass_user,
    )
    return _client


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def send_message(conversation_id: str, message: str) -> dict:
    """Send a message to the Salesforce Agentforce AI agent and get a response.

    On the first call for a given conversation_id, a new Agentforce session is
    created automatically. Subsequent calls with the same conversation_id reuse
    the existing session, maintaining conversation context.

    Args:
        conversation_id: A unique identifier for this conversation (e.g. VAPI call ID).
            Messages with the same conversation_id share an Agentforce session.
        message: The question or message to send to the Agentforce agent.
    """
    client = get_agentforce_client()
    return await client.send_message(conversation_id, message)


@mcp.tool()
async def end_conversation(conversation_id: str) -> dict:
    """End a conversation and clean up its Agentforce session.

    Call this when the conversation is complete (e.g. when a VAPI call ends)
    to release resources. Idempotent: safe to call even if the conversation
    has already ended or was never started.

    Args:
        conversation_id: The conversation identifier to end.
    """
    client = get_agentforce_client()
    return await client.end_conversation(conversation_id)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app() -> Starlette:
    streamable_http_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield
        # Cleanup on shutdown
        if _client is not None:
            await _client.close()

    return Starlette(
        routes=[
            Route("/health", health_check),
            Mount("/", app=streamable_http_app),
        ],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            ),
            Middleware(TrustedHostMiddleware, allowed_hosts=["*"]),
            Middleware(BearerAuthMiddleware),
        ],
        lifespan=lifespan,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "sse").lower()
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    if transport == "stdio":
        logger.info("Starting Agentforce MCP server (stdio)...")
        mcp.run(transport="stdio")
    else:
        logger.info("Starting Agentforce MCP server (%s) on %s:%s...", transport, host, port)

        import uvicorn

        uvicorn.run(create_app(), host=host, port=port)
