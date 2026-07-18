# Self-Hosted Slack Teammate Setup

Illo's first Slack integration uses Socket Mode only. The self-hosted server
opens an outbound WebSocket to Slack, so operators do not need to expose a
public Slack Events API endpoint.

## Setup

1. Create a Slack app from `deploy/slack/illo-self-hosted-manifest.yml`.
2. Enable Socket Mode in Slack.
3. Install the app to your Slack workspace. Existing installations should be
   reinstalled after manifest scope changes so `chat:write.public` and
   `reactions:write` are granted. Updating the manifest alone does not grant a
   new scope to an existing installation.
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
- Illo sets Slack's native assistant thread status to `is working on it...`
  after a mention or DM admits a fresh run. This is a transient loading
  indicator, not a chat reply. Slack clears it when Illo replies or after the
  platform timeout; Illo also clears it explicitly after posting a reply.
- Socket Mode envelopes are acknowledged before durable inbound processing.
  This is a transport acknowledgement only, not user-visible Illo speech.
- User-visible Slack text is model-authored. Illo decides whether to answer a
  simple request directly with `post_slack_reply`, or to make longer work
  durable with `spawn_worker`/`manage_idea` and then post a natural Slack update
  describing what it actually did.
- For a purely social acknowledgement, Illo may use
  `react_to_slack_message` instead of posting text. The tool is locked to the
  message that triggered the run and permits one emoji choice per run. It never
  replaces an answer, clarification, task update, or incident response. A
  failed attempt may retry the same emoji safely, but it cannot switch emoji
  and stack reactions.
- If Illo creates or selects a Cortex Thread, it should share only the
  `thread_url` returned by that tool. Slack ids and run ids are never converted
  into Thread URLs.
- When a Slack-origin run or non-headless Slack-origin child run reaches a
  terminal final answer, the runner posts that generated final answer back to
  Slack unless the same run already completed a successful `post_slack_reply`
  call or a successful `react_to_slack_message` call marked as a visible
  response. Failed reaction calls do not suppress the generated text fallback.
- Regular channel messages without an Illo mention are ignored.
- The default manifest subscribes to `app_mention` and `message.im` only. It
  keeps channel history scopes for context reads, but avoids generic channel
  message events as trigger sources because Slack can also deliver the same
  human mention as `app_mention`.
- The connector records durable health while it connects. If the app-level
  Socket Mode token is wrong or Slack rejects the connection, `manage_slack`
  reports the connection as `error` instead of leaving it looking configured.
- Actionable Slack events are stored as inbound events with kind
  `slack_message`.
- Slack-origin runs receive normal Illospace tools plus:
  - `read_slack_conversation`
  - `post_slack_reply`
  - `react_to_slack_message`
  - `manage_slack`

## Identity Mapping

Unmapped Slack users are allowed in the self-hosted MVP. Their runs use the
Slack connection owner as the authority user while preserving Slack provenance.

When identity matters, Illo can use `manage_slack` to inspect connection health
and link a Slack user id to an Illospace user id. Once linked, later Slack runs
from that Slack user are attributed to the mapped Illospace user.

`manage_slack(action="link_identity")` may also save an explicit Slack DM name
and bounded communication preferences: tone, brevity, humour, language, and
timezone. Illo uses these only after the Slack identity map and profile link
agree on the same Illospace user. It never substitutes the connection owner's
profile for an unmapped speaker, and it does not put the private DM name into a
shared-channel prompt.


## Channel inventory

Illo can list Slack conversations through `manage_slack(action="list_channels")`. The result includes Slack API inventory plus Slack-origin channels Illo has already observed from mentions or DMs. Slack API inventory is bounded by Slack itself: public channels require `channels:read`, private channels require `groups:read` and may only appear when the app/bot can see them, MPIMs require `mpim:read`, and DMs require `im:read`. Posting into public channels the bot has not joined requires `chat:write.public`; private channels still require inviting the bot, but an observed private channel can still expose the channel id needed for posting.
