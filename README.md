# agentforce-mcp

An MCP (Model Context Protocol) server that connects VAPI to the Salesforce Agentforce Agent API. VAPI sends user queries via MCP tools, and this server forwards them to an Agentforce AI agent, returning the agent's responses.

## Tools

| Tool | Description |
|------|-------------|
| `send_message(conversation_id, message)` | Send a message to the Agentforce agent. Auto-creates a session on the first call for a given `conversation_id`, and reuses it on subsequent calls. |
| `end_conversation(conversation_id)` | End a conversation and clean up its Agentforce session. Idempotent. |

### How it works

1. **VAPI calls `send_message`** with its own call/conversation ID and the user's message
2. On the first call for that ID, the server automatically creates an Agentforce session
3. On subsequent calls, the server reuses the existing session (multi-turn conversation)
4. When the call ends, VAPI calls `end_conversation` to clean up
5. Stale sessions (idle >30 min) are automatically cleaned up

Multiple concurrent users are fully isolated — each `conversation_id` maps to its own Agentforce session with independent state.

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

```bash
export SF_MY_DOMAIN_URL="https://your-domain.my.salesforce.com"
export SF_CONSUMER_KEY="your-consumer-key"
export SF_CONSUMER_SECRET="your-consumer-secret"
export SF_AGENT_ID="your-agent-id"

# Optional
export MCP_API_KEY="your-api-key"          # Bearer auth for VAPI
export SF_BYPASS_USER="true"               # Use agent-assigned user (default)
export LOG_LEVEL="INFO"
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
| `MCP_API_KEY` | No | — | Bearer token for VAPI authentication (disabled if unset) |
| `MCP_TRANSPORT` | No | `sse` | `sse` (HTTP server) or `stdio` (Claude Desktop) |
| `MCP_HOST` | No | `0.0.0.0` | HTTP bind address |
| `MCP_PORT` | No | `8000` | HTTP port |
| `LOG_LEVEL` | No | `INFO` | Logging level |
