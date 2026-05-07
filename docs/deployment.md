# Deployment

Illo Brain is currently an early self-hosted preview. The supported public path
is local development or a single self-managed Linux host.

## Local Development

```bash
./illo setup
./illo
```

`.env` is optional. Copy `.env.example` to `.env` only when you want file-based
local overrides; otherwise export environment variables in your shell or use
your platform's secret manager.

See [configuration.md](configuration.md) for the full environment contract.

The launcher can start a local pgvector Docker container when PostgreSQL is not
already reachable. It also installs Python/frontend dependencies and browser
runtime support.

On a fresh Linux host with another PostgreSQL already listening on `DB_PORT`,
the launcher will not take over that port. If `DB_PORT` was not explicitly set
and the host is local, it can choose an alternate port for the Docker pgvector
database automatically. If `DB_PORT` is pinned, either install pgvector and
create the configured Illo database/user in that server, update the DB settings,
or unset `DB_PORT` so the launcher can choose a Docker fallback port.

GPU embedding models are lazy by default. Configure GPU embeddings from the
System tab after first boot, or set `ILLO_DOWNLOAD_GPU_MODELS=1` before setup to
prefetch the repo-local Hugging Face model copies.

## Generic Self-Hosting

1. Clone the repository onto the target host.
2. Create a private environment file, for example
   `~/.config/illo-brain/production.env`.
3. Set `ILLO_ENV=production`, database settings, `SECRET_KEY`,
   `VAULT_MASTER_KEY`, and provider keys or database-backed credentials.
4. Run `./illo setup`.
5. Install the systemd templates in `ops/` after adjusting paths if your checkout
   is not `~/illo-brain`.
6. Run `./illo doctor --production` before restarting services.

The included `ops/deploy-remote.sh` is a generic SSH helper. It intentionally has
no project-specific host defaults:

```bash
./ops/deploy-remote.sh --host example.com --user illo --dir illo-brain
```

## Ops Directory

The public `ops/` tree is intentionally small:

- `deploy.sh` and `deploy-remote.sh` for single-host deployments.
- systemd user-service templates for the scheduler, Cortex worker, and optional
  embedding/GPU server.
- browser and frontend dependency helpers used by the launcher.
- `test-with-db.sh` for the Docker-backed test database.

Deployment-specific secrets, one-off production migrations, cron wrappers, and
private hook bundles do not belong in the public tree.

## Production Notes

- Keep `.env`, `production.env`, database dumps, uploads, logs, and operator
  notes out of git.
- Prefer `EMBEDDING_BACKEND=api` for the simplest install.
- Use GPU workers only when the host has the right CUDA/PyTorch stack.
- Run Alembic migrations before starting workers.
- Treat browser automation and tool execution as privileged local capabilities.
