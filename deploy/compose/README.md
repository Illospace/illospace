# Illospace Compose Deployment

This is the canonical single-server open-source path for Illospace. It is meant
to be easy for a human operator or an LLM agent to run without installing
public ingress, TLS automation, or cloud-specific infrastructure.

## What It Runs

- `web`: private HTTP entrypoint for the dashboard plus `/api` and `/ws` proxying
- `api`: FastAPI backend
- `worker`: standalone AgentRun worker
- `scheduler`: recurring job daemon
- `updater`: owner-triggered self-update sidecar with access to the host repo and Docker socket
- `migrate`: one-shot Alembic migration job
- `postgres`: Postgres 16 with pgvector

Persistent data lives in Docker volumes. Server bootstrap secrets live in
`deploy/compose/.env`; model provider credentials are added inside Illospace
after boot and stored encrypted in Postgres.

## Fresh Server Setup

Prerequisites on the server:

- Linux VM
- Docker Engine with Docker Compose v2
- SSH access

From the repository root:

```bash
./illo deploy up
```

`deploy up` creates `deploy/compose/.env` with generated secrets when the file
does not exist. Run `./illo deploy init` only when you want to create or edit
that file before the first start.

Edit `deploy/compose/.env` only for server bootstrap settings such as generated
app secrets, database password, and the browser-facing `ILLO_PUBLIC_URL` if you
bring your own private network or reverse proxy. Do not put model provider API
keys in this file; add them from the Illospace System/Access screens after
first boot.

The app listens on server loopback. From your workstation:

```bash
ssh -L 8080:127.0.0.1:8080 <ssh-user>@<server>
```

Open `http://localhost:8080`, create the owner account, and add provider
credentials in System/Access.

Memory setup is also runtime-managed: choosing OpenAI/Gemini memory saves the
non-secret embedding settings in Postgres and encrypts the embedding API key
with `VAULT_MASTER_KEY`. The Compose app image and `/app/.env` are not mutated.

OpenAI/Codex sign-in uses OpenAI's registered localhost callback. In this
Compose deployment, the API runs inside a container, so the browser's
`localhost:1455` is your workstation, not the API container. Illospace therefore
shows the manual callback field by default: finish OpenAI sign-in, copy the
final `localhost:1455/auth/callback?...` URL from the sign-in tab, and paste it
back into Illospace.

For team-wide access, put your own reverse proxy, VPN, tunnel, or private
network in front of `127.0.0.1:8080`, then set `ILLO_PUBLIC_URL` to that
browser-facing URL.

## Local Image Fallback

Published images are the intended production path:

```text
ghcr.io/illospace/api:latest
ghcr.io/illospace/web:latest
```

Until those images are published for a release, build on the server:

```bash
./illo deploy up --build --no-pull
```

## Health Checks

The API and web entrypoint are exposed on loopback for operators:

```bash
curl http://127.0.0.1:8000/api/health/live
curl http://127.0.0.1:8000/api/health/ready
curl http://127.0.0.1:8080/api/health/live
```

The full deployment doctor is:

```bash
./illo deploy doctor
```

After an owner/admin has added provider credentials in the app, verify that the
running stack can see DB-backed credential records:

```bash
./illo deploy doctor --strict-credentials
```

### The healthy-but-inert state

An HTTP probe cannot tell you the stack is working. The `worker` service owns
both AgentRun execution and the cycle-scheduler thread, so if it alone is
missing every signal above still passes — `docker ps` is green, both health
endpoints return 200, the dashboard loads — while Illo does no work at all.
Presence has to be asserted directly:

```bash
./illo deploy inert-check
```

It exits `0` when every always-on service is running, `3` when the stack is up
but one is absent (inert), and `4` when nothing is running (down). The split
matters for an external watcher: `3` is the failure nothing else reports.
`./illo deploy doctor` runs the same assertion.

### Surviving a host reboot and runtime failures

Per-container restart policies are not enough on their own. They are a property
of a *container*, so anything that mutates or loses that property — an
interrupted worker swap, a container whose containerd shim died during host
shutdown — silently drops that service from the next boot. They also ignore
`depends_on`, so on boot the worker races a cold Postgres. Install the boot unit
so the whole project is reconciled from its declared spec instead:

```bash
./illo deploy boot-unit
```

The unit is generated per host rather than committed, because the Docker unit
name and binary path differ (a Docker snap install has no `docker.service` at
all). Inspect it before installing with `./illo deploy boot-unit --print`.

Install both the boot reconcile and the five-minute health watchdog with one
command:

```bash
deploy/scripts/install-boot-unit.sh && deploy/scripts/install-watchdog-unit.sh
```

The watchdog runs `inert-stack-check.sh`, reconciles a missing stack with
`docker compose up -d`, and restarts services that Docker marks unhealthy. It
does not recreate running services, and it takes no action while an update is
in flight. Its output is available in the systemd journal. Inspect the units
before installation with `deploy/scripts/install-watchdog-unit.sh --print`.

Both installers use **user** units by default when you are not root, so they
need no `sudo`. User units start at boot on their own provided the account has
lingering enabled (each installer turns it on when it can). A user unit cannot
order itself after a system Docker unit, so it waits for the daemon to accept
connections instead, bounded by `TimeoutStartSec`. Pass `--system` to install
into `/etc/systemd/system` and order directly after the Docker unit; that path
needs root.

Two worker settings interact with shutdown and must stay consistent:

- `stop_grace_period` (default `10s`, override with `ILLO_WORKER_STOP_GRACE_PERIOD`)
  must stay inside the Docker daemon's own shutdown budget — `dockerd
  --shutdown-timeout`, default 15s. Raising it past that reintroduces the bug
  where the worker is still draining when containerd is torn down, is recorded
  `Exited(255)` rather than stopped, and never comes back.
- `ILLO_AGENT_RUNNER_DRAIN_TIMEOUT_SECONDS` (default `infinity`) bounds the
  *deploy-time* drain, not shutdown. The graceful handoff signals with `docker
  kill -s TERM`, which ignores `stop_grace_period` entirely, and enforces its own
  bound via `COMPOSE_RUNTIME_WORKER_DRAIN_TIMEOUT_SECONDS`. When two consecutive
  canonical snapshots confirm zero active AgentRuns,
  `COMPOSE_RUNTIME_WORKER_IDLE_EXIT_TIMEOUT_SECONDS` (default `120`) bounds the
  remaining idle exit wait. Any later active AgentRun abandons that shorter
  deadline until two consecutive zero-active snapshots re-arm it.

## Operations

Back up Postgres:

```bash
./illo deploy backup
```

Restore a backup:

```bash
./illo deploy restore /path/to/illospace-YYYYMMDDTHHMMSSZ.dump
```

Upgrade:

```bash
./illo deploy upgrade
```

Build during upgrade when release images are unavailable:

```bash
./illo deploy upgrade --build --no-pull
```

Owners/admins can also start the same update flow from System/Update Illospace
or by asking Illo to update the deployment. In Compose, the API queues the
request in the private data volume and the `updater` sidecar runs
`./illo update --mode compose` from the host checkout. That syncs `origin/main`,
builds app images, runs migrations before runtime restart, and force-recreates
runtime services so rebuilt same-tag images are picked up. The sidecar mounts
the Docker socket, so keep this stack on trusted hosts only.

## Advanced Paths

Native virtualenv and systemd templates still live under `ops/` for operators
who need them. Prefer this Compose deployment for first-time team servers.

The default production image is optimized for `EMBEDDING_BACKEND=api`. Local
CPU/GPU embeddings require the optional packages in `requirements-gpu.txt` and
should be handled as an advanced deployment profile.
