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

- `illo_submit`: default async path for personal agents sending ordered context,
  instructions, or work handoffs to Illo. Sends a message, parts, provenance,
  constraints, correlation, response hints, and idempotency information so Illo
  can decide what to do.
- `illo_read`: non-mutating workspace read lane. Use for search, known Thread
  or Domain reads, teammate resolution, and capability discovery.
- `illo_act`: explicit user-authorized action lane. Use for visible team
  coordination such as creating/updating Threads, writing Domain records, or
  asking Illo to actively respond through a named capability.
- `illo_get_result`: retrieve or poll asynchronous results returned by
  `illo_submit`, `illo_read`, or `illo_act`.

Behavior guidance lives in tool descriptions so MCP clients can use this package
without a separate skill or prompt file.

The local package is intentionally a thin forwarder. Each local tool call is
sent to the hosted Illo MCP endpoint as a JSON-RPC `tools/call` request with the
same tool name and arguments.

## Submit

Personal agents should prefer `illo_submit` when they need to hand new
conversation, trace, file, diff, link, or artifact context to Illo. A typical
payload:

```json
{
  "message": "Review the Codex thread and decide what the team should do next.",
  "origin": "codex.submit",
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
  "response": {
    "mode": "webhook"
  },
  "idempotency_key": "codex:universal-thread-context:2026-05-21T18:30Z"
}
```

The submission may include `correlation.thread_id` or `correlation.thread_url`
when the user is already working around an existing Thread. It should not choose
projects, pins, teammates, or workflow-specific outcomes. Illo owns coordination
in the team workspace.

## Read

Use `illo_read` for context lookup that should not mutate team-visible state.
A typical payload:

```json
{
  "capability": "workspace.search",
  "arguments": {
    "query": "universal thread context ingress",
    "limit": 10
  }
}
```

## Act

Use `illo_act` only when the user has asked for a visible team action or for
Illo to actively coordinate. A typical payload:

```json
{
  "capability": "thread.create",
  "arguments": {
    "title": "Universal Thread context ingress update",
    "body": "Implemented local MCP forwarding and tests.",
    "teammate_user_ids": ["user_123"],
    "trigger_illo": false
  },
  "reason": "Share this implementation status with the team.",
  "idempotency_key": "codex:universal-thread-context:share-status"
}
```

## Results

When any tool returns a `result_id`, use `illo_get_result` instead of repeating
the original request:

```json
{
  "result_id": "result_123",
  "include_payload": true,
  "limit": 10
}
```
