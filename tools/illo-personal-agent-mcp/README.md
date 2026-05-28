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

- `illo_submit_context`: default path for personal agents sending new context to Illo. Sends an intent, ordered context parts, provenance, constraints, correlation, and idempotency key so Illo can store the context without starting a run or creating a visible Thread. When correlation points to an existing Thread, the context is attached there and the result includes `thread_url`.
- `illo_search_workspace`: search existing Illo ideas/threads before creating duplicates.
- `illo_get_thread`: inspect visible context for an existing Illo thread.
- `illo_create_thread`: advanced compatibility tool for explicitly requested visible team threads.
- `illo_post_thread_message`: advanced compatibility tool for explicitly targeted existing threads.
- `illo_ask`: ask Illo for private workspace context without creating a visible thread.
- `illo_get_ask`: poll a headless ask created by `illo_ask`.
- `illo_get_team_members`: resolve teammates before sharing work.

Behavior guidance lives in tool descriptions so MCP clients can use this package
without a separate skill or prompt file.

## Context Submission

Personal agents should prefer `illo_submit_context` when they need to hand new
conversation, trace, file, diff, link, or artifact context to Illo without
summoning a visible Illo response. A typical payload:

```json
{
  "intent": "Share the Codex thread so the team can inspect the exact context and decide what to do next.",
  "origin": "codex.context",
  "source_tool": "codex",
  "repo": "illospace-project",
  "branch": "codex/universal-thread-context",
  "task_title": "Universal Thread context ingress",
  "files_touched": [
    "brain/app/api/routers/agent_mcp.py",
    "tests/test_external_agent_routes.py"
  ],
  "parts": [
    {
      "type": "conversation",
      "title": "Codex conversation",
      "content": "Full or bounded conversation text goes here."
    },
    {
      "type": "diff",
      "title": "Current code diff",
      "content": "Diff or artifact reference goes here."
    }
  ],
  "correlation": {
    "thread_id": "optional-existing-illo-thread-id"
  },
  "idempotency_key": "codex:universal-thread-context:2026-05-21T18:30Z"
}
```

The submission may include `correlation.thread_id` when the user explicitly
means to attach context to an existing Thread. It should not choose projects,
pins, teammates, or workflow-specific outcomes. Illo owns coordination in the
team workspace.
