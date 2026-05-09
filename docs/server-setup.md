# Server Setup

This is the LLM-friendly runbook for setting up Illospace for a team on one
Linux server.

## Ask The Operator First

Collect these values before touching the server:

- Server SSH user and install directory
- Browser-facing URL if the operator already has a private network, VPN,
  tunnel, or reverse proxy; otherwise use the default `http://localhost:8080`
- Confirmation that the owner/admin will add model provider credentials inside
  Illospace after first boot
- Backup destination and retention expectation

## Target Server

Use the Compose deployment unless the operator explicitly asks for an advanced
native/systemd install.

Required server state:

- Linux host with Docker Engine and Docker Compose v2
- SSH access
- enough disk for Postgres volume data and backups

## Install Commands

For the default one-command install, run these from the chosen install
directory:

```bash
git clone https://github.com/Illospace/illospace.git
cd illospace
./illo deploy up
```

`deploy up` creates `deploy/compose/.env` with generated secrets when the file
does not exist.

If you want to inspect or edit bootstrap settings before first start, use the
two-step form instead:

```bash
./illo deploy init
```

Edit `deploy/compose/.env` if needed, then run `./illo deploy up`:

- leave `ILLO_PUBLIC_URL=http://localhost:8080` for SSH-tunnel access, or set it
  to the operator's private network/reverse-proxy URL
- keep `SECRET_KEY`, `VAULT_MASTER_KEY`, and `DB_PASSWORD` private

Do not put model provider API keys in `deploy/compose/.env`. Production
provider credentials should be added from the Illospace System/Access screens
after first boot so they are encrypted with `VAULT_MASTER_KEY` and stored in
Postgres.

The memory setup screen follows the same rule. OpenAI/Gemini embedding choices
are saved as runtime DB settings, and embedding API keys are encrypted in
Postgres rather than written back to `/app/.env`.

## Private Access

The Compose stack does not install public TLS or domain ingress. The browser
entrypoint binds to server loopback at `127.0.0.1:8080`.

From your workstation, open an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 <ssh-user>@<server>
```

Then open `http://localhost:8080`, create the owner account, and add provider
credentials in System/Access.

For OpenAI/Codex sign-in, the manual callback fallback is expected in the
Compose path. OpenAI redirects to `localhost:1455` in your browser, but the API
container cannot receive that workstation-local callback. Copy the final
`localhost:1455/auth/callback?...` URL from the sign-in tab and paste it into
the callback field in Illospace.

For team-wide access, put your own reverse proxy, VPN, tunnel, or private
network in front of `127.0.0.1:8080`, then update `ILLO_PUBLIC_URL` if browser
users will not use `http://localhost:8080`.

Expected result:

- `./illo deploy status` shows `postgres`, `api`, `worker`, `scheduler`, and
  `web` running
- `./illo deploy doctor` exits successfully
- `http://localhost:8080` opens the dashboard through the SSH tunnel
- an owner/admin can sign in and add provider credentials in System/Access

## Health Checks

Use the loopback API checks from the server:

```bash
curl http://127.0.0.1:8000/api/health/live
curl http://127.0.0.1:8000/api/health/ready
curl http://127.0.0.1:8080/api/health/live
```

Use the deployment doctor:

```bash
./illo deploy doctor
```

After adding provider credentials in System/Access, run the stricter checks
from the server:

```bash
./illo deploy doctor --strict-credentials --check-app-url
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
./illo deploy up --build --no-pull
```

If the SSH tunnel does not open the app:

- confirm `./illo deploy status` shows `web` and `api` running
- confirm the tunnel points to `127.0.0.1:8080` on the server
- check `./illo deploy logs web`

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
