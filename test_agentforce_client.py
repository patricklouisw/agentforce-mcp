"""Tests for agentforce_client.py."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio

from agentforce_client import AgentforceClient, SessionState


@pytest_asyncio.fixture
async def client():
    """Create an AgentforceClient for testing."""
    c = AgentforceClient(
        my_domain_url="https://test.my.salesforce.com",
        consumer_key="test-key",
        consumer_secret="test-secret",
        agent_id="0XxTEST000000001",
    )
    yield c
    await c.close()


def _mock_token_response():
    """Build a mock OAuth token response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "mock-token-123",
        "token_type": "Bearer",
        "instance_url": "https://test.my.salesforce.com",
    }
    resp.raise_for_status = MagicMock()
    return resp


def _mock_session_response(session_id="sess-abc-123"):
    """Build a mock session creation response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "sessionId": session_id,
        "_links": {
            "messages": {
                "href": f"https://api.salesforce.com/einstein/ai-agent/v1/sessions/{session_id}/messages"
            },
            "messagesStream": {
                "href": f"https://api.salesforce.com/einstein/ai-agent/v1/sessions/{session_id}/messages/stream"
            },
            "end": {
                "href": f"https://api.salesforce.com/einstein/ai-agent/v1/sessions/{session_id}"
            },
        },
        "messages": [
            {
                "type": "Inform",
                "id": "greeting-id",
                "isContentSafe": True,
                "message": "Hi, I'm an AI assistant. How can I help?",
            }
        ],
    }
    resp.raise_for_status = MagicMock()
    return resp


def _mock_message_response(text="Here is your answer."):
    """Build a mock synchronous message response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "messages": [
            {
                "type": "Inform",
                "content": {"text": text, "citedReferences": []},
            }
        ],
    }
    resp.raise_for_status = MagicMock()
    return resp


def _mock_delete_response():
    """Build a mock session delete response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 204
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# TestAuthentication
# ---------------------------------------------------------------------------
class TestAuthentication:
    @pytest.mark.asyncio
    async def test_authenticate_success(self, client):
        client._http.post = AsyncMock(return_value=_mock_token_response())

        token, expiry = await client._authenticate()

        assert token == "mock-token-123"
        assert expiry > time.time()
        client._http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_authenticate_failure(self, client):
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 401
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized", request=MagicMock(), response=error_resp
        )
        client._http.post = AsyncMock(return_value=error_resp)

        with pytest.raises(httpx.HTTPStatusError):
            await client._authenticate()

    @pytest.mark.asyncio
    async def test_token_caching(self, client):
        client._http.post = AsyncMock(return_value=_mock_token_response())

        token1 = await client._ensure_token()
        token2 = await client._ensure_token()

        assert token1 == token2
        # Only one call - token was cached
        assert client._http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_token_refresh_on_expiry(self, client):
        client._http.post = AsyncMock(return_value=_mock_token_response())

        await client._ensure_token()
        # Simulate token expiry
        client._token_expiry = time.time() - 1

        await client._ensure_token()

        assert client._http.post.call_count == 2


# ---------------------------------------------------------------------------
# TestCreateSession
# ---------------------------------------------------------------------------
class TestCreateSession:
    @pytest.mark.asyncio
    async def test_create_session_success(self, client):
        client._http.post = AsyncMock(
            side_effect=[_mock_token_response(), _mock_session_response()]
        )

        session = await client._create_session()

        assert session.session_id == "sess-abc-123"
        assert session.sequence_id == 1
        assert "messages" in session.links
        assert "end" in session.links

    @pytest.mark.asyncio
    async def test_create_session_api_error(self, client):
        error_resp = MagicMock(spec=httpx.Response)
        error_resp.status_code = 400
        error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request", request=MagicMock(), response=error_resp
        )
        client._http.post = AsyncMock(
            side_effect=[_mock_token_response(), error_resp]
        )

        with pytest.raises(httpx.HTTPStatusError):
            await client._create_session()


# ---------------------------------------------------------------------------
# TestSendMessage
# ---------------------------------------------------------------------------
class TestSendMessage:
    @pytest.mark.asyncio
    async def test_first_message_creates_session(self, client):
        """First message for a conversation_id should auto-create a session."""
        client._http.post = AsyncMock(
            side_effect=[
                _mock_token_response(),   # auth for session creation
                _mock_session_response(),  # session creation
                _mock_message_response("Hello!"),  # message send
            ]
        )

        result = await client.send_message("conv-1", "Hi there")

        assert result["conversation_id"] == "conv-1"
        assert result["response"] == "Hello!"
        assert "conv-1" in client._conversations

    @pytest.mark.asyncio
    async def test_second_message_reuses_session(self, client):
        """Second message for same conversation_id should reuse existing session."""
        client._http.post = AsyncMock(
            side_effect=[
                _mock_token_response(),
                _mock_session_response(),
                _mock_message_response("First response"),
                _mock_message_response("Second response"),
            ]
        )

        await client.send_message("conv-1", "First question")
        result = await client.send_message("conv-1", "Follow-up question")

        assert result["response"] == "Second response"
        # Session should have sequence_id incremented to 3 (started at 1, sent 2 messages)
        assert client._conversations["conv-1"].sequence_id == 3

    @pytest.mark.asyncio
    async def test_sequence_id_increments(self, client):
        """Sequence ID should increment with each message."""
        client._http.post = AsyncMock(
            side_effect=[
                _mock_token_response(),
                _mock_session_response(),
                _mock_message_response("R1"),
                _mock_message_response("R2"),
                _mock_message_response("R3"),
            ]
        )

        await client.send_message("conv-1", "Msg 1")
        assert client._conversations["conv-1"].sequence_id == 2

        await client.send_message("conv-1", "Msg 2")
        assert client._conversations["conv-1"].sequence_id == 3

        await client.send_message("conv-1", "Msg 3")
        assert client._conversations["conv-1"].sequence_id == 4


# ---------------------------------------------------------------------------
# TestEndConversation
# ---------------------------------------------------------------------------
class TestEndConversation:
    @pytest.mark.asyncio
    async def test_end_conversation_success(self, client):
        """Ending a known conversation should delete the session and remove it."""
        # Pre-populate a conversation
        client._conversations["conv-1"] = SessionState(
            session_id="sess-123",
            sequence_id=3,
            links={
                "end": {"href": "https://api.salesforce.com/einstein/ai-agent/v1/sessions/sess-123"},
            },
            created_at=time.time(),
            last_used=time.time(),
        )
        client._access_token = "mock-token"
        client._token_expiry = time.time() + 3600

        client._http.delete = AsyncMock(return_value=_mock_delete_response())

        result = await client.end_conversation("conv-1")

        assert result == {"conversation_id": "conv-1", "status": "ended"}
        assert "conv-1" not in client._conversations
        client._http.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_end_conversation_idempotent(self, client):
        """Ending an unknown conversation should return success (idempotent)."""
        result = await client.end_conversation("unknown-conv")

        assert result == {"conversation_id": "unknown-conv", "status": "ended"}


# ---------------------------------------------------------------------------
# TestMultiUserIsolation
# ---------------------------------------------------------------------------
class TestMultiUserIsolation:
    @pytest.mark.asyncio
    async def test_two_conversations_get_separate_sessions(self, client):
        """Two different conversation_ids should have separate Agentforce sessions."""
        client._http.post = AsyncMock(
            side_effect=[
                _mock_token_response(),
                _mock_session_response("sess-A"),
                _mock_message_response("Response A"),
                _mock_session_response("sess-B"),
                _mock_message_response("Response B"),
            ]
        )

        result_a = await client.send_message("conv-A", "Hello from A")
        result_b = await client.send_message("conv-B", "Hello from B")

        assert result_a["conversation_id"] == "conv-A"
        assert result_b["conversation_id"] == "conv-B"
        assert client._conversations["conv-A"].session_id == "sess-A"
        assert client._conversations["conv-B"].session_id == "sess-B"
        # Each has independent sequence counters
        assert client._conversations["conv-A"].sequence_id == 2
        assert client._conversations["conv-B"].sequence_id == 2


# ---------------------------------------------------------------------------
# TestStaleSessionCleanup
# ---------------------------------------------------------------------------
class TestStaleSessionCleanup:
    def test_cleanup_removes_old_sessions(self, client):
        """Sessions idle beyond max_age should be removed."""
        now = time.time()
        client._conversations["stale"] = SessionState(
            session_id="sess-old",
            sequence_id=1,
            links={},
            created_at=now - 3600,
            last_used=now - 3600,  # 1 hour ago
        )
        client._conversations["fresh"] = SessionState(
            session_id="sess-new",
            sequence_id=1,
            links={},
            created_at=now,
            last_used=now,
        )

        client._cleanup_stale_sessions(max_age_seconds=1800)

        assert "stale" not in client._conversations
        assert "fresh" in client._conversations


# ---------------------------------------------------------------------------
# TestExtractResponseText
# ---------------------------------------------------------------------------
class TestExtractResponseText:
    def test_extract_from_message_field(self):
        body = {"messages": [{"type": "Inform", "message": "Hello there"}]}
        assert AgentforceClient._extract_response_text(body) == "Hello there"

    def test_extract_from_content_text(self):
        body = {
            "messages": [
                {"type": "Inform", "content": {"text": "Content response"}}
            ]
        }
        assert AgentforceClient._extract_response_text(body) == "Content response"

    def test_extract_multiple_messages(self):
        body = {
            "messages": [
                {"type": "Inform", "message": "Part 1"},
                {"type": "Inform", "message": "Part 2"},
            ]
        }
        assert AgentforceClient._extract_response_text(body) == "Part 1\nPart 2"

    def test_extract_empty_messages(self):
        body = {"messages": [], "message": "Fallback"}
        assert AgentforceClient._extract_response_text(body) == "Fallback"
