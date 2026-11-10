# Slack channel

Install the app from `deploy/slack/manifest.yaml`. Invite it to each configured channel.
Set `ILLO_SLACK_BOT_TOKEN` and `ILLO_SLACK_APP_TOKEN` outside the checkout.
The app token needs `connections:write`. The manifest sets the bot scopes and events.
Socket Mode needs no public HTTP address.

Run `illo slack whoami` to read the bot identity.
Run `illo slack users` to read member IDs, names, and email addresses.
Copy the needed IDs into `people.yaml`.
Set the channel IDs and modes in `illo.yaml`.

The channel uses `ctx.slack_client`, the native async Slack client.
It sends gateway events and calls `ctx.slack_actions.dispatch` for known people.
Each area registers its own card handlers and checks who may use them.
The asks area owns card layouts and draft approval.
This channel registers draft performers for its two write tools.

Illo posts `Looking…` before it reads a clip, reads a thread, or calls the brain.
It edits that message with the answer. Long answers continue in the same thread.
It reads at most 20 messages of thread context, with their authors.
A transcript can wait for up to 15 seconds. A thread read can wait for up to 15 seconds.
Illo uses only Slack's transcript. It sends no clip to another service.
Private DMs never become search items.
Configured channel messages become team search items after the gateway handles them.

The Slack sensor records connection health every 10 seconds.
The SDK reconnects a lost socket. The channel also retries a failed initial connection.
Shutdown cancels pending work and closes the socket.

Tests use invented payloads and recording clients. They make no Slack calls.

```sh
scripts/test-db.sh
.venv/bin/pytest -q tests/slack tests/safety
.venv/bin/ruff check .
```
