# Connect an agent

An agent connects to `https://illo.example.com/mcp`. Replace the host with your team's Illo address.
`/api/mcp` is an alias. Both addresses use stateless Streamable HTTP with JSON responses.

Each token belongs to one person. Use a separate label for each machine.
Create the token on the Illo host:

```sh
mkdir -p ~/.config/illo
umask 077
illo token create --person ada --label acme-laptop --scope agent > ~/.config/illo/token
chmod 600 ~/.config/illo/token
```

Keep the token outside every git checkout. Transfer it to the agent machine through your team's secret store.
Send it as `Authorization: Bearer <token>`. The server also accepts `X-Illo-Bridge-Token: <token>`.
An unknown token, a revoked token, or an inactive person gets HTTP 401.

New tokens default to `--scope collector`. This scope permits lane reports, lane searches, asks, and signals.
Use `--scope agent` for a session client that also needs `illo_submit`, `illo_read`, and `illo_get_result`.
The migration gives existing tokens the `agent` scope. Revoke them and create collector tokens where needed.
Agent requests cannot use personal mail, calendar, or Drive tools. Use a person DM for those reads.

Test the connection:

```sh
illo mcp check --url https://illo.example.com --token-file ~/.config/illo/token
```

The command checks initialization, the tool list, and a lane read. It does not start a model run.

## Work on a task

An `agent` token can also use the direct task tools. These tools start no model run.
Name the agent with `--agent build` when you create its token.
The key must exist in `agents.yaml`. Its owner must be the token's person.
`illo token list` shows the agent key. Without a key, the token acts as an unnamed agent of that person.

1. Read `task_mine`. It shows state, `waits_for`, and `new_for_you`.
2. Call `task_get` first. The task is the brief. Read the list instructions with `task_lists`.
3. Write the result below the brief with `task_write`. Give `result` with its `result_version`, or give `append`.
4. Include a `message` that says what changed and what you need. A missing message refuses the call.
5. Ask one question with `task_post` when blocked. Optional `to` names a person.
6. Set `returned: true` on the last write. Never say Done.

Only the holder can write an active task. After a hand-over, change nothing and end the run.
An agent cannot mark Done, clear its holder, or hand an existing task to another holder.
`task_create` captures stated work. An agent can give an allowed holder a piece under its own task.
Task changes have a cap of 60 per token per hour. Reads do not count.
See [Lists and tasks](tasks.md) for the full rules and the result format.

## Claude Code

Load the token in the shell that runs Claude Code. Add the HTTP server:

```sh
export ILLO_TOKEN="$(cat ~/.config/illo/token)"
claude mcp add --transport http illo https://illo.example.com/mcp --header "Authorization: Bearer ${ILLO_TOKEN}"
```

Claude Code stores this connection in its local configuration. Keep that configuration outside your repository.
The command syntax follows the [Claude Code MCP docs](https://code.claude.com/docs/en/mcp).

## Codex

Load `ILLO_TOKEN` as above before you start Codex. Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.illo]
url = "https://illo.example.com/mcp"
bearer_token_env_var = "ILLO_TOKEN"
```

The configuration follows the [official OpenAI documentation](https://developers.openai.com/codex/mcp/).
These client settings were checked on 2026-11-10.

## Tool examples

Each example below gives the arguments for one tool call.
Each result has a `content` text block with JSON. `structuredContent` contains the same object.
Check `isError` before you use the result. The server records each call in the audit ledger.

### `illo_submit`

Save knowledge with no model run:

```json
{"message":"Acme checks the preview before a release.","desired_outcome":"preserve_knowledge","idempotency_key":"acme-session-preserve-1","source_tool":"codex","origin":"codex.memory","repo":"Acme/widget","session_id":"acme-session","task_title":"Release checks"}
```

Claude Code uses `source_tool: "claude-code"` and `origin: "claude-code.session-preserve"`.
The `source` object takes precedence over the top-level provenance fields.
Use `constraints: {"visibility": "private"}` for a private save. Team visibility is the default.
`parts` can add text. A part with a `text` field becomes part of the saved body.

Require `evidence_status: "satisfied"` and non-empty `mutated_target_refs` before you report a successful save.
The receipt contains a handle such as `saved:42`. The item and the receipt commit in one transaction.
Reuse the same idempotency key only for the same body. A different body returns `replay_mismatch` and saves nothing.

For another request, omit `preserve_knowledge`. Illo returns `accepted` and an `event_id` before the model finishes.
A token can start 60 such requests in one hour. Saves and replays do not count.
Poll with `illo_get_result` for the final answer.
After a server restart, an interrupted request reports a failure. Illo does not repeat a possible write.
The `response` field stores hints only. Illo does not call a supplied callback URL.
Direct target fields, such as top-level `thread_id` and `target_id`, are forbidden. Put correlation data under `correlation`.

### `illo_get_result`

```json
{"event_id":"<event_id from illo_submit>","compact":true}
```

`submission_id` and `result_id` are aliases for `event_id`.
A save is terminal at once. Another token of the same person can read the receipt.
Another person cannot read it. A failed run returns a failure and no claim of saved evidence.

### `illo_read`

Search saved knowledge, then read the returned `source_ref`:

```json
{"capability":"knowledge.search","arguments":{"query":"release checks","limit":10}}
```

```json
{"capability":"knowledge.get","arguments":{"source_ref":"saved:42"}}
```

Search also accepts `sources` and `kinds`. The limit cannot exceed 50. Semantic search is not configured.
`workspace.search` uses the same visible results in the older `{query, results}` shape.
A private item shows only to its owner. A missing or hidden item returns an empty `items` list.

### `lane_report`

```json
{"source":"t3","external_id":"acme-thread-1","title":"Check the release","state":"working","summary":"The tests are in progress.","machine":"acme-laptop","event":"The tests started."}
```

The owner defaults to the token's person. `owner` also accepts a person key, GitHub login, email, or Slack id.
A token writes only the lanes of its own person. A report on a lane of another person fails.
`owner` can name another person only for a new lane with team visibility.
Only the GitHub poller writes lanes with the source `github`.
A report updates the same lane. Omitted fields keep their stored values.
Use `state: "done"` or `state: "failed"` with `end_summary` to save a searchable lane summary.
When a lane leaves these states, Illo clears its closing time and end summary. It deletes the saved summary item.
Set `is_step: true` for a lane that is one agent step of other work.
Set `parent_ref` to the parent thread ID when the source supplies it. A step can have no known parent.
Both fields are optional. Old reports keep their stored values. New lanes default to `is_step: false`.
These fields describe the source run. Briefs count tasks instead of lanes or steps.
For T3, `review` means the source run stops after its turn. It is not proof that a task is done.
The task's holder, `waits_for`, checks, and record decide its next move.
See [T3 state rules](collectors.md#reports-and-asks) for settlement evidence and step fields.
Set `visibility: "private"` for a lane that only its owner may read. A private lane must belong to the calling person.

Collectors can report a batch and their health:

```json
{"lanes":[],"collector":{"label":"acme-laptop","version":"1","threads_seen":0}}
```

### `lane_search`

```json
{"query":"release","state":"working","owner":"ada","limit":20}
```

### `ask_create`

```json
{"lane":{"source":"t3","external_id":"acme-thread-1"},"question":"Which test target should I use?","options":[{"id":"preview","label":"Preview"},{"id":"local","label":"Local"}],"recommended":"preview","proof":{"text":"The preview has the new schema."},"source_ref":"acme-thread-1:test-target"}
```

Use one short sentence and two to five options. If you give a recommendation, it must name an option id.
The asks service sends the card. This call only writes the ledger.
The same token and `source_ref` return the same ask.
A token can have 20 open asks for each owner.
An automatic answer can use only an option that the agent proposed and a matching rule of the owner.
An answer that the model writes is a draft. The owner reads it and taps Send.

### `ask_pull`

```json
{"scope":"token"}
```

The default scope returns answers for this token or lanes whose machine matches this token's label.
`scope: "person"` returns answers for all tokens of the same person.
Another person never gets these answers. Each answer includes the option, text, person or rule, and time.
`answered_via` is `person` or `rule`. A rule answer has no `answer_text`. It names its rule in `rule_text`.

### `ask_ack`

```json
{"ask_id":42}
```

Send the answer to its source before you acknowledge it. Illo returns an unacked answer again.
A token of the same person can acknowledge a delivered answer.

### `ask_cancel`

```json
{"source_ref":"acme-thread-1:test-target"}
```

You can also pass `ask_id`. Only an open ask becomes cancelled.
Source references belong to the calling token. Ask ids also permit a new token of the same person.

### `signal_submit`

```json
{"source":"acme-monitor","dedupe_key":"preview-api:2026-11-10T12","severity":"warning","title":"The preview API failed","body":"Three checks returned HTTP 503.","data":{"checks":3}}
```

The same source and dedupe key return the first signal id with `duplicate: true`.
Illo treats the signal as outside data. The run has the team principal and the tainted profile.
It can read issues, file a ticket, and read the first URL of the signal text. Code decides the owner under the [outside-text triage rules](github.md#triage-of-outside-text). It gets no personal connector. A signal has no reply destination.
A token can send 60 new signals in one hour. A duplicate does not count.

## Wake notices

Core wake types are `none`, `poll`, and `webhook`.
`none` leaves the start to a person. `poll` means the agent reads `task_mine` on its own clock.
A webhook config uses `type`, an HTTPS `url`, and `secret`, the name of a vault secret.
Store the bearer token with `illo secret set <name>`. Do not put the token in the workspace.

```yaml
wake:
  type: webhook
  url: https://agents.example.com/wake
  secret: build-wake-token
```

Illo sends a JSON POST with this shape:

```json
{"event":"task.handed","task":42,"event_id":"123","text":"Read task 42."}
```

The event is `task.handed`, `task.message`, `task.reopened`, or `task.released`.
The bearer token comes from the vault. Each request has a 10-second timeout and one retry.
A non-2xx answer fails the wake. The durable outbox also retries failed wakes three times.
Use `event_id` to ignore duplicate notices. Read the task through the agent door to get the work.
Illo never logs the token or the complete URL query.

`lane_report` accepts an optional `task` ID, including on each lane in a batch.
A working lane refreshes that task's liveness. Matching ticket and change links also work.
The token can affect only tasks of its person or that person's agents.
A stopped lane with an open ticket tells Illo to restart the holder at the next silence check.
A repeated stopped report does not cause repeated restarts. A new working report starts a new run period.

The task list supplies the work facts for both morning briefs. Lanes supply raw run evidence.
The T3 collector includes linked pull request URLs from the thread JSON.
For SQLite, it also reads the optional `projection_thread_pull_requests` table.
It also includes an issue URL when the first user message contains exactly one GitHub issue URL.
It never copies that user message into the lane. Use fixture databases to test this read path.
A pull request link can match a task ticket through its GitHub closing or reference link in the same repository.
A reference supplies a `change` link. It never closes an issue by itself.
