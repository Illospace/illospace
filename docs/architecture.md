# Architecture

Illo Brain is a self-hostable AI workspace with a FastAPI backend, a SvelteKit
frontend, PostgreSQL/pgvector storage, and an agent runtime for tool-using work.

For a visual map of boundaries, data flow, deployment services, and operating
constraints, see [Architecture Diagram](architecture-diagram.md).

## Repository Map

```text
brain/
  app/               FastAPI app, CLI, scheduler, hooks, MCP, web adapters
  jobs/              Offline pipelines, evals, and recurring job code
  kernel/            Shared config, runtime primitives, and common helpers
  platform/          Database, provider, browser, GPU, and telemetry adapters
  systems/           Cortex, memory, skills, runs, vault, domains, and apps

frontend/
  src/lib/api/       Typed API and WebSocket client helpers
  src/lib/components Svelte UI components
  src/lib/stores/    Svelte state stores
  src/routes/        Application routes

ops/                 Generic local/self-hosting helpers and systemd templates
scripts/             Operator diagnostics and maintenance scripts
tests/               Backend, frontend utility, and regression tests
```

## Runtime Shape

- The API process serves REST endpoints, WebSocket streams, static frontend
  assets in production mode, and optional inline local run execution.
- The Cortex worker consumes queued AgentRuns for production/self-hosted
  deployments.
- PostgreSQL stores users, orgs, memories, ideas, runs, skills, vault metadata,
  workspace apps, notifications, cycles, and learning state.
- pgvector powers semantic retrieval when embeddings are configured.
- Provider keys can come from environment variables or encrypted database-backed
  credentials, depending on deployment mode.
- Optional GPU workers provide local embeddings or local LLM support; API-backed
  embeddings are the easiest public default.

## Extension Points

- Add API surfaces under `brain/app/api/routers/` and schemas under
  `brain/app/api/schemas/`.
- Add durable tables through SQLAlchemy models plus Alembic migrations.
- Add agent-visible behavior through `brain/systems/runs/tool_catalog/`.
- Add portable skill behavior under
  `brain/systems/skills/builtin_skill_bundles/` or via database/imported skill
  bundles.
- Add frontend product surfaces under `frontend/src/routes/` and shared
  primitives under `frontend/src/lib/components/constellation/`.

## Private State Boundary

The public source tree should stay free of local operator state. Runtime-private
files belong under `.illo/`, `ILLO_PRIVATE_HOME`, or deployment-specific config
paths such as `~/.config/illo-brain/production.env`.
