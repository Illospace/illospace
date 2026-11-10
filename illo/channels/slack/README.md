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
A channel message from a person who is not in `people.yaml` starts no run.
A user of another workspace (Slack Connect) counts as unknown.
A bot, webhook, or workflow post starts a `tainted` run.
Each area registers its own card handlers and checks who may use them.
The asks area owns card layouts and draft approval.
This channel registers draft performers for its two write tools.

Illo posts `Looking…` before it reads a clip, reads a thread, or calls the brain.
It edits that message with the answer. Long answers continue in the same thread.
It reads at most 20 messages of thread context, with their authors.
An author who is not in `people.yaml` shows as `Outside user (<id>)`.
A bot or an unknown author in the context makes the run `tainted`.
A transcript can wait for up to 15 seconds. A thread read can wait for up to 15 seconds.
Illo uses only Slack's transcript. It sends no clip to another service.
Private DMs never become search items.
Messages of a configured public channel become team search items after the gateway handles them.
A private channel starts no run and enters no index.
A channel is private when Slack says so, when `illo.yaml` sets `visibility: private`, or when Slack does not answer.
`slack_read_thread` reads a private channel only for a member. It checks membership live on each call.

The Slack sensor records connection health every 10 seconds.
The SDK reconnects a lost socket. The channel also retries a failed initial connection.
Shutdown cancels pending work and closes the socket.

Tests use invented payloads and recording clients. They make no Slack calls.

```sh
scripts/test-db.sh
.venv/bin/pytest -q tests/slack tests/safety
.venv/bin/ruff check .
```
