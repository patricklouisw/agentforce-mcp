# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Model Context Protocol (MCP) server** named `agentforce-mcp`, built with Python 3.12 and the [`mcp[cli]`](https://github.com/modelcontextprotocol/python-sdk) package. It bridges VAPI with the Salesforce Agentforce Agent API, forwarding user queries to an Agentforce AI agent and returning responses.

## Architecture

- **server.py** - MCP server entry point. Defines tools, middleware (auth, VAPI ID extraction), health check, and app factory.
- **agentforce_client.py** - Async client for the Agentforce Agent API. Handles OAuth authentication, session lifecycle, and conversation-to-session mapping.
- **.env** - Environment variables (not committed). Loaded automatically via `python-dotenv`.

### VAPI Integration

The server automatically extracts VAPI's conversation identifier from HTTP headers via `VapiIdMiddleware`:
- `X-Call-Id` (voice calls, highest priority)
- `X-Chat-Id` (chat interactions)
- `X-Session-Id` (chat sessions)

This means the LLM only needs to pass `message` to `send_message` — no `conversation_id` tracking required.

## Package Management

This project uses [`uv`](https://github.com/astral-sh/uv) for dependency management.

```bash
# Install dependencies
uv sync

# Run the server (HTTP mode, default)
uv run server.py

# Run in stdio mode (for Claude Desktop)
MCP_TRANSPORT=stdio uv run server.py

# Run via MCP CLI (dev mode with inspector)
uv run mcp dev server.py

# Install for Claude Desktop
uv run mcp install server.py

# Run tests
uv run pytest -v
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `send_message(message)` | Send a message to the Agentforce agent. Auto-creates session on first call, reuses on subsequent calls. `conversation_id` is auto-resolved from VAPI headers. |
| `end_conversation()` | End a conversation and clean up its Agentforce session. Idempotent. `conversation_id` is auto-resolved from VAPI headers. |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SF_MY_DOMAIN_URL` | Yes | - | Salesforce My Domain URL |
| `SF_CONSUMER_KEY` | Yes | - | OAuth consumer key |
| `SF_CONSUMER_SECRET` | Yes | - | OAuth consumer secret |
| `SF_AGENT_ID` | Yes | - | Agentforce Agent ID |
| `SF_BYPASS_USER` | No | `true` | Use agent-assigned user vs token user |
| `MCP_API_KEY` | No | - | Bearer token for VAPI auth |
| `MCP_TRANSPORT` | No | `streamable-http` | `streamable-http` (HTTP) or `stdio` |
| `MCP_HOST` | No | `0.0.0.0` | HTTP bind address |
| `MCP_PORT` | No | `8000` | HTTP port |
| `LOG_LEVEL` | No | `INFO` | Logging level |

## Testing

```bash
# Run all tests
uv run pytest -v

# Run only client tests
uv run pytest test_agentforce_client.py -v

# Run only server tests
uv run pytest test_server.py -v
```
