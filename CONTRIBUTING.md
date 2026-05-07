# Contributing

Thanks for helping improve Illo Brain. This repo is an early open-source preview,
so small, well-scoped PRs are much easier to review than large rewrites.

## Local setup

```bash
./illo setup
./illo
```

`.env` is optional. Copy `.env.example` to `.env` only when you want local
file-based overrides such as provider keys or custom database settings.

For tests:

```bash
make test       # fast suite, excludes DB-required tests
make test-all   # full suite with Docker PostgreSQL + pgvector
```

## Pull request guidelines

- Keep changes focused and avoid unrelated refactors.
- Add or update tests for behavior changes.
- Redact provider keys, user data, thread transcripts, database rows, uploads, and logs.
- Document new environment variables in `.env.example` and user-facing behavior in `README.md` or `docs/`.
- Run the relevant backend/frontend checks before opening a PR.
- By contributing, you agree that your contribution is submitted under the Apache License, Version 2.0 unless you explicitly state otherwise.

## Runtime-private context

Personal agent context does not belong in public source control. Use
`ILLO_PRIVATE_HOME`, `AGENT_CONTEXT_DIR`, and `AGENT_CHECKLIST_PATH` for local prompts,
operator preferences, heartbeat notes, and generated checklists.
