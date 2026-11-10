# Contribute

Read [the architecture](docs/architecture.md) first. It is the contract: the words, the parts, the safety rules, the tables, and the tools. Then read [the builder guide](docs/builders.md).

## Run the tests

Use Python 3.12 or later and a local Docker daemon.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]' -e './brain[dev]'
scripts/test-db.sh
.venv/bin/ruff check .
.venv/bin/pytest
```

`scripts/test-db.sh` starts one shared Postgres container. Each test session creates and drops its own database. The container stays up.

A test never calls a real Slack, GitHub, mailbox, or model. Use the fakes in `tests/fakes.py`.

## Rules for a change

- **Stay in one area.** Each area has its own files, its own test folder, and its own block of migration numbers.
- **Never edit an old migration.** Add a new file in the number block of your area.
- **A safety rule needs a test.** Put it in `tests/safety/`, with the rule number in the file name.
- **A new tool states its risk and its profiles.** Give the profile `tainted` or `outsider` only to a tool that is safe when hostile text drives the run.
- **Keep examples fictional.** Use the team "Acme" and invented people. No real name, id, channel, address, or internal URL.
- **No secret in a file, a log, a test, or a command output.**

## Write for people

This covers the docs, the comments, the CLI messages, the card text, and the messages of Illo.

- Short sentences. One idea in each sentence.
- Active voice and the present tense.
- The same word for the same thing. Use the words of the architecture file.
- No filler, no metaphor, no marketing words.
- Keep the numbers, the names, and the paths.

The voice of Illo lives in `illo/brain/base_prompt.md` and `illo/brain/prompts/`. A change to that text changes the snapshots in `tests/prompts/snapshots/`. Update them with `ILLO_UPDATE_SNAPSHOTS=1 .venv/bin/pytest tests/prompts`, and read the diff.

## Commits and pull requests

- Use conventional commit messages, such as `feat(asks): send one reminder`.
- Each commit message ends with a `Co-authored-by:` line. The ruleset of `main` refuses a pull request with a commit that lacks it.
- Run Ruff and the full test suite before a pull request.
