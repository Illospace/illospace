# Illospace Compose Deployment

This is the canonical single-server production path for Illospace. It is meant
to be easy for a human operator or an LLM agent to run without inventing a
custom deployment.

## What It Runs

- `caddy`: public HTTP/HTTPS entrypoint with automatic TLS
- `web`: static SvelteKit dashboard
- `api`: FastAPI backend
- `worker`: standalone AgentRun worker
- `scheduler`: recurring job daemon
- `migrate`: one-shot Alembic migration job
- `postgres`: Postgres 16 with pgvector

Persistent data lives in Docker volumes. Server bootstrap secrets live in
`deploy/compose/.env`; model provider credentials are added inside Illospace
after boot and stored encrypted in Postgres.

## Fresh Server Setup

Prerequisites on the server:

- Linux VM with DNS pointed at the server
- Docker Engine with Docker Compose v2
- ports `80` and `443` open

From the repository root:

```bash
./illo deploy init --domain team.example.com --email admin@example.com
```

Edit `deploy/compose/.env` only for server bootstrap settings such as domain,
TLS email, generated app secrets, and database password. Do not put model
provider API keys in this file; add them from the Illospace System/Access
screens after first boot.

Start the stack:

```bash
./illo deploy up
```

Open the URL from `ILLO_PUBLIC_URL`.

## Local Image Fallback

Published images are the intended production path:

```text
ghcr.io/illospace/api:latest
ghcr.io/illospace/web:latest
```

Until those images are published for a release, build on the server:

```bash
./illo deploy build
./illo deploy up --no-pull
```

## Health Checks

The API is also exposed on loopback for operators:

```bash
curl http://127.0.0.1:8000/api/health/live
curl http://127.0.0.1:8000/api/health/ready
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

## Advanced Paths

Native virtualenv and systemd templates still live under `ops/` for operators
who need them. Prefer this Compose deployment for first-time team servers.

The default production image is optimized for `EMBEDDING_BACKEND=api`. Local
CPU/GPU embeddings require the optional packages in `requirements-gpu.txt` and
should be handled as an advanced deployment profile.
