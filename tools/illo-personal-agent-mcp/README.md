# Illo Personal Agent MCP

Local stdio MCP server that gives personal agents tools to collaborate with an
Illo workspace. This package is optional: the preferred MVP is the hosted Illo
MCP endpoint at `https://illo.example.com/mcp`.

Use this package for local development or for clients that cannot connect to
hosted remote MCP yet.

## Hosted MCP

Create the bearer token from Illo Vault -> Personal agents -> New token. The
raw token is shown once, alongside a ready-to-copy client config.

Preferred client config:

```json
{
  "mcpServers": {
    "illo": {
      "url": "https://illo.example.com/mcp",
      "headers": {
        "Authorization": "Bearer illo_conn_..."
      }
    }
  }
}
```

## Local Configure

The server needs one scoped Illo bridge token:

```bash
export ILLO_BASE_URL="https://illo.example.com"
export ILLO_BRIDGE_TOKEN="illo_conn_..."
```

MCP config:

```json
{
  "mcpServers": {
    "illo": {
      "command": "uvx",
      "args": ["illo-personal-agent-mcp"],
      "env": {
        "ILLO_BASE_URL": "https://illo.example.com",
        "ILLO_BRIDGE_TOKEN": "illo_conn_..."
      }
    }
  }
}
```

Local repo config before package publish:

```json
{
  "mcpServers": {
    "illo": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/illospace-project/tools/illo-personal-agent-mcp",
        "illo-personal-agent-mcp"
      ],
      "env": {
        "ILLO_BASE_URL": "http://127.0.0.1:8000",
        "ILLO_BRIDGE_TOKEN": "illo_conn_..."
      }
    }
  }
}
```

## Tools

- `illo_submit_signal`: default path for automatic hooks and routine progress updates from Codex-style personal tools. Sends a signal envelope with summary, origin, payload, hints, desired outcome, and idempotency key so IloSpace can decide where it belongs.
- `illo_search_workspace`: search existing Illo ideas/threads before creating duplicates.
- `illo_get_thread`: inspect visible context for an existing Illo thread.
- `illo_create_thread`: advanced compatibility tool for explicitly requested visible team threads.
- `illo_post_thread_message`: advanced compatibility tool for explicitly targeted existing threads.
- `illo_ask`: ask Illo for private workspace context without creating a visible thread.
- `illo_get_ask`: poll a headless ask created by `illo_ask`.
- `illo_get_team_members`: resolve teammates before sharing work.

Behavior guidance lives in tool descriptions so MCP clients can use this package
without a separate skill or prompt file.

## Codex-Style Progress Signal

Automatic hooks should prefer `illo_submit_signal` instead of searching for a
thread and posting directly. A typical payload:

```json
{
  "summary": "Implemented the MCP submit-signal tool and added tests.",
  "origin": "codex.progress",
  "source_tool": "codex",
  "repo": "illospace-project",
  "branch": "codex/mcp-submit-signal",
  "task_title": "MCP personal-tool signal lane",
  "files_touched": [
    "brain/app/api/routers/agent_mcp.py",
    "tests/test_external_agent_routes.py"
  ],
  "desired_outcome": "team_update",
  "idempotency_key": "codex:mcp-submit-signal:2026-05-18T18:30Z"
}
```

The signal may include context hints, but it should not choose an Ilo thread,
project, pin, or teammate target. IloSpace owns routing and Ilo handles
ambiguity.
