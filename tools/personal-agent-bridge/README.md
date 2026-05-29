# Personal Agent Bridge

Neutral bridge between Illo external-agent tasks and personal agents such as
Hermes or OpenClaw.

## Architecture

- `POST /mcp` (also `POST /api/mcp`): hosted remote MCP endpoint. This is
  the MCP path for Hermes, Codex, OpenClaw, or another remote-capable MCP
  client. Clients provide `Authorization: Bearer illo_conn_...`.
- `tools/personal-agent-bridge/bridge.py`: outbound worker. Illo queues external
  tasks, the bridge claims them, runs Hermes/OpenClaw, then posts results back.

MVP deliverable is hosted MCP for personal-agent -> Illo collaboration. The
outbound bridge remains proven for Hermes task delegation, but OpenClaw adapter
work is intentionally deferred.

Hosted MCP client config:

Create the bearer token from Illo Vault -> Personal agents -> New token. The
raw token is shown once, alongside this config shape:

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

Some clients store bearer tokens separately instead of inline headers. In that
case, use the client-specific secret/auth field and keep the MCP URL as
`https://illo.example.com/mcp`.

## CI coverage

Default CI should run the no-network contract tests:

```bash
venv/bin/python -m pytest tests/test_external_agents_service.py tests/test_external_agent_routes.py tests/test_alembic_config.py -q
```

These tests cover:

- bridge token scope helpers
- bridge `run_once` claim -> start event -> adapter -> complete flow
- Hermes `/v1/chat/completions` request contract
- Hermes `/v1/runs` polling contract
- hosted Illo MCP tool descriptions and route/auth contracts
- Illo bridge routes and fanout commit ordering
- fresh Alembic baseline replay for external-agent tables

Live Hermes coverage is opt-in because it needs a running Hermes gateway and
provider credentials:

```bash
ILLO_LIVE_HERMES_SMOKE=1 \
HERMES_BASE_URL=http://127.0.0.1:8642 \
HERMES_API_KEY=illo-hermes-local \
venv/bin/python -m pytest tests/test_external_agents_service.py::test_live_hermes_runs_adapter_smoke -q
```

Use this in self-hosted CI, release gates, or manual smoke before deploying
bridge changes. Public/default CI should leave `ILLO_LIVE_HERMES_SMOKE` unset.
