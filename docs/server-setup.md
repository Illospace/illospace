# Server Setup

This is the LLM-friendly runbook for setting up Illospace for a team on one
Linux server.

## Ask The Operator First

Collect these values before touching the server:

- Domain name, for example `team.example.com`
- Admin email for TLS certificate notices
- Server SSH user and install directory
- Confirmation that the owner/admin will add model provider credentials inside
  Illospace after first boot
- Backup destination and retention expectation

## Target Server

Use the Compose deployment unless the operator explicitly asks for an advanced
native/systemd install.

Required server state:

- Linux host with Docker Engine and Docker Compose v2
- DNS `A` or `AAAA` record pointing the domain to this server
- inbound ports `80` and `443` open
- enough disk for Postgres volume data and backups

## Install Commands

Run these from the chosen install directory:

```bash
git clone https://github.com/Illospace/illospace.git
cd illospace
./illo deploy init --domain team.example.com --email admin@example.com
```

Edit `deploy/compose/.env`:

- replace `team.example.com` and `admin@example.com` if needed
- keep `SECRET_KEY`, `VAULT_MASTER_KEY`, and `DB_PASSWORD` private

Do not put model provider API keys in `deploy/compose/.env`. Production
provider credentials should be added from the Illospace System/Access screens
after first boot so they are encrypted with `VAULT_MASTER_KEY` and stored in
Postgres.

Start:

```bash
./illo deploy up
```

Expected result:

- `./illo deploy status` shows `postgres`, `api`, `worker`, `scheduler`, `web`,
  and `caddy` running
- `./illo deploy doctor` exits successfully
- `https://team.example.com` opens the dashboard
- an owner/admin can sign in and add provider credentials in System/Access

## Health Checks

Use the loopback API checks from the server:

```bash
curl http://127.0.0.1:8000/api/health/live
curl http://127.0.0.1:8000/api/health/ready
```

Use the deployment doctor:

```bash
./illo deploy doctor
```

Before sending a domain to users, add provider credentials in System/Access,
then run the stricter public check from the server:

```bash
./illo deploy doctor --strict-credentials --check-public-url
```

Use Caddy logs for public HTTP/TLS problems:

```bash
./illo deploy logs caddy
```

Use API logs for backend startup, migration, or database problems:

```bash
./illo deploy logs api
./illo deploy logs migrate
./illo deploy logs postgres
```

## Backups

Create an on-demand backup:

```bash
./illo deploy backup
```

The backup script writes a compressed custom-format Postgres dump under
`deploy/backups/` by default. Move those dumps to durable off-server storage.

Restore:

```bash
./illo deploy restore /path/to/illospace-YYYYMMDDTHHMMSSZ.dump
```

## Upgrades

For published release images:

```bash
./illo deploy upgrade
```

If release images are not available yet:

```bash
./illo deploy upgrade --build --no-pull
```

## Troubleshooting

If image pull fails, build local images with:

```bash
./illo deploy build
./illo deploy up --no-pull
```

If Caddy cannot get a certificate:

- confirm DNS points at the server
- confirm ports `80` and `443` are open
- check `./illo deploy logs caddy`

If readiness fails:

- check `./illo deploy logs migrate`
- check `./illo deploy logs api`
- run `./illo deploy doctor` and confirm the pgvector check passes

If agent runs do not execute:

- check `./illo deploy logs worker`
- confirm provider credentials have been added in System/Access
- run `./illo deploy doctor --strict-credentials` to verify DB-backed
  credential records are present
- keep `EMBEDDING_BACKEND=api` unless the server is intentionally configured
  for local CPU/GPU embeddings
- local CPU/GPU embeddings require an advanced image/runtime with
  `requirements-gpu.txt`

## Advanced Installs

The `ops/` directory still contains native virtualenv and systemd templates.
Treat that path as advanced. The Compose deployment is the default production
contract for team servers.
