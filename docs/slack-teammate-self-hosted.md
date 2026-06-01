# Self-Hosted Slack Teammate Setup

Illo's first Slack integration uses Socket Mode only. The self-hosted server
opens an outbound WebSocket to Slack, so operators do not need to expose a
public Slack Events API endpoint.

## Setup

1. Create a Slack app from `deploy/slack/illo-self-hosted-manifest.yml`.
2. Enable Socket Mode in Slack.
3. Install the app to your Slack workspace. Existing installations should be
   reinstalled after manifest scope changes so `chat:write.public` is granted.
4. Save `SLACK_BOT_TOKEN` to Illospace Vault, or set it as an environment
   variable for the connector process.
5. Save `SLACK_APP_TOKEN` to Illospace Vault, or set it as an environment
   variable for the connector process. This must be a Slack app-level Socket
   Mode token, usually prefixed `xapp-`; do not reuse the bot token here.
6. Start the connector process:

```bash
python -m brain.app.cli.slack_connector
```

For Docker Compose, run the optional Slack profile:

```bash
docker compose --profile slack up -d slack-connector
```

When using the Illospace server deployment, Illo can queue a restart for known
runtime services through the host controller after the Vault tokens are saved.

`ILLO_SLACK_ORG_ID` and `ILLO_SLACK_OWNER_USER_ID` are optional. If omitted,
the connector uses the first Illospace user it can find as the permissive
self-hosted authority for unmapped Slack users.

Optional Slack hints:

- `SLACK_TEAM_ID`: Slack workspace/team id.
- `SLACK_BOT_USER_ID`: Slack bot user id, used to detect direct mentions in
  raw message events.

## Runtime Behavior

- `app_mention` events and every DM are actionable.
- Top-level channel mentions reply as normal channel messages; mentions inside
  Slack threads reply back into that thread; DMs reply as normal DM messages.
- Illo sets a best-effort Slack working status while a Slack-origin run is
  being admitted, so teammates can see that the request was accepted. This uses
  the existing `chat:write` scope and Slack clears the status on reply or after
  its short timeout.
- Regular channel messages without an Illo mention are ignored.
- The default manifest subscribes to `app_mention` and `message.im` only. It
  keeps channel history scopes for context reads, but avoids generic channel
  message events as trigger sources because Slack can also deliver the same
  human mention as `app_mention`.
- Socket Mode envelopes are acknowledged before durable inbound processing.
- The connector records durable health while it connects. If the app-level
  Socket Mode token is wrong or Slack rejects the connection, `manage_slack`
  reports the connection as `error` instead of leaving it looking configured.
- Actionable Slack events are stored as inbound events with kind
  `slack_message`.
- Slack-origin runs receive normal Illospace tools plus:
  - `read_slack_conversation`
  - `post_slack_reply`
  - `manage_slack`

## Identity Mapping

Unmapped Slack users are allowed in the self-hosted MVP. Their runs use the
Slack connection owner as the authority user while preserving Slack provenance.

When identity matters, Illo can use `manage_slack` to inspect connection health
and link a Slack user id to an Illospace user id. Once linked, later Slack runs
from that Slack user are attributed to the mapped Illospace user.
