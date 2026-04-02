import contextlib
import contextvars
import logging
import os
import sys
import uuid
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

# ContextVar to pass VAPI's conversation identifier from HTTP middleware to MCP tools.
# VAPI sends different headers depending on interaction type:
#   - X-Call-Id: voice calls
#   - X-Chat-Id: chat interactions
#   - X-Session-Id: chat sessions (groups multiple chats)
_vapi_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vapi_id", default=None
)

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


class VapiIdMiddleware(BaseHTTPMiddleware):
    """Extract VAPI's conversation identifier from request headers.

    VAPI sends identifying headers with each tool request:
      - X-Call-Id: voice call identifier
      - X-Chat-Id: chat interaction identifier
      - X-Session-Id: chat session identifier (groups multiple chats)

    Priority: X-Call-Id > X-Chat-Id > X-Session-Id
    The resolved ID is stored in a context variable for tool functions to use.
    """

    async def dispatch(self, request: Request, call_next):
        vapi_id = (
            request.headers.get("X-Call-Id")
            or request.headers.get("X-Chat-Id")
            or request.headers.get("X-Session-Id")
        )
        token = _vapi_id_var.set(vapi_id)
        try:
            return await call_next(request)
        finally:
            _vapi_id_var.reset(token)


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
# Helpers
# ---------------------------------------------------------------------------
def _resolve_conversation_id(conversation_id: str | None = None) -> str:
    """Resolve the conversation ID from parameter, VAPI header, or generate one.

    Priority:
    1. Explicit parameter (if provided and non-empty)
    2. VAPI header: X-Call-Id / X-Chat-Id / X-Session-Id (set by VapiIdMiddleware)
    3. Auto-generated UUID as fallback
    """
    if conversation_id:
        return conversation_id
    vapi_id = _vapi_id_var.get()
    if vapi_id:
        return vapi_id
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def send_message(message: str, conversation_id: str | None = None) -> dict:
    """Send a message to the Salesforce Agentforce AI agent and get a response.

    On the first call for a conversation, a new Agentforce session is created
    automatically. Subsequent calls reuse the existing session, maintaining
    conversation context.

    When called from VAPI, the conversation is automatically tracked using
    VAPI's call ID (no need to pass conversation_id). For non-VAPI callers,
    pass a conversation_id to maintain multi-turn context.

    Args:
        message: The question or message to send to the Agentforce agent.
        conversation_id: Optional. A unique identifier for this conversation.
            Automatically provided by VAPI via X-Call-Id, X-Chat-Id, or X-Session-Id header.
    """
    resolved_id = _resolve_conversation_id(conversation_id)
    client = get_agentforce_client()
    return await client.send_message(resolved_id, message)


@mcp.tool()
async def end_conversation(conversation_id: str | None = None) -> dict:
    """End a conversation and clean up its Agentforce session.

    Call this when the conversation is complete to release resources.
    Idempotent: safe to call even if the conversation has already ended.

    When called from VAPI, the conversation is automatically identified using
    VAPI's call/chat ID. Stale sessions are also auto-cleaned after 30 min inactivity.

    Args:
        conversation_id: Optional. The conversation identifier to end.
            Automatically provided by VAPI via X-Call-Id, X-Chat-Id, or X-Session-Id header.
    """
    resolved_id = _resolve_conversation_id(conversation_id)
    client = get_agentforce_client()
    return await client.end_conversation(resolved_id)


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
            Middleware(VapiIdMiddleware),
            Middleware(BearerAuthMiddleware),
        ],
        lifespan=lifespan,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").lower()
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

    if transport == "stdio":
        logger.info("Starting Agentforce MCP server (stdio)...")
        mcp.run(transport="stdio")
    else:
        logger.info("Starting Agentforce MCP server (streamable-http) on %s:%s...", host, port)

        import uvicorn

        uvicorn.run(create_app(), host=host, port=port)
