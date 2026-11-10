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
| `brain.max_turns` | Tool-call limit for each run. Default: 12. Range: 1–100. The brain service accepts at most 50. |
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
