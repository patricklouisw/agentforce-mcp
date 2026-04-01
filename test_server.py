import os
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

# Set env vars before importing server to prevent RuntimeError on import
os.environ.setdefault("SF_MY_DOMAIN_URL", "https://test.my.salesforce.com")
os.environ.setdefault("SF_CONSUMER_KEY", "test-key")
os.environ.setdefault("SF_CONSUMER_SECRET", "test-secret")
os.environ.setdefault("SF_AGENT_ID", "0XxTEST000000001")

from server import create_app, get_agentforce_client  # noqa: E402


@pytest.fixture(scope="session")
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
class TestHealthCheck:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_no_auth_required(self, client):
        """Health check should work even when MCP_API_KEY is set."""
        with patch.dict(os.environ, {"MCP_API_KEY": "secret"}):
            resp = client.get("/health")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CORS / OPTIONS preflight
# ---------------------------------------------------------------------------
class TestCORS:
    def test_cors_preflight_returns_200(self, client):
        resp = client.options(
            "/mcp",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.status_code == 200

    def test_cors_headers_present(self, client):
        resp = client.options(
            "/mcp",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers


# ---------------------------------------------------------------------------
# Bearer auth middleware
# ---------------------------------------------------------------------------
class TestBearerAuth:
    def test_no_api_key_allows_all(self, client):
        """When MCP_API_KEY is not set, all requests pass through."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_API_KEY", None)
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_valid_bearer_token(self, client):
        with patch.dict(os.environ, {"MCP_API_KEY": "my-secret"}):
            resp = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer my-secret",
                    "Content-Type": "application/json",
                },
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
            )
            assert resp.status_code != 401

    def test_invalid_bearer_token(self, client):
        with patch.dict(os.environ, {"MCP_API_KEY": "my-secret"}):
            resp = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer wrong-token",
                    "Content-Type": "application/json",
                },
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
            )
            assert resp.status_code == 401
            assert resp.json() == {"error": "Unauthorized"}

    def test_missing_bearer_token(self, client):
        with patch.dict(os.environ, {"MCP_API_KEY": "my-secret"}):
            resp = client.post(
                "/mcp",
                headers={"Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
            )
            assert resp.status_code == 401

    def test_options_skips_auth(self, client):
        """CORS preflight should bypass auth even with MCP_API_KEY set."""
        with patch.dict(os.environ, {"MCP_API_KEY": "my-secret"}):
            resp = client.options(
                "/mcp",
                headers={
                    "Origin": "https://example.com",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Streamable HTTP endpoint (/mcp)
# ---------------------------------------------------------------------------
class TestMCPEndpoint:
    def test_mcp_post_accepts_jsonrpc(self, client):
        """POST /mcp should accept JSON-RPC requests (not return 404/405)."""
        resp = client.post(
            "/mcp",
            headers={"Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "id": 1,
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
        )
        assert resp.status_code != 404
        assert resp.status_code != 405

    def test_mcp_get_not_allowed(self, client):
        """GET /mcp is not a valid method for streamable HTTP."""
        resp = client.get("/mcp")
        assert resp.status_code in (405, 406)


# ---------------------------------------------------------------------------
# Agentforce client factory
# ---------------------------------------------------------------------------
class TestGetAgentforceClient:
    def test_missing_all_credentials(self):
        """Should raise RuntimeError when no env vars are set."""
        import server
        old_client = server._client
        server._client = None
        try:
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(RuntimeError, match="Agentforce not configured"):
                    get_agentforce_client()
        finally:
            server._client = old_client

    def test_missing_partial_credentials(self):
        """Should list specific missing env vars in error."""
        import server
        old_client = server._client
        server._client = None
        try:
            with patch.dict(
                os.environ,
                {"SF_MY_DOMAIN_URL": "https://test.sf.com", "SF_CONSUMER_KEY": "key"},
                clear=True,
            ):
                with pytest.raises(RuntimeError, match="SF_CONSUMER_SECRET"):
                    get_agentforce_client()
        finally:
            server._client = old_client

    def test_valid_config_creates_client(self):
        """Should create an AgentforceClient when all env vars are set."""
        import server
        from agentforce_client import AgentforceClient

        old_client = server._client
        server._client = None
        try:
            with patch.dict(
                os.environ,
                {
                    "SF_MY_DOMAIN_URL": "https://test.my.salesforce.com",
                    "SF_CONSUMER_KEY": "key",
                    "SF_CONSUMER_SECRET": "secret",
                    "SF_AGENT_ID": "agent-123",
                },
                clear=True,
            ):
                result = get_agentforce_client()
                assert isinstance(result, AgentforceClient)
        finally:
            server._client = old_client


# ---------------------------------------------------------------------------
# MCP tools (unit tests with mocked AgentforceClient)
# ---------------------------------------------------------------------------
class TestSendMessageTool:
    @patch("server.get_agentforce_client")
    def test_send_message_success(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.send_message.return_value = {
            "conversation_id": "conv-1",
            "response": "Hello! How can I help?",
        }
        mock_get_client.return_value = mock_client

        from server import send_message
        import asyncio

        result = asyncio.run(send_message("conv-1", "Hi there"))

        assert result["conversation_id"] == "conv-1"
        assert result["response"] == "Hello! How can I help?"
        mock_client.send_message.assert_called_once_with("conv-1", "Hi there")

    @patch("server.get_agentforce_client")
    def test_send_message_error(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.send_message.return_value = {
            "error": "Agentforce API error: 500 - Internal Server Error"
        }
        mock_get_client.return_value = mock_client

        from server import send_message
        import asyncio

        result = asyncio.run(send_message("conv-1", "Hi"))

        assert "error" in result

    @patch("server.get_agentforce_client")
    def test_send_message_passes_conversation_id(self, mock_get_client):
        """Different conversation_ids should be passed through to the client."""
        mock_client = AsyncMock()
        mock_client.send_message.return_value = {
            "conversation_id": "vapi-call-xyz",
            "response": "Response",
        }
        mock_get_client.return_value = mock_client

        from server import send_message
        import asyncio

        asyncio.run(send_message("vapi-call-xyz", "Test message"))

        mock_client.send_message.assert_called_once_with("vapi-call-xyz", "Test message")


class TestEndConversationTool:
    @patch("server.get_agentforce_client")
    def test_end_conversation_success(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.end_conversation.return_value = {
            "conversation_id": "conv-1",
            "status": "ended",
        }
        mock_get_client.return_value = mock_client

        from server import end_conversation
        import asyncio

        result = asyncio.run(end_conversation("conv-1"))

        assert result == {"conversation_id": "conv-1", "status": "ended"}
        mock_client.end_conversation.assert_called_once_with("conv-1")

    @patch("server.get_agentforce_client")
    def test_end_conversation_idempotent(self, mock_get_client):
        """Ending an unknown conversation should still return success."""
        mock_client = AsyncMock()
        mock_client.end_conversation.return_value = {
            "conversation_id": "unknown",
            "status": "ended",
        }
        mock_get_client.return_value = mock_client

        from server import end_conversation
        import asyncio

        result = asyncio.run(end_conversation("unknown"))

        assert result["status"] == "ended"
