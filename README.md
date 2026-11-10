# Illo

Illo is one assistant for a whole team. People talk to it in Slack, and later by email. It knows each lane of agent work on each machine. It answers "give me an update on X". It sends a person a card only for a real blocker, and it sends the answer back to the agent. It carries requests between people. It starts clear work through a GitHub ticket. It finds the team context where that context lives, and it keeps the decisions.

Status: **rebuild in progress**. Read [the architecture](docs/architecture.md).
The channel connections and the brain sandbox are not ready yet.

Use Python 3.12 or later. Run the tests with a local Docker daemon:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
scripts/test-db.sh
.venv/bin/ruff check .
.venv/bin/pytest
```

Each test session creates and drops its own database. The shared container stays up.
See [the workspace keys](docs/workspace.md) and [the environment file](deploy/env.example).
