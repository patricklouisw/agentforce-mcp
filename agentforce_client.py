"""Async client for the Salesforce Agentforce Agent API."""

import dataclasses
import logging
import re
import time
import uuid

import httpx

logger = logging.getLogger("agentforce-mcp")

AGENT_API_BASE = "https://api.salesforce.com/einstein/ai-agent/v1"
TOKEN_CACHE_SECONDS = 5400  # 90 minutes
TOKEN_REFRESH_BUFFER = 300  # refresh 5 min before expiry
STALE_SESSION_MAX_AGE = 1800  # 30 minutes

# Reserved Agentforce context variable for end-user language. This is the only
# $Context variable that Agentforce allows updating after session start.
LANGUAGE_VARIABLE_NAME = "$Context.EndUserLanguage"

# Accept ISO 639-1 codes, optionally with an ISO 3166-1 region suffix (e.g. "en", "en_US").
_LANGUAGE_RE = re.compile(r"^[a-z]{2}(_[A-Z]{2})?$")


def _normalize_language(lang: str | None) -> str | None:
    """Validate and return a language locale, or None if unset.

    Raises ValueError on malformed input so the MCP tool surfaces a clear error
    to the LLM instead of letting Agentforce reject a mangled value.
    """
    if lang is None:
        return None
    lang = lang.strip()
    if not lang:
        return None
    if not _LANGUAGE_RE.match(lang):
        raise ValueError(
            f"Invalid language {lang!r}; expected ISO locale like 'en' or 'en_US'"
        )
    return lang


def _language_variable(lang: str) -> dict:
    """Build the Agentforce variables-array element for $Context.EndUserLanguage.

    Shape matches the documented Agent API variables example: {name, type, value}.
    """
    return {"name": LANGUAGE_VARIABLE_NAME, "type": "Text", "value": lang}


@dataclasses.dataclass
class SessionState:
    """Internal state for an active Agentforce session."""

    session_id: str
    sequence_id: int  # starts at 1, auto-incremented per message
    links: dict  # _links from session creation response
    created_at: float
    last_used: float
    language: str | None = None  # current $Context.EndUserLanguage value


class AgentforceClient:
    """Async client for the Salesforce Agentforce Agent API.

    Manages OAuth authentication, session lifecycle, and conversation-to-session
    mapping. VAPI passes its own conversation_id; this client transparently maps
    each conversation_id to an Agentforce session.
    """

    def __init__(
        self,
        my_domain_url: str,
        consumer_key: str,
        consumer_secret: str,
        agent_id: str,
        bypass_user: bool = True,
        default_language: str | None = None,
    ):
        self._my_domain_url = my_domain_url.rstrip("/")
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._agent_id = agent_id
        self._bypass_user = bypass_user
        self._default_language = _normalize_language(default_language)

        self._access_token: str | None = None
        self._token_expiry: float = 0.0

        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(130.0, connect=10.0),
        )

        # conversation_id -> SessionState
        self._conversations: dict[str, SessionState] = {}

    async def close(self):
        """Close the underlying HTTP client."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _authenticate(self) -> tuple[str, float]:
        """Perform OAuth client_credentials flow.

        Returns (access_token, expiry_timestamp).
        """
        url = f"{self._my_domain_url}/services/oauth2/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self._consumer_key,
            "client_secret": self._consumer_secret,
        }

        logger.info("Authenticating to Salesforce via client_credentials")
        resp = await self._http.post(
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()

        body = resp.json()
        token = body["access_token"]
        # Salesforce client_credentials may not return expires_in reliably
        expires_in = body.get("expires_in", TOKEN_CACHE_SECONDS)
        expiry = time.time() + float(expires_in)

        logger.info("Authenticated successfully")
        return token, expiry

    async def _ensure_token(self) -> str:
        """Return a valid access token, refreshing if expired or near-expiry."""
        if (
            self._access_token is None
            or time.time() >= self._token_expiry - TOKEN_REFRESH_BUFFER
        ):
            self._access_token, self._token_expiry = await self._authenticate()
        return self._access_token

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Session lifecycle (internal)
    # ------------------------------------------------------------------

    async def _create_session(self, language: str | None = None) -> SessionState:
        """Create a new Agentforce agent session. Returns internal SessionState.

        If `language` is provided it is seeded as $Context.EndUserLanguage on the
        new session; otherwise the client's default_language (if any) is used.
        """
        token = await self._ensure_token()

        initial_language = language or self._default_language

        url = f"{AGENT_API_BASE}/agents/{self._agent_id}/sessions"
        payload = {
            "externalSessionKey": str(uuid.uuid4()),
            "instanceConfig": {"endpoint": self._my_domain_url},
            "featureSupport": "Sync",
            "bypassUser": self._bypass_user,
        }
        if initial_language:
            payload["variables"] = [_language_variable(initial_language)]

        logger.info("Creating Agentforce session for agent %s", self._agent_id)
        resp = await self._http.post(
            url, json=payload, headers=self._auth_headers(token)
        )

        # On 401, invalidate token and retry once
        if resp.status_code == 401:
            logger.warning("Got 401 creating session, refreshing token and retrying")
            self._access_token = None
            token = await self._ensure_token()
            resp = await self._http.post(
                url, json=payload, headers=self._auth_headers(token)
            )

        if resp.status_code >= 400:
            logger.error("Agentforce session error %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        body = resp.json()

        now = time.time()
        session = SessionState(
            session_id=body["sessionId"],
            sequence_id=1,
            links=body.get("_links", {}),
            created_at=now,
            last_used=now,
            language=initial_language,
        )

        logger.info("Session created: %s", session.session_id)
        return session

    async def _end_session(self, session: SessionState) -> None:
        """End an Agentforce session via the API. Best-effort, does not raise."""
        try:
            token = await self._ensure_token()
            end_url = session.links.get("end", {}).get("href")
            if end_url:
                resp = await self._http.delete(
                    end_url, headers=self._auth_headers(token)
                )
                logger.info(
                    "Ended session %s (status=%s)", session.session_id, resp.status_code
                )
            else:
                # Fallback URL
                url = f"{AGENT_API_BASE}/sessions/{session.session_id}"
                resp = await self._http.delete(
                    url, headers=self._auth_headers(token)
                )
                logger.info(
                    "Ended session %s via fallback (status=%s)",
                    session.session_id,
                    resp.status_code,
                )
        except Exception:
            logger.warning(
                "Failed to end session %s (best-effort)", session.session_id, exc_info=True
            )

    # ------------------------------------------------------------------
    # Public API (called by MCP tools)
    # ------------------------------------------------------------------

    async def send_message(
        self,
        conversation_id: str,
        message: str,
        language: str | None = None,
    ) -> dict:
        """Send a message to the Agentforce agent.

        Auto-creates a session on first call for a given conversation_id.
        Reuses the existing session on subsequent calls.

        If `language` is provided, it is applied as $Context.EndUserLanguage:
        seeded at session creation on the first turn, or updated mid-session
        on later turns when it differs from the current session language.
        """
        self._cleanup_stale_sessions()

        try:
            normalized_language = _normalize_language(language)

            # Get or create session
            if conversation_id not in self._conversations:
                session = await self._create_session(language=normalized_language)
                self._conversations[conversation_id] = session
                logger.info(
                    "Mapped conversation %s -> session %s",
                    conversation_id,
                    session.session_id,
                )

            session = self._conversations[conversation_id]

            # Build request
            token = await self._ensure_token()
            messages_url = session.links.get("messages", {}).get("href")
            if not messages_url:
                messages_url = f"{AGENT_API_BASE}/sessions/{session.session_id}/messages"
            # Append ?sync=true for synchronous response
            if "?" not in messages_url:
                messages_url += "?sync=true"

            # Only emit the language variable when it actually changes, so we
            # don't churn the variables array on every turn.
            message_variables: list[dict] = []
            language_changed = (
                normalized_language is not None
                and normalized_language != session.language
            )
            if language_changed:
                message_variables.append(_language_variable(normalized_language))
                logger.info(
                    "Updating $Context.EndUserLanguage on session %s: %s -> %s",
                    session.session_id,
                    session.language,
                    normalized_language,
                )

            payload = {
                "message": {
                    "sequenceId": session.sequence_id,
                    "type": "Text",
                    "text": message,
                },
                "variables": message_variables,
            }

            logger.info(
                "Sending message to session %s (seq=%d): %s",
                session.session_id,
                session.sequence_id,
                message[:100],
            )

            resp = await self._http.post(
                messages_url, json=payload, headers=self._auth_headers(token)
            )

            # On 401, invalidate token and retry once
            if resp.status_code == 401:
                logger.warning("Got 401 sending message, refreshing token and retrying")
                self._access_token = None
                token = await self._ensure_token()
                resp = await self._http.post(
                    messages_url, json=payload, headers=self._auth_headers(token)
                )

            if resp.status_code >= 400:
                logger.error(
                    "Agentforce API error %s: %s", resp.status_code, resp.text
                )
            resp.raise_for_status()
            body = resp.json()

            # Increment sequence and update last_used
            session.sequence_id += 1
            session.last_used = time.time()
            if language_changed:
                session.language = normalized_language

            # Extract response text from messages
            response_text = self._extract_response_text(body)

            return {
                "conversation_id": conversation_id,
                "response": response_text,
            }

        except httpx.HTTPStatusError as e:
            logger.error("HTTP error sending message: %s", e)
            return {
                "error": f"Agentforce API error: {e.response.status_code} - {e.response.text}"
            }
        except httpx.RequestError as e:
            logger.error("Request error sending message: %s", e)
            return {"error": f"Failed to reach Agentforce API: {e}"}

    async def end_conversation(self, conversation_id: str) -> dict:
        """End a conversation and clean up its Agentforce session.

        Idempotent: returns success even if conversation_id is unknown.
        """
        session = self._conversations.pop(conversation_id, None)
        if session:
            await self._end_session(session)
            logger.info("Ended conversation %s (session %s)", conversation_id, session.session_id)
        else:
            logger.info("Conversation %s not found (already ended or never started)", conversation_id)

        return {"conversation_id": conversation_id, "status": "ended"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_response_text(body: dict) -> str:
        """Extract agent response text from the API response body."""
        messages = body.get("messages", [])
        parts = []
        for msg in messages:
            # Handle different message formats from the API
            if isinstance(msg, dict):
                # Direct message field
                if "message" in msg:
                    parts.append(msg["message"])
                # Nested content.text field
                elif "content" in msg and isinstance(msg["content"], dict):
                    text = msg["content"].get("text", "")
                    if text:
                        parts.append(text)
        return "\n".join(parts) if parts else body.get("message", "")

    def _cleanup_stale_sessions(self, max_age_seconds: int = STALE_SESSION_MAX_AGE):
        """Remove conversations whose sessions have been idle too long."""
        now = time.time()
        stale = [
            cid
            for cid, session in self._conversations.items()
            if now - session.last_used > max_age_seconds
        ]
        for cid in stale:
            session = self._conversations.pop(cid)
            logger.info(
                "Cleaning up stale conversation %s (session %s, idle %.0fs)",
                cid,
                session.session_id,
                now - session.last_used,
            )
            # Best-effort end session - fire and forget since we can't await here
            # The session will time out on Salesforce's side anyway
