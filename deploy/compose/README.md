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

Owners/admins can also start the same update flow from System/Update Illospace.
In Compose, the API queues the request in the private data volume and the
`updater` sidecar runs `./illo update --mode compose` from the host checkout.
The sidecar mounts the Docker socket, so keep this stack on trusted hosts only.

## Advanced Paths

Native virtualenv and systemd templates still live under `ops/` for operators
who need them. Prefer this Compose deployment for first-time team servers.

The default production image is optimized for `EMBEDDING_BACKEND=api`. Local
CPU/GPU embeddings require the optional packages in `requirements-gpu.txt` and
should be handled as an advanced deployment profile.
