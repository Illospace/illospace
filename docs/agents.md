# Connect an agent

An agent connects to `https://illo.example.com/mcp`. Replace the host with your team's Illo address.
`/api/mcp` is an alias. Both addresses use stateless Streamable HTTP with JSON responses.

Each token belongs to one person. Use a separate label for each machine.
Create the token on the Illo host:

```sh
mkdir -p ~/.config/illo
umask 077
illo token create --person ada --label acme-laptop > ~/.config/illo/token
chmod 600 ~/.config/illo/token
```

Keep the token outside every git checkout. Transfer it to the agent machine through your team's secret store.
Send it as `Authorization: Bearer <token>`. The server also accepts `X-Illo-Bridge-Token: <token>`.
An unknown token, a revoked token, or an inactive person gets HTTP 401.

Test the connection:

```sh
illo mcp check --url https://illo.example.com --token-file ~/.config/illo/token
```

The command checks initialization, the tool list, and a lane read. It does not start a model run.

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
A report updates the same lane. Omitted fields keep their stored values.
Use `state: "done"` or `state: "failed"` with `end_summary` to save a searchable lane summary.
A private lane must belong to the calling person.

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

Use one short sentence and two to five options. The recommendation must name an option id.
The asks service sends the card. This call only writes the ledger.
The same token and `source_ref` return the same ask.
An automatic answer can use only an option that the agent proposed and a matching rule of the owner.

### `ask_pull`

```json
{"scope":"token"}
```

The default scope returns answers for this token or lanes whose machine matches this token's label.
`scope: "person"` returns answers for all tokens of the same person.
Another person never gets these answers. Each answer includes the option, text, person or rule, and time.

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
It gets no personal connector. A signal has no reply destination.
