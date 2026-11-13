# Workspace files

Teach Illo through chat with [Team memory](memory.md). You can still edit these files by hand.

`ILLO_WORKSPACE_DIR` points to the folder. Start with `workspace.example/`.
The loader rejects unknown fields. Errors name the file and field.
A failed reload keeps the last valid workspace. The loader syncs people in one transaction.
Deleted people become inactive. Their old records stay for ledger references.
Do not store secrets in this folder. Use the vault or boot environment.

## illo.yaml

| Key | Meaning |
| --- | --- |
| `team_name` | Team name. Required. |
| `language` | Optional team language for the team brief, for example `French`. Default: the language of the run message. |
| `timezone` | IANA time zone for schedules. Default: `UTC`. |
| `default_owner` | Active person key from `people.yaml`. Required. |
| `slack.default_mode` | `listen` (default) or `off`. The mode for channels with no entry. |
| `slack.images` | `on` (default) or `off`. Allow still images in safe person and team runs. |
| `slack.max_images` | Maximum images per run. Default and upper limit: 4. Minimum: 1. |
| `slack.max_image_bytes` | Maximum bytes per file. Default and upper limit: 8388608 (8 MB). Minimum: 1. |
| `slack.default_rule_files` | Rule file paths for channels with no entry. Default: empty. Paths must name loaded rules. |
| `slack.channels` | Optional channel overrides. List a channel for coordinator mode, its own rules, or `mode: off`. |
| `slack.channels[].name` | Display name. |
| `slack.channels[].id` | Unique Slack channel ID. |
| `slack.channels[].mode` | `coordinator`, `listen`, or `off`. Default: `listen`. |
| `slack.channels[].visibility` | `team` or `private`. Default: `team`. Illo also asks Slack. A private channel answers known active members. Only class `team` enters shared search. |
| `slack.channels[].rule_files` | Rule paths relative to the workspace. Default: all matching rules. |
| `github.repositories` | List of `owner/repository` names to follow. Default: empty. |
| `digest.schedule` | Five-field cron expression in each person's time zone. Default: `0 9 * * 1-5`. |
| `digest.personal` | `always` (default) sends each morning. `needs` sends only when work waits or a fault exists. |
| `digest.team.channel` | Optional team brief. Use the name of an enabled channel with `visibility: team`. |
| `digest.team.send_empty` | Send an empty team brief. Default: `false`. |
| `digest.team.schedule` | Five-field cron expression in the team time zone. Default: `0 9 * * 1-5`. |
| `digest.destination` | Legacy destination setting. Required. The morning brief goes to each active person. |
| `stale_minutes` | Limits for raw lane reads, in minutes. The briefs do not use these limits. |
| `stale_minutes.t3` | Default: 30. |
| `stale_minutes.claude-code` | Default: 30. |
| `stale_minutes.codex` | Default: 30. |
| `stale_minutes.github` | Default: 1440. |
| `stale_minutes.manual` | Default: 1440. |
| `stale_minutes.email` | Default: 1440. |
| `brain.runtime` | Runtime sent to the brain service. Default: `claude`. |
| `brain.model` | Model sent to the brain service. Default: `default`. |
| `brain.auth` | `api_key` (default) or `chatgpt`. ChatGPT needs `openai_responses`. |
| `brain.team_credential` | Active person key for team runs and person runs in `team` mode. Required with `auth: chatgpt`. |
| `brain.person_credential` | `own` (default) or `team`. `team` uses `team_credential` for untainted person runs and needs `auth: chatgpt`. |
| `brain.tainted_credential` | `none` (default), `team`, or `api_key`. Select the model credential for tainted and outsider runs. |
| `brain.api_key_fallback` | Allow the brain's API key when the selected grant is missing, broken, or at its limit. Default: `false`. |
| `brain.effort` | Optional profile-to-effort map: `low`, `medium`, or `high`. Person DM, team channel, and agent request default to `medium`; others default to `low`. This setting wins over `ILLO_BRAIN_EFFORT`. |
| `brain.max_turns` | Tool-call limit for each run. Default: 12. Range: 1–50, the limit of the brain service. |
| `brain.timeout_s` | Run timeout in seconds. Default: 300. Maximum: 3600. |
| `workspace.git_push` | Commit and push confirmed notes. Push connector commits too. Default: false. |
| `memory.list_changes` | `owners` (default) or `everyone`. Controls who creates lists and changes holders or Done rules. |
| `connect.public_url` | Optional HTTPS browser address for `/oauth/` and `/list`. No credentials or query. Default: `ILLO_PUBLIC_URL`. |
| `run_limits.max_concurrent` | Maximum active runs. Default: 4. Range: 1–100. |
| `run_limits.max_message_chars` | Maximum inbound text length. Default: 20000. |

`coordinator` can start a run for a channel message. `listen` starts a run only
for an explicit mention, or a reply in a thread where Illo answered. `off` ignores messages.
A channel adapter supplies mention and bot metadata from the channel API.
A channel needs no entry. Add Illo to the channel in Slack, then mention it.
Only a known active person can start a run in a channel with no entry.
Webhook, bot, and form text uses the `tainted` profile only in listed team channels.
`slack.default_mode: off` accepts only listed channels.
A listed channel uses its own settings and rule files.
A message from a person who is not in `people.yaml` starts no run.
No Slack token is required to load these files or start the HTTP app.

To use subscriptions, set these keys in `illo.yaml`:

```yaml
brain:
  runtime: openai_responses
  model: gpt-6.1-sol
  auth: chatgpt
  team_credential: ada
  person_credential: own
  tainted_credential: none
  api_key_fallback: false
```

Use an active person key from your own `people.yaml` for `team_credential`.
If `ILLO_BRAIN_RUNTIME` is set, set it to `openai_responses` too. Environment overrides still take priority.
The app refuses startup or reload if the effective runtime cannot use ChatGPT.
With the default `person_credential: own`, each person sends `connect my ChatGPT` in their own Slack DM with Illo.
The DM gives a device code. ChatGPT sign-in needs no public Illo address.
Send `connect my ChatGPT with a link` to use the OpenAI link and paste the final local address.
In `own` mode, person DMs, agent requests, person routines, and morning briefs use that person's grant.
Team channels and team routines use `team_credential`. They share that person's ChatGPT limit.
The workspace owner can explicitly set `person_credential: team` to pay for all untainted runs with that sign-in.
This choice changes only who pays for the model. Each run keeps the requester's authority and personal data scope.
The requester's messages then pass through the ChatGPT account of the person in `team_credential`.
Only that person needs to send `connect my ChatGPT` in a DM with Illo.
Other members receive a reply that names the sign-in and says they do not need to connect.
`ChatGPT status` names the shared sign-in and shows its state to any member.
If that person sends `disconnect my ChatGPT`, Illo warns that all members lose the model sign-in.
Other members can still disconnect their own unused grant. This does not disconnect the team grant.
Tainted runs, signals, and outsiders get no model run by default, even with API fallback.
Set `tainted_credential: team` to share the team credential and its limit.
Set `tainted_credential: api_key` to use the API key for those runs.
The default owner gets at most one notice per hour when Illo skips tainted runs.
A missing personal grant never selects another person's grant in the default `own` mode.
In `team` mode, a missing or broken team grant gives a reply that names the person who must repair it.
The main app refreshes a token when it expires within five minutes.
No grant means no run, unless `api_key_fallback` is `true`.
A team run with no grant records a sensor error. The default owner gets at most one notice per hour.
This notice also covers person runs in `team` mode. It goes to `default_owner`, even if the payer is another person.
The notice is not sent when the API-key fallback supplies a credential.
For a subscription limit, an allowed API retry resumes the current turn with its existing tool results.
It does not repeat completed tool calls. The final answer says that the API key was used.
The API key must exist in the brain's environment for this retry to work.
Check that the provider's terms allow this use. An API key is the alternative.

## Check the ChatGPT connection

`illo chatgpt status` prints the person credential mode and team person key on one line, then the grant states.
For `openai_responses`, `runs.usage` records `auth_kind`, also when a run fails.
When the run uses a ChatGPT grant, it also records the payer's key as `credential_person`.
An API-key fallback records `auth_kind: api_key` and has no `credential_person`.

After sign-in, run this command in the main app environment:

```sh
illo chatgpt smoke --person ada
```

`--person` accepts an active person key or database ID.
The command resolves that person's grant with the normal credential rules.
The main app refreshes it when needed. It sends `provider_auth` to the brain for this check.
The brain offers only `smoke_ping`, a test function with no team data or side effects.
The check makes one tool call, then sends its result back to the model.
It uses the configured model and can spend subscription usage or API credits.
It prints the auth kind, model, tool result, token usage, and elapsed time.
It never prints a token, account ID, prompt, or model answer.
A failure prints the HTTP status when available and a short reason. It exits with status 1.
With `brain.api_key_fallback: false`, a missing grant or subscription limit fails without an API retry.
The brain service must run the same version as the main app.

Example output with a fake provider:

```text
Auth kind: chatgpt
Model: test-model
Tool round trip: yes
Token usage: input_tokens=4 output_tokens=2 total_tokens=6
Latency: 0.025 s
```

## Voice clips

The default mode uses only Slack's transcript. No audio leaves Slack.
Slack does not transcribe clips in every workspace.
A clip with no Slack transcript fails at once. Only `pending` or `processing` waits, for at most 15 seconds.

To let Illo transcribe a clip when Slack has no transcript, add this block to `illo.yaml`:

```yaml
voice:
  transcription: openai
  model: gpt-4o-mini-transcribe
  max_duration_s: 120
  max_bytes: 10485760
  timeout_s: 30
  max_per_person_per_hour: 20
```

| Key | Default | Meaning |
| --- | --- | --- |
| `voice.transcription` | `slack` | `slack` keeps audio in Slack. `openai` allows the fallback. |
| `voice.model` | `gpt-4o-mini-transcribe` | Model ID for the OpenAI transcription endpoint. |
| `voice.max_duration_s` | `120` | Maximum duration in seconds. Must be positive; maximum 3600. |
| `voice.max_bytes` | `10485760` | Maximum audio size in bytes (10 MiB). Maximum 25 MiB. |
| `voice.timeout_s` | `30` | Total deadline for each fallback, including download and transcription. Maximum 300 seconds. |
| `voice.max_per_person_per_hour` | `20` | Maximum fallback attempts for each person in one hour. Maximum 1000. |

Set `OPENAI_API_KEY` in the main app's boot environment, outside the workspace.
Startup and reload reject `openai` mode without the key. A failed reload keeps the last valid workspace.
`OPENAI_BASE_URL` is optional. A blank value uses `https://api.openai.com/v1`.
A custom base URL must use HTTPS. It selects the service that receives the audio and API key.
Voice uses the API key even when the brain uses a ChatGPT subscription.
It does not use or change the person's ChatGPT grant.

Slack's transcript always wins. The fallback accepts only audio from a verified, active team member.
The file must belong to that Slack user. Slack must report its duration and size within the limits.
Bot, webhook, outside workspace, and tainted events cannot use the fallback.
Illo also checks thread authors before it sends audio out.
Illo gets the download URL from `files.info` for that file ID.
It accepts only `https://files.slack.com/` and refuses redirects.
It checks the byte limit during download. It deletes its temporary audio file after each attempt.
The per-person limit survives restarts. Failed download or API attempts count toward the limit.
The event records `clip_transcribed_by: openai`, `clip_transcription_count`, and total `clip_duration_ms` without audio content.
An audit row records each admitted attempt. It contains no transcript or credentials.
The transcript then follows the same path as the person's typed text, including answers to ask cards.

Model and endpoint references: [OpenAI speech to text](https://developers.openai.com/api/docs/guides/speech-to-text)
and [GPT-4o Mini Transcribe](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe).

## Person language

| Key | Meaning |
| --- | --- |
| `language` | Optional. The language of the morning brief and of routine messages to this person, for example `French`. Default: the language of the run message. |

## Other files

- `people.yaml`: a mapping with `people`, a list of people. Each person has `key` and `name`.
  Optional fields: `slack_id`, `github_login`, `emails`, `role`, `timezone`, `language`, `areas`, `active`.
  The role is `owner` or `member`. The default role is `member`. The default time zone is `UTC`.
  The default `active` is true. All identities must be unique, including inactive entries.
- `rules/*.md`: YAML front matter with `name`, optional `description`, `channels`, `profiles`, and `principals`.
  Profile names: `person_dm`, `team_channel`, `tainted`, `routine`, `agent_request`, `digest`, and `team_digest`.
  Empty filters match all runs. Briefs get only rules that name `digest` or `team_digest`. The Markdown body supplies the rule text.
- `skills/<name>/SKILL.md`: front matter with `name` and `description`. The body holds the steps.
- `routines/*.md`: front matter with `name`, `schedule`, `destination`, and `principal`.
  The principal is `team` or `person:<key>`. The body is the prompt.
- `connectors/*.yaml`: `key`, `kind`, `scope`, `url`, and optional `tools`.
  The kind is `mcp`, `google`, or `github`. The scope is `team` or `personal`.
  Each tool has a `risk`: `read`, `write_internal`, or `send_outside`. A `read` tool can set `tainted: true`.
  Illo loads a personal grant for the person of the run. The files contain no grants or secrets.
  See [connectors.md](connectors.md).
- `notes/*.md`: team notes with front matter: `title`, `about`, `author`, `created`, and `visibility: team`.
  Plain Markdown notes still load. See [Search, archive import, and notes](search-notes.md).

The loader checks files every `ILLO_WORKSPACE_CHECK_SECONDS` seconds.
`illo reload` requests an immediate reload. It needs `ILLO_RELOAD_TOKEN` and a loopback URL.
File paths in channel rules must match a loaded rule. Destinations must name a configured person or a member channel.


## Morning brief and routine time zones

The personal brief sends at most once per person per local day.
With `digest.personal: always`, it sends each scheduled morning.
With `digest.personal: needs`, it sends only for a task in Needs you, an open ask, or an owner service fault.
Finished work alone does not send a DM in `needs` mode. Illo records a skipped day.
A skipped day keeps the time of the last sent brief for Done and restart counts.
The first brief also explains how to change the time or turn it off.

Each person can change their brief in their own DM:

- "Send my brief at 8:30" sets the local time. Use five-minute steps.
- "Stop the morning brief" turns it off.
- "Turn my brief back on" turns it on.
- "When do you send my brief?" reads the current settings.

The `brief_settings` tool acts only for the person of the DM run.
Tainted runs and agent requests cannot use it.
Illo stores these settings in `brief_preferences`. A workspace reload keeps them.
A time override keeps the configured days. A time change cannot send twice on the same local day.
When no override exists, Illo uses the workspace schedule.
It uses the person's `timezone` in `people.yaml` and `digest.schedule` in `illo.yaml`.
A time zone change takes effect after reload. It does not repeat a brief on the same local day.

`routines/digest.md` supplies the model instructions for this brief. Front matter is optional.
Illo uses only the body. The name, schedule, principal, and destination in old front matter have no effect.
A routine named `daily-digest` also supplies instructions when `routines/digest.md` is absent.
The scheduler never runs either file as a separate routine. The built-in brief owns delivery.
Use `{name}` for the person's name. Without either file, Illo uses its default instructions.

The run uses the same personal grant and model credential rules as a person routine.
It can call only `digest_snapshot`. That tool returns the snapshot that the brief starts with.
It cannot send messages or change data. The gateway captures the final reply.
The digest sends that reply with its normal daily delivery key.
A model failure, timeout, or missing credential gives a plain brief from the same snapshot.
The brief suppresses a separate ChatGPT sign-in notice. Other runs keep their normal sign-in notices.
A failed send keeps the same delivery key for a retry.

In a DM, `digest_snapshot` and the older alias `asks_digest` return the same personal data.
The task list supplies all work facts. Lanes supply run evidence, not a second picture of work.

The personal sections appear in this order:

1. **Needs you:** open tasks whose `waits_for` names the person.
   Each task has its ID, title, reason, wait time, and Slack thread link when present.
   Overdue tasks come first. Within each group, the oldest wait comes first.
   The snapshot shows seven tasks, then the count of the rest.
2. **Your agents:** held task counts, silent tasks, and automatic restarts since the last brief.
   Silence means more than half the list's `silence.agent_hours`.
3. **Done since the last brief:** the count and at most five titles, with proof and who set Done.
   This includes tasks the person creates, reviews, or hands over, and tasks held by their agents.
4. **Due:** tasks the person holds or reviews, overdue or due within 24 hours, outside Needs you.

Open asks and failed answer deliveries still appear. Only a workspace owner gets service faults.
A private task appears only in its owner's brief.
Titles and questions stay inside outside-data markers in the model's input.
The fallback text uses the same sections and limits.
Both snapshots include `recent_openings`: the first line of the last five completed briefs, newest first.
Each line has at most 160 characters. Personal openings belong only to that person.
The event stores these openings with the rest of the snapshot before the model runs.

Event sensors can stay idle without a fault. A sensor error after the last success is a fault.
Timer sensors record `expected_every_s` when they report.
A timer sensor becomes silent after three intervals, with a minimum of 30 minutes.
Scheduled jobs record their actual interval. Collector reports default to 300 seconds and can set another interval.
After an upgrade, a timer records its interval on its next report.
The snapshot keeps raw sensor keys for operators. The brief uses plain fault sentences.

To add a team brief, configure one team-visible channel by name:

```yaml
digest:
  schedule: '0 9 * * 1-5'
  destination: person:ada
  personal: always
  team:
    channel: software
    schedule: '0 9 * * 1-5'
    send_empty: false
```

Add Illo to `software` in Slack. The channel must have class `team` and a mode other than `off`.
An absent `digest.team` block disables the team brief. Illo checks an unlisted channel in Slack at run time.
A failed reload keeps the last valid workspace.
The team brief runs in the team time zone, at most once per local day.
With `send_empty: false`, it sends nothing when all four task sections are empty.
Illo records a skipped day. Held tasks alone cause an In work line.

`routines/team-digest.md` supplies the instructions. Front matter is optional; Illo uses only the body.
Use `{team}` for the team name. Without that file, Illo uses the default instructions.
The scheduler never runs this instruction file as a separate routine.
The run has principal `team`, profile `routine`, and the normal team model credential.
It can call only `team_snapshot`. It has no personal connector access and cannot send through a tool.
The event stores the snapshot so a delivery retry uses the same data.
A model failure or timeout sends a plain brief from that snapshot.
The normal channel path converts links to Slack format and splits long text into thread replies.
Each part has a stable delivery key.

The team snapshot reads only team-visible tasks. It excludes private tasks, asks, and service faults.
Its sections appear in this order:

1. **Done:** the count and at most seven titles, with each holder and proof.
2. **Waiting on people:** each person's task count, oldest wait, and single oldest task.
3. **Stuck:** a final failed wake, silence after the restart limit, no holder for more than 24 hours, or an overdue task.
   Each item names the fact and who must act. A quiet ticket alone is not stuck.
4. **In work:** one line with how many tasks agents hold and how many people hold.

The first brief uses 24 hours. Later briefs use the separate `digest-team` state.
The text uses neutral facts about tasks. It does not judge a person's work.
No link is invented when a task has no Slack thread.

A known team member can reply in the team brief's thread without a mention for 24 hours.
This starts a normal `team_channel` run with the thread as context.
After 24 hours, a `listen` channel needs a mention again.
Bot and outside text keep the normal taint checks. An `off` or private channel starts no run.

Other routines use the destination person's time zone for a person destination.
A person principal with no channel destination also uses that person's time zone.
A channel destination uses the team time zone. This rule takes priority over the principal.
Reload recalculates the next run after a time zone change.
It suppresses another person run on the last run's local day after that change.

## Triage of outside text

`triage.auto_assign.slack_bots` lists trusted Slack monitor `bot_id` values.
Each id must be a non-empty string without spaces.
`triage.auto_assign.signal_sources` lists trusted machine signal source names.
Both lists default to empty. `triage.max_per_hour` defaults to 6 and accepts 1–100.
See [GitHub triage](github.md#triage-of-outside-text) for the assignment and card rules.

`slack.channels[].team_members_only` defaults to `false`.
Set it to `true` only with `visibility: team` to verify the members of a Slack-private channel.
See [private team channels](install.md#private-team-channels) for the checks and risks.

## Lists and agents

`lists/<key>.md` defines a task list with YAML front matter and Markdown instructions.
`agents.yaml` defines named agents and their person owners.
Both files reload with the rules. `illo doctor` names bad files and warns about unknown references.
Without a `lists/` folder, Illo supplies the `tasks` list with a person confirmation rule.
The example workspace includes `bugs` and `launches` and needs no code change for either workflow.
See [Lists and tasks](tasks.md#workspace-files) for every field, defaults, and the token setup.

## Task wake and silence

Set the optional `github.task_list` to a team list key.
Tickets from alerts then enter that list. A ticket URL maps to one task.
The list must exist. Remove the setting when you remove the list.

```yaml
github:
  repositories: [acme/app]
  task_list: bugs
tasks:
  release_max: 2
  check_minutes: 5
  reminder_window: [8, 20]
  github_writes_per_hour: 30
  stop_grace_minutes: 30
```

These values are the defaults. `release_max` is from 1 to 10. `check_minutes` is from 1 to 60.
The window has two hours from 0 to 24, in time order. The end hour is exclusive.
The write limit is from 1 to 1,000. Each attempted connector write counts for one hour.
A list sets `silence.agent_hours` and `silence.person_hours`. Both must be positive.
A person sets their IANA `timezone` in `people.yaml`.

`agents.yaml` accepts registered wake types. Unknown types give a doctor warning.
Known types validate their own fields. See [Agents](agents.md) and [GitHub](github.md).


## Channels with no entry

A channel name or a `C…`/`G…` ID can select a member channel for a list or the team brief.
Illo caches member channel names for ten minutes. It reads every page from Slack.
`illo doctor` runs no Slack check. It prints a note for a channel name with no entry.
At run time, a missing name gives an error and a sensor error.

A public channel has class `team`.
A private channel also has class `team` when its humans are exactly the active people in `people.yaml`.
Illo applies this proof to private channels with no entry.
For a listed channel, keep `team_members_only: true` to use this proof.
Other private channels have class `members`. `visibility: private` always selects this class.
Illo answers mentions from known active people there with team tools only.
The channel text stays out of shared search. A private thread read needs live proof of membership.
A task that a person requests there goes to the normal team list.
The run context tells the model to copy no other channel text into that task.

## Slack images

Illo sees PNG, JPEG, GIF, and WebP files from known active people.
It reads files on the run message first. It then reads retained context, newest first.
It reads no PDF, video, or SVG files.
Only safe `person_dm` and `team_channel` runs get images.
A bot, webhook, unknown sender, or outside workspace never supplies an image.
A run with outside text gets no images. Routines and agent requests get no images.
A private channel of class `members` needs a mention before Illo keeps text or reads images.

The defaults allow four images, each at most 8 MB as Slack reports it.
You can lower these limits. The whole image read has a 20-second deadline.
For a file above 2 MB, Illo reads Slack's 1024-pixel copy, when Slack has one. That copy also lets Illo show a file above the size limit.
Illo checks the file's first bytes. A failed read does not stop the answer.
Illo names each image's author and place. It reports files that it cannot show, with the reason.
Set `slack.images: off` to stop all image downloads.

The `openai_responses` runtime sends images with API keys and ChatGPT sign-in.
If the model provider refuses the images, the runtime sends the request again with text only. Illo then says that it could not see the images.
Other runtimes receive text that says the images could not be shown.
Image bytes and private Slack URLs stay in memory for the run.
The model provider receives the images. Illo does not store the bytes, URLs, or bot token in its ledger or logs.
The event records each shown file's ID, type, size, and person key. It also records skip counts by reason.
Text inside an image is data, never an instruction.
