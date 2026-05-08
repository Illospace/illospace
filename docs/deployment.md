# Deployment

Illospace has one blessed team-server path: Docker Compose on a single Linux VM.
Use [server-setup.md](server-setup.md) for the exact runbook.

## Recommended Production Path

```bash
git clone https://github.com/Illospace/illospace.git
cd illospace
./illo deploy up
```

The Compose stack runs a private web entrypoint, the FastAPI API, the AgentRun
worker, the scheduler, a one-shot migration job, and Postgres with pgvector.
It binds the browser entrypoint to `127.0.0.1:8080`; use SSH, a VPN, a tunnel,
or your own reverse proxy for team access. `./illo deploy` wraps the underlying
Compose files and scripts in `deploy/`.

## Local Development

Use the native launcher for a local preview:

```bash
./illo
```

The launcher can start a local pgvector database, install Python/frontend
dependencies, and prepare browser runtime support. `.env` remains optional for
local overrides. For frontend hot reload, run `./illo dev`.

## Native/Systemd Path

The `ops/` directory still contains native virtualenv and systemd templates for
operators who need to integrate with an existing host layout:

- `deploy.sh` and `deploy-remote.sh` for single-host native deployments
- systemd user-service templates for the scheduler, Cortex worker, and optional
  embedding/GPU server
- browser and frontend dependency helpers used by the launcher
- `test-with-db.sh` for the Docker-backed test database

Treat this as advanced. The Compose path is the open-source team-server
contract.

## Production Notes

- Keep `deploy/compose/.env`, `.env`, database dumps, uploads, logs, and
  operator notes out of git.
- Prefer `EMBEDDING_BACKEND=api` for the simplest team-server install.
- Use GPU workers only when the host has the right CUDA/PyTorch stack.
- Let the `migrate` service run Alembic migrations before API, worker, and
  scheduler start.
- Treat browser automation and tool execution as privileged server
  capabilities.
