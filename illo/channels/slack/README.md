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
A pending Slack transcript can wait for up to 15 seconds. A thread read can wait for up to 15 seconds.
A missing transcript or `none` status fails at once by default.
Illo sends audio to the transcription API only with `voice.transcription: openai`.
See [voice setup](../../../docs/workspace.md#voice-clips) for limits and privacy checks.
Illo reads still images from known active people in safe DMs and team runs.
It reads the run message first, then retained context, newest first.
It supports PNG, JPEG, GIF, and WebP. It reads no PDF, video, or SVG files.
The defaults allow four images of at most 8 MB each, with 20 seconds for all image reads.
Set `slack.images: off` to stop image downloads. You can lower the count and size limits.
Illo records file IDs, types, sizes, person keys, and skip counts.
It keeps no image bytes, private Slack URLs, or bot tokens in the ledger or logs.
The model provider receives the images. A failed image read does not stop the answer.
Only `openai_responses` shows images. Other runtimes get a text notice.
See [image setup](../../../docs/workspace.md#slack-images) for settings and privacy checks.
Private DMs never become search items.
Messages of a configured public channel become team search items after the gateway handles them.
Illo reads message text, Block Kit text, button links, and legacy attachments in display order.
The event, search item, and thread readers use the same text reader.
It keeps Slack escapes and skips unknown elements. It cuts text at 8,000 characters with a clear marker.
It reads at most 1,000 elements and 12 nested levels. Bot text keeps its `tainted` status.
Sign-in commands and card answers use only the person's typed text or verified voice transcript.
A channel of class `members` answers known active people after a mention. It enters no shared index.
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
