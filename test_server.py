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
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_API_KEY", None)
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
# Resolve conversation ID helper
# ---------------------------------------------------------------------------
class TestResolveConversationId:
    def test_explicit_id_takes_priority(self):
        """Explicit conversation_id param should be used even if header is set."""
        from server import _resolve_conversation_id, _vapi_id_var

        token = _vapi_id_var.set("header-id")
        try:
            assert _resolve_conversation_id("explicit-id") == "explicit-id"
        finally:
            _vapi_id_var.reset(token)

    def test_falls_back_to_vapi_header(self):
        """When no explicit ID, should use VAPI header from context var."""
        from server import _resolve_conversation_id, _vapi_id_var

        token = _vapi_id_var.set("vapi-call-123")
        try:
            assert _resolve_conversation_id(None) == "vapi-call-123"
        finally:
            _vapi_id_var.reset(token)

    def test_generates_uuid_as_fallback(self):
        """When no explicit ID and no header, should generate a UUID."""
        from server import _resolve_conversation_id, _vapi_id_var

        token = _vapi_id_var.set(None)
        try:
            result = _resolve_conversation_id(None)
            import uuid
            uuid.UUID(result)
        finally:
            _vapi_id_var.reset(token)


# ---------------------------------------------------------------------------
# VapiIdMiddleware
# ---------------------------------------------------------------------------
class TestVapiIdMiddleware:
    def test_call_id_header(self, client):
        """X-Call-Id header should be captured."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_API_KEY", None)
            resp = client.get(
                "/health",
                headers={"X-Call-Id": "vapi-call-abc"},
            )
            assert resp.status_code == 200

    def test_chat_id_header(self, client):
        """X-Chat-Id header should be captured."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_API_KEY", None)
            resp = client.get(
                "/health",
                headers={"X-Chat-Id": "vapi-chat-abc"},
            )
            assert resp.status_code == 200

    def test_call_id_takes_priority_over_chat_id(self):
        """X-Call-Id should be preferred over X-Chat-Id and X-Session-Id."""
        from server import _resolve_conversation_id, _vapi_id_var

        # Simulate middleware resolving: X-Call-Id wins
        token = _vapi_id_var.set("call-id-wins")
        try:
            assert _resolve_conversation_id(None) == "call-id-wins"
        finally:
            _vapi_id_var.reset(token)


# ---------------------------------------------------------------------------
# MCP tools (unit tests with mocked AgentforceClient)
# ---------------------------------------------------------------------------
class TestSendMessageTool:
    @patch("server.get_agentforce_client")
    def test_send_message_with_explicit_id(self, mock_get_client):
        """Explicit conversation_id param should be used."""
        mock_client = AsyncMock()
        mock_client.send_message.return_value = {
            "conversation_id": "conv-1",
            "response": "Hello!",
        }
        mock_get_client.return_value = mock_client

        from server import send_message
        import asyncio

        result = asyncio.run(send_message("Hi there", conversation_id="conv-1"))

        assert result["conversation_id"] == "conv-1"
        assert result["response"] == "Hello!"
        mock_client.send_message.assert_called_once_with("conv-1", "Hi there")

    @patch("server.get_agentforce_client")
    def test_send_message_uses_vapi_header(self, mock_get_client):
        """When no explicit ID, should use VAPI header from context var."""
        from server import send_message, _vapi_id_var
        import asyncio

        mock_client = AsyncMock()
        mock_client.send_message.return_value = {
            "conversation_id": "vapi-call-xyz",
            "response": "Response",
        }
        mock_get_client.return_value = mock_client

        token = _vapi_id_var.set("vapi-call-xyz")
        try:
            asyncio.run(send_message("Test message"))
            mock_client.send_message.assert_called_once_with("vapi-call-xyz", "Test message")
        finally:
            _vapi_id_var.reset(token)

    @patch("server.get_agentforce_client")
    def test_send_message_error(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.send_message.return_value = {
            "error": "Agentforce API error: 500 - Internal Server Error"
        }
        mock_get_client.return_value = mock_client

        from server import send_message
        import asyncio

        result = asyncio.run(send_message("Hi", conversation_id="conv-1"))

        assert "error" in result


class TestEndConversationTool:
    @patch("server.get_agentforce_client")
    def test_end_conversation_with_explicit_id(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.end_conversation.return_value = {
            "conversation_id": "conv-1",
            "status": "ended",
        }
        mock_get_client.return_value = mock_client

        from server import end_conversation
        import asyncio

        result = asyncio.run(end_conversation(conversation_id="conv-1"))

        assert result == {"conversation_id": "conv-1", "status": "ended"}
        mock_client.end_conversation.assert_called_once_with("conv-1")

    @patch("server.get_agentforce_client")
    def test_end_conversation_uses_vapi_header(self, mock_get_client):
        """When no explicit ID, should use VAPI header from context var."""
        from server import end_conversation, _vapi_id_var
        import asyncio

        mock_client = AsyncMock()
        mock_client.end_conversation.return_value = {
            "conversation_id": "vapi-call-xyz",
            "status": "ended",
        }
        mock_get_client.return_value = mock_client

        token = _vapi_id_var.set("vapi-call-xyz")
        try:
            result = asyncio.run(end_conversation())
            assert result["status"] == "ended"
            mock_client.end_conversation.assert_called_once_with("vapi-call-xyz")
        finally:
            _vapi_id_var.reset(token)
