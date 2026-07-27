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

GPU embedding models are lazy by default. Configure GPU embeddings from the
System tab after first boot, or set `ILLO_DOWNLOAD_GPU_MODELS=1` before setup to
prefetch the repo-local Hugging Face model copies.

## Generic Self-Hosting

For a fresh single-host install, run the installer on the target Linux user:

```bash
curl -fsSL https://illospace.ai/install.sh | bash
```

The installer clones or updates `~/illospace`, writes
`~/.config/illo-brain/production.env`, installs user services, runs migrations,
builds the frontend, and starts the API. If no `DATABASE_URL` or complete
`DB_*` settings are present, it creates a local `pgvector/pgvector:pg16`
container named `illo-db` and writes the generated `DB_*` values to the
production env file. Provider keys can be supplied before running it, for
example:

```bash
export OPENAI_API_KEY='sk-...'
curl -fsSL https://illospace.ai/install.sh | bash
```

To use an external database instead:

```bash
export DATABASE_URL='postgresql+psycopg://user:password@host:5432/illospace'
curl -fsSL https://illospace.ai/install.sh | bash
```

Useful overrides:

```bash
curl -fsSL https://illospace.ai/install.sh | bash -s -- --dir /home/uwear/illospace
curl -fsSL https://illospace.ai/install.sh | bash -s -- --no-deploy
```

1. Clone the repository onto the target host.
2. Create a private environment file, for example
   `~/.config/illo-brain/production.env`.
3. Set `ILLO_ENV=production`, database settings, `SECRET_KEY`,
   `VAULT_MASTER_KEY`, and provider keys or database-backed credentials.
4. Run `./illo setup`.
5. Run `./ops/deploy.sh` to sync dependencies, build the frontend, run
   migrations, install user services, and restart the app.
6. Run `./illo doctor --production` when changing production configuration.

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

- `deploy.sh` and `deploy-remote.sh` for single-host deployments. They render
  systemd user services for the current checkout path.
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

## External Illo Deadman

Runtime liveness must be observed outside Illo's host. The scheduler publishes
a heartbeat every five minutes to the public orphan branch `ops/heartbeat` in
`Illospace/illospace`; `.github/workflows/illo-deadman.yml` reads it without
using the host, its database, or the tailnet. The dependency-free watcher lives
at `ops/external_deadman/watcher.py`, outside `brain/jobs/`, because it runs on
GitHub rather than on Illo. The public heartbeat is deliberately limited to
this shape:

```json
{"ts":"2026-07-27T12:00:00Z","last_run_id":2718,"last_surface":"slack"}
```

No credential, customer identifier, thread content, prompt, response, or error
body belongs in that branch. The branch also carries the watcher's alarm
delivery state.

The emitter reuses Illo's org-owned GitHub App project binding for
`illospace/illospace` and mints a repository-scoped installation token with
`contents:write`. The installed App must have Contents read/write approval.
There is no separate heartbeat PAT. If the binding or token is unavailable, the
scheduler job records a clean skip instead of entering a crash loop. If three
compare-and-swap publish attempts conflict, it records a successful
`conflict_skipped` outcome with the attempt count and final conflict status; the
next five-minute heartbeat supersedes that missed write, while a sustained
failure is escalated by the heartbeat becoming stale.

In the `Illospace/illospace` repository settings, allow Actions workflows read
and write access, then create these two Actions secrets:

- `SLACK_DEADMAN_WEBHOOK_URL` — required incoming-webhook URL targeting
  `#alerts`. Configure the webhook with a distinct identity such as
  `Illo Deadman (GitHub)`; it must not look like Illo and must not use Illo's
  Slack bot token.
- `ILLO_DEADMAN_HEALTHCHECK_URL` — the success-ping URL for an independent
  healthchecks.io, Cronitor, or equivalent check. This self-monitor is optional
  to the workflow so first deployment is not blocked, but production should
  configure it. When absent, every successful watcher run emits a prominent
  warning rather than claiming self-visibility.

GitHub's five-minute schedule is a minimum interval, not a delivery guarantee.
The watcher declares a 12-minute staleness threshold so ordinary scheduler
delay does not trigger on one late run while the alarm normally still lands
within about 15 minutes. GitHub disables scheduled workflows after 60 days
without repository activity; the independent success-ping monitor is what makes
that silent-stop mode observable.

Slack alarm and recovery notices use at-least-once delivery. The watcher posts
first, then persists the state transition. If Slack accepts a notice but the
GitHub state write fails, the next run posts it again; persisting first would
risk suppressing the only notice when Slack fails. Alarm and recovery messages
carry the same stable outage ID so operators can recognize a repost as another
delivery for the existing outage rather than a new outage.

For a non-destructive setup check, run the **Illo External Deadman** workflow
manually with `force_stale=true` and leave `dry_run=true`. The run must report an
`alarm` action and a dry-run notice without posting to Slack or changing the
stored delivery state.
