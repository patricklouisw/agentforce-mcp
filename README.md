# agentforce-mcp

An MCP (Model Context Protocol) server that connects VAPI to the Salesforce Agentforce Agent API. VAPI sends user queries via MCP tools, and this server forwards them to an Agentforce AI agent, returning the agent's responses.

## Tools

| Tool | Description |
|------|-------------|
| `send_message(message, language=None)` | Send a message to the Agentforce agent and get a response. Optional `language` (ISO locale, e.g. `en_US`, `es_ES`, `fr_FR`) sets the `$Context.EndUserLanguage` context variable so the agent answers in that language — and can be changed mid-conversation. |
| `end_conversation()` | End a conversation and clean up its Agentforce session. Idempotent. |

Both tools accept an optional `conversation_id` parameter, but when called from VAPI this is **automatically resolved** from VAPI's request headers — the LLM does not need to track it.

### Language switching

`send_message` accepts an optional `language` parameter (ISO locale like `en_US`, `es_ES`). The server maps it to Agentforce's reserved `$Context.EndUserLanguage` context variable, which is the only `$Context` variable Agentforce allows updating after session start — so the LLM can switch languages at any turn without restarting the conversation. If the caller does not pass `language` on subsequent turns the current language is preserved. A default language can also be seeded on session creation via the optional `SF_DEFAULT_LANGUAGE` env var.

### How it works

1. **VAPI calls `send_message`** with just the user's message
2. The server reads VAPI's `X-Call-Id` (voice) or `X-Chat-Id` / `X-Session-Id` (chat) header to identify the conversation
3. On the first message for that ID, the server automatically creates an Agentforce session
4. On subsequent messages, the server reuses the existing session (multi-turn conversation)
5. When the call ends, VAPI calls `end_conversation` to clean up (or the server auto-cleans after 30 min inactivity)

Multiple concurrent users are fully isolated — each VAPI call/chat gets its own Agentforce session with independent state.

### VAPI header support

| Header | VAPI interaction type | Priority |
|--------|----------------------|----------|
| `X-Call-Id` | Voice calls | 1 (highest) |
| `X-Chat-Id` | Chat interactions | 2 |
| `X-Session-Id` | Chat sessions | 3 |

## Prerequisites

1. **Salesforce Org** with Agentforce enabled and an activated agent
2. **External Client App (ECA)** configured with OAuth scopes:
   - `api` (Manage user data via APIs)
   - `refresh_token, offline_access` (Perform requests at any time)
   - `chatbot_api` (Access chatbot services)
   - `sfap_api` (Access the Salesforce API Platform)
3. **Client Credentials Flow** enabled on the ECA with a Run As user
4. **Agent ID** — see [Salesforce docs](https://developer.salesforce.com/docs/ai/agentforce/guide/agent-api-agent-id.html)

## Setup

### Install dependencies

```bash
uv sync
```

### Configure environment variables

Copy `.env` and fill in your values:

```bash
# Salesforce Agentforce Configuration
SF_MY_DOMAIN_URL=https://your-domain.my.salesforce.com
SF_CONSUMER_KEY=your-consumer-key
SF_CONSUMER_SECRET=your-consumer-secret
SF_AGENT_ID=your-agent-id
SF_BYPASS_USER=true
SF_DEFAULT_LANGUAGE=en_US  # optional; ISO locale seeded on new sessions

# MCP Server Configuration
MCP_API_KEY=your-api-key
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
MCP_PORT=8000
LOG_LEVEL=INFO
```

### Run the server

```bash
# HTTP mode (for VAPI and other HTTP clients)
uv run server.py

# stdio mode (for Claude Desktop)
MCP_TRANSPORT=stdio uv run server.py

# Dev mode with MCP Inspector
uv run mcp dev server.py
```

The HTTP server starts on `http://0.0.0.0:8000` by default. Override with `MCP_HOST` and `MCP_PORT`.

## VAPI Configuration

1. In VAPI Dashboard, go to **Tools > Create Tool** and select **MCP** type
2. Set the server URL to `https://your-server-url/mcp`
3. Add the Authorization header if `MCP_API_KEY` is set:

```json
{
  "type": "mcp",
  "function": {
    "name": "mcpTools"
  },
  "server": {
    "url": "https://your-server-url/mcp",
    "headers": {
      "Authorization": "Bearer your-api-key"
    }
  }
}
```

4. Attach the MCP tools to your VAPI assistant
5. In your assistant's system prompt, instruct it to call `send_message` with the user's question — no need to manage `conversation_id`, it's handled automatically

## Claude Desktop Configuration

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentforce-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/agentforce-mcp", "server.py"],
      "env": {
        "SF_MY_DOMAIN_URL": "https://your-domain.my.salesforce.com",
        "SF_CONSUMER_KEY": "your-consumer-key",
        "SF_CONSUMER_SECRET": "your-consumer-secret",
        "SF_AGENT_ID": "your-agent-id",
        "MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

## Docker

```bash
docker build -t agentforce-mcp .
docker run -p 8000:8000 \
  -e SF_MY_DOMAIN_URL="https://your-domain.my.salesforce.com" \
  -e SF_CONSUMER_KEY="your-consumer-key" \
  -e SF_CONSUMER_SECRET="your-consumer-secret" \
  -e SF_AGENT_ID="your-agent-id" \
  agentforce-mcp
```

## Testing

```bash
uv run pytest -v
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SF_MY_DOMAIN_URL` | Yes | — | Salesforce My Domain URL (e.g. `https://mycompany.my.salesforce.com`) |
| `SF_CONSUMER_KEY` | Yes | — | Connected App OAuth consumer key |
| `SF_CONSUMER_SECRET` | Yes | — | Connected App OAuth consumer secret |
| `SF_AGENT_ID` | Yes | — | Agentforce Agent ID |
| `SF_BYPASS_USER` | No | `true` | `true` = agent-assigned user, `false` = token user |
| `SF_DEFAULT_LANGUAGE` | No | — | ISO locale (e.g. `en_US`) seeded as `$Context.EndUserLanguage` when a new session is created. If unset, the agent uses its configured default until the LLM passes `language` on a `send_message` call. |
| `MCP_API_KEY` | No | — | Bearer token for VAPI authentication (disabled if unset) |
| `MCP_TRANSPORT` | No | `streamable-http` | `streamable-http` (HTTP server) or `stdio` (Claude Desktop) |
| `MCP_HOST` | No | `0.0.0.0` | HTTP bind address |
| `MCP_PORT` | No | `8000` | HTTP port |
| `LOG_LEVEL` | No | `INFO` | Logging level |
