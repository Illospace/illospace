# Illospace

Illospace is an open-source workspace where humans and AI agents can work
together in teams. It brings shared memory, skills, vault-backed secrets,
Cortex thought threads, browser/tool execution, and operational dashboards into
one place for collaborative agent work.

> Status: early/open-source preview. The project is actively changing and is not
> yet a hosted product or stable library API. Contributions, issues, and design
> feedback are welcome.

## Why Illospace?

AI agents are most useful when they can participate in the same context as the
people guiding them: shared projects, durable memory, team permissions, visible
runs, and tools that make collaboration inspectable. Illospace is a foundation
for that kind of human-agent teamwork.

## What is in this repo?

- **FastAPI backend** for teams, Cortex threads, memory, skills, vault, projects, browser sessions, and agent runs.
- **PostgreSQL + pgvector** storage for durable memory and workspace state.
- **SvelteKit frontend** for the shared workspace and operational dashboards.
- **Agent runtime tooling** for model invocation, workspace tools, skill bundles, and recurring cycles.
- **Optional local embedding/GPU workers** for lower-latency semantic retrieval.

## Local setup

Use this path when you want to run Illospace on your own machine for local
development, evaluation, or a personal preview.

Prerequisites:

- Python 3.11+
- Node.js 22+
- one local database path: Docker, Podman, or PostgreSQL 16+ server tools with
  pgvector

Single-host Linux install. If no database is configured, the installer uses
Docker/Podman to create a local pgvector container:

```bash
curl -fsSL https://illospace.com/install.sh | bash
```

Local development checkout:

```bash
git clone https://github.com/Illospace/illospace.git
cd illospace
./illo
```

For a first install, `./illo` is the command to run. It performs setup when
needed, prepares local secrets, syncs dependencies, starts a local pgvector
database when needed, builds the frontend, and then starts the native server:

```text
App: http://localhost:8000  (API docs at /api/docs)
```

For frontend hot reload while developing, run `./illo dev`.

After the app opens, add model/provider credentials from the System or
onboarding screens. Illospace can boot without provider keys, but LLM-backed
agent work needs at least one provider key, local model, or database-backed
credential.

Useful local commands:

```bash
./illo doctor     # Diagnose local setup and configuration
./illo setup      # Prepare dependencies and database without starting the app
./illo test       # Run the fast test suite
./illo uninstall  # Remove local runtime/config/local DB and reset next setup
```

The launcher prefers a Docker/Podman pgvector container for local storage, but
can also manage a repo-local PostgreSQL runtime under `.runtime/postgres` when
server tools and pgvector are installed. If another local Postgres already owns
`5432` and you have not pinned `DB_PORT`, it can choose an alternate port
automatically.

## Team server deployment

The recommended open-source team-server path is Docker Compose on a single
Linux VM with Postgres + pgvector, the FastAPI API, the private web entrypoint,
the AgentRun worker, and the scheduler. It does not install public TLS or
domain ingress for you.

```bash
git clone https://github.com/Illospace/illospace.git
cd illospace
./illo deploy up
```

The web entrypoint binds to server loopback. Access it over SSH:

```bash
ssh -L 8080:127.0.0.1:8080 <ssh-user>@<server>
# open http://localhost:8080 and create the owner
```

For team-wide access, bring your own reverse proxy, VPN, tunnel, or private
network in front of `127.0.0.1:8080`.

See [docs/server-setup.md](docs/server-setup.md) for the exact server runbook,
backups, restore, upgrades, and troubleshooting.

## Which command should I run?

Use `./illo` for a new checkout, native server runs, and the first self-hosted
preview. It owns the native runtime, auto-runs setup when needed, builds the
frontend, starts the API/dashboard on `localhost:8000`, and keeps AgentRuns
self-contained.

Native mode binds to `127.0.0.1` by default; set `ILLO_API_HOST=0.0.0.0` only
when you intentionally want direct network access and have your own firewall,
tunnel, or reverse proxy in front of it.

Use `./illo dev` when you are editing the frontend or backend locally and want
the Vite dashboard on `localhost:5173`.

Use `./illo setup` only when you want to install/sync dependencies and prepare
the database without starting the app.

Use `./illo deploy` for team servers. It manages the recommended Docker Compose
deployment, including secret initialization, startup, doctor checks, backups,
restore, upgrades, logs, and status.

## Configuration and secrets

- `.env` is optional. The app reads real environment variables first and only
  loads `.env` when the file exists.
- In production, add model/provider credentials from Illospace System/Access so
  they are encrypted and stored in Postgres. Environment provider keys remain a
  development fallback, not the recommended self-hosted server path.
- Memory provider settings are saved as runtime DB settings. Embedding API keys
  are encrypted with `VAULT_MASTER_KEY`; the app does not rewrite `.env` after
  first boot.
- `./illo setup` creates ignored checkout-local defaults for `SECRET_KEY` and
  `VAULT_MASTER_KEY` in `.illo/runtime.env` when they are not provided, so a
  self-hosted preview can boot cleanly.
- Codex sign-in uses OpenAI's localhost callback. On a remote/self-hosted
  server or Docker Compose install, Illospace opens the manual fallback
  automatically: finish sign-in, copy the final
  `localhost:1455/auth/callback?...` URL from the sign-in tab, and paste it
  back into Illospace. Custom server callbacks are opt-in with
  `ILLO_OPENAI_OAUTH_SERVER_CALLBACK=1` only for OAuth clients that accept your
  deployed callback URL.
- For local file-based overrides, copy `.env.example` to `.env` and fill in the
  values you need. For production, prefer your platform's secret manager or an
  external environment file such as `~/.config/illospace/production.env`.
- Run `./illo doctor --production` before starting a production deployment.
- Never commit `.env`, provider keys, database dumps, uploads, or generated journals.
- Runtime-private state defaults to `.illo/` via `ILLO_PRIVATE_HOME` and is ignored by git.
- Personalized operator prompt/context files should live under `.illo/agent-context/`
  (or another private `AGENT_CONTEXT_DIR`), not in the public repo root.
  Illo's editable personality file defaults to `.illo/agent-context/SOUL.md`
  and can be overridden with `AGENT_SOUL_PATH`.

See [docs/configuration.md](docs/configuration.md) for the production
configuration contract.

Illo runtime liveness is monitored outside the host by a scheduled GitHub
Actions deadman. Its independent credentials, public-data boundary, and
operator test procedure live in [docs/deployment.md](docs/deployment.md#external-illo-deadman).

## Voice dictation

Composer voice input supports two transcription providers, selectable in
System → Voice:

- **OpenAI Realtime** (default) — low-latency streaming transcription using the
  OpenAI API key saved in AI Runtime. Words appear as you speak.
- **Local (faster-whisper)** — on-device CPU transcription with no API key.
  Push-to-talk: the recorded clip is transcribed when you stop. Choose a model
  size (`tiny` / `base` / `small`, default `base`); weights download on first
  use into `ILLO_PRIVATE_HOME/voice-models`. `faster-whisper` ships in the
  default image and pulls no Torch/CUDA, so the provider works out of the box;
  if it is ever absent the provider reports as unavailable and OpenAI dictation
  is unaffected.

## Development commands

```bash
./illo              # Recommended for new users; auto-setup, then local preview
./illo dev          # Development mode with frontend hot reload
./illo setup        # Setup only
./illo deploy up    # Initialize/start the Compose team-server deploy and run doctor
./illo doctor       # Diagnose config and common setup issues
./illo uninstall    # Remove local runtime/config/local DB and reset next setup
./illo test         # Fast tests
make test           # Fast pytest selection
make test-all       # Full DB-backed suite via Docker
```

## Project structure

```text
brain/app/              FastAPI app, CLI, scheduler, hooks, MCP, and web adapters
brain/jobs/             Offline pipelines, evals, and recurring job code
brain/kernel/           Shared config, runtime primitives, and common helpers
brain/platform/         Database, provider, browser, GPU, and telemetry adapters
brain/systems/          Cortex, memory, skills, runs, vault, domains, and apps
frontend/               SvelteKit dashboard
tests/                  Pytest suite and regression fixtures
ops/                    Local setup, self-hosting templates, and test DB helpers
docs/                   Public architecture, setup, security, and extension notes
```

For a deeper map, see [docs/architecture.md](docs/architecture.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please open issues/PRs with clear repro steps,
logs with secrets removed, and tests where practical.

## Security

Please do not open public issues for vulnerabilities or leaked secrets. See
[SECURITY.md](SECURITY.md) for the reporting process.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md).
