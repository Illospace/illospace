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

- `illo_search_workspace`: search existing Illo ideas/threads before creating duplicates.
- `illo_get_thread`: inspect visible context for an existing Illo thread.
- `illo_create_thread`: publish personal-agent work into Illo as a visible team thread.
- `illo_post_thread_message`: update an existing Illo thread.
- `illo_ask`: ask Illo for private workspace context without creating a visible thread.
- `illo_get_ask`: poll a headless ask created by `illo_ask`.
- `illo_get_team_members`: resolve teammates before sharing work.

Behavior guidance lives in tool descriptions so MCP clients can use this package
without a separate skill or prompt file.
