# Meetings

Meetbot is an optional Illospace service that joins one Google Meet as a visible,
muted notetaker. It reads live captions. It does not record or process meeting
audio. The build contract and HTTP details remain in
[`specs/meetbot/README.md`](../specs/meetbot/README.md).

## Google account bootstrap

Create the Google account `illo@uwear.ai`. Keep it as a service identity. Do not
make the bot look like a teammate.

The bot needs an identity, not a mailbox, so give it a **Cloud Identity Free**
license instead of a paid Workspace seat. Order matters: on a Flexible plan a new
user takes a billable license the moment you create it. So add Cloud Identity Free
first, under Billing → Buy or upgrade → Cloud Identity. Then create an org unit for
bots and set the Workspace subscription to off for it, under Billing → License
settings. Create the user last, inside that org unit, and confirm on the user's
Licenses page that only Cloud Identity Free is assigned.

This also keeps the stored session credential cheap to lose: the account owns no
mail and no Drive, so `google-storage-state.json` grants an attacker nothing beyond
joining meetings. Never point this bot at a real staff account for the same reason.

Anonymous guest join needs no account at all. It works when a host admits the bot
by hand, and it is the correct fallback if Meet blocks the service identity.

On a local computer with a visible browser, run:

```bash
python -m meetbot.auth
```

Complete Google sign-in in the headed browser. This creates
`google-storage-state.json`. Copy that file to the `illo-dev` server at:

```text
/data/private/meetbot/google-storage-state.json
```

Protect this file as a credential. Do not add it to Git.

## Admission and consent

Invite `illo@uwear.ai` to each calendar event. An invited, signed-in account is
less likely to wait in the guest lobby. Google Meet and host policy still control
admission. If the bot stays in `lobby`, a host must admit it.

The participant name is visible to everyone. Keep the default name
`Illo (notetaker)` or use another clear bot name. The visible identity provides
the consent signal. Tell participants that live captions are used for notes when
local policy requires a separate notice.

## Provision the callback

Run the provisioning command in the Illospace runtime environment:

```bash
python -m brain.app.cli.meetbot_provision
```

The command reuses the active `meetbot` connection and prints a new bridge token
with only the `signal:submit` scope. Save `bridge_token` as
`ILLO_MEETBOT_BRIDGE_TOKEN` for the meetbot service.

Set these values in the deployment secret environment:

| Variable | Runtime | Value or default |
| --- | --- | --- |
| `ILLO_MEETBOT_URL` | API and worker | Set to `http://meetbot:8010`. Unset keeps meeting tools dormant. |
| `ILLO_MEETBOT_TOKEN` | API, worker, and meetbot | One shared random secret for meetbot HTTP requests. |
| `ILLO_MEETBOT_BRIDGE_TOKEN` | meetbot | Token printed by the provisioning command. |
| `ILLO_MEETBOT_CALLBACK_URL` | meetbot | Defaults to `http://api:8000`. |
| `ILLO_MEETBOT_DISPLAY_NAME` | meetbot | Defaults to `Illo (notetaker)`. |
| `ILLO_MEETBOT_MAX_SESSION_SECONDS` | meetbot | Defaults to `7200`. |

Start the optional Compose service with the meetbot profile:

```bash
docker compose --profile meetbot up -d
```

## Live verification

1. Create a short Google Meet and invite `illo@uwear.ai`.
2. In Slack, send Illo the Meet link and ask it to join.
3. Confirm that the tool reports `lobby`, `admitted`, or `captions_flowing`. A
   page load alone is not a successful join.
4. Admit the bot if needed. Speak while live captions are enabled. Confirm that
   the state becomes `captions_flowing` after a caption is observed.
5. End the call or ask Illo to leave.
6. Confirm that the originating Slack thread receives the summary, clarification
   questions, ticket plan, and filed-ticket follow-up. If no Slack origin was
   available, inspect the admitted AgentRun in the run inbox.

If the session reports `admitted` with a caption warning, check that Meet captions
are available for the bot account and meeting language. Do not treat this state as
a transcript success.
