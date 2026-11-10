# Workspace files

`ILLO_WORKSPACE_DIR` points to the folder. Start with `workspace.example/`.
The loader rejects unknown fields. Errors name the file and field.
A failed reload keeps the last valid workspace. The loader syncs people in one transaction.
Deleted people become inactive. Their old records stay for ledger references.
Do not store secrets in this folder. Use the vault or boot environment.

## illo.yaml

| Key | Meaning |
| --- | --- |
| `team_name` | Team name. Required. |
| `timezone` | IANA time zone for schedules. Default: `UTC`. |
| `default_owner` | Active person key from `people.yaml`. Required. |
| `slack.channels` | List of channel mappings. Default: empty. |
| `slack.channels[].name` | Display name. |
| `slack.channels[].id` | Unique Slack channel ID. |
| `slack.channels[].mode` | `coordinator`, `listen`, or `off`. Default: `listen`. |
| `slack.channels[].visibility` | `team` or `private`. Default: `team`. Illo also asks Slack. A private channel starts no run and enters no index. |
| `slack.channels[].rule_files` | Rule paths relative to the workspace. Default: all matching rules. |
| `github.repositories` | List of `owner/repository` names to follow. Default: empty. |
| `digest.schedule` | Five-field cron expression in the team time zone. Default: `0 9 * * 1-5`. |
| `digest.destination` | `person:<key>` or `channel:<id>`. Required. |
| `stale_minutes` | Source limits in minutes. All values must be positive. |
| `stale_minutes.t3` | Default: 30. |
| `stale_minutes.claude-code` | Default: 30. |
| `stale_minutes.codex` | Default: 30. |
| `stale_minutes.github` | Default: 1440. |
| `stale_minutes.manual` | Default: 1440. |
| `stale_minutes.email` | Default: 1440. |
| `brain.runtime` | Runtime sent to the brain service. Default: `claude`. |
| `brain.model` | Model sent to the brain service. Default: `default`. |
| `brain.auth` | `api_key` (default) or `chatgpt`. ChatGPT needs `openai_responses`. |
| `brain.team_credential` | Explicit active person key for runs with no person. Required with `auth: chatgpt`. |
| `brain.tainted_credential` | `none` (default), `team`, or `api_key`. Select the model credential for tainted and outsider runs. |
| `brain.api_key_fallback` | Allow the brain's API key when the selected grant is missing, broken, or at its limit. Default: `false`. |
| `brain.max_turns` | Tool-call limit for each run. Default: 12. Range: 1–50, the limit of the brain service. |
| `brain.timeout_s` | Run timeout in seconds. Default: 120. Maximum: 3600. |
| `workspace.git_push` | Commit and push confirmed notes. Default: false. |
| `run_limits.max_concurrent` | Maximum active runs. Default: 4. Range: 1–100. |
| `run_limits.max_message_chars` | Maximum inbound text length. Default: 20000. |

`coordinator` can start a run for a channel message. `listen` starts a run only
for an explicit mention, or a reply in a thread where Illo answered. `off` ignores messages.
A channel adapter supplies mention and bot metadata from the channel API.
Webhook, bot, and form text uses the `tainted` profile.
A message from a person who is not in `people.yaml` starts no run.
No Slack token is required to load these files or start the HTTP app.

To use subscriptions, set these keys in `illo.yaml`:

```yaml
brain:
  runtime: openai_responses
  model: gpt-6.1-sol
  auth: chatgpt
  team_credential: ada
  tainted_credential: none
  api_key_fallback: false
```

Use an active person key from your own `people.yaml` for `team_credential`.
If `ILLO_BRAIN_RUNTIME` is set, set it to `openai_responses` too. Environment overrides still take priority.
Each person sends `connect my ChatGPT` in their own Slack DM with Illo.
Person DMs, agent requests, and person routines use that person's grant.
Team channels and team routines use `team_credential`. They share that person's ChatGPT limit.
Tainted runs, signals, and outsiders get no model run by default, even with API fallback.
Set `tainted_credential: team` to share the team credential and its limit.
Set `tainted_credential: api_key` to use the API key for those runs.
The default owner gets at most one notice per hour when Illo skips tainted runs.
A missing personal grant never selects another person's grant.
The main app refreshes a token when it expires within five minutes.
No grant means no run, unless `api_key_fallback` is `true`.
A team run with no grant records a sensor error. The default owner gets at most one notice per hour.
For a subscription limit, an allowed API retry resumes the current turn with its existing tool results.
It does not repeat completed tool calls. The final answer says that the API key was used.
The API key must exist in the brain's environment for this retry to work.
Check that the provider's terms allow this use. An API key is the alternative.

## Check the ChatGPT connection

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

## Other files

- `people.yaml`: a mapping with `people`, a list of people. Each person has `key` and `name`.
  Optional fields: `slack_id`, `github_login`, `emails`, `role`, `timezone`, `areas`, `active`.
  The role is `owner` or `member`. The default role is `member`. The default time zone is `UTC`.
  The default `active` is true. All identities must be unique, including inactive entries.
- `rules/*.md`: YAML front matter with `name`, optional `description`, `channels`, `profiles`, and `principals`.
  Empty filters match all runs. The Markdown body supplies the rule text.
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
File paths in channel rules must match a loaded rule. Destinations must name a configured person or channel.
