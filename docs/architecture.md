# Illo: architecture and build contract

This file is the contract of Illo. Each builder reads it first. It is public: it has no data of any team.

## 1. The product

Illo is one list of tasks for people and their agents.
The task holds the brief, result, proof, next move, and record.
Lists hold each team's instructions and Done rules. People and outside agents do the work.
The task list is the picture of work. Lanes are run evidence from collectors.
Asks, alerts, and knowledge tools stay available.
See [Lists and tasks](tasks.md) for the task contract.
See [Team memory](memory.md) for chat saves, correction buttons, and memory limits.

Design goals, in order:
1. **Safe by code, not by prompt.** The ten safety rules of section 4 live in server code and have tests.
2. **Small.** A person can read the whole engine. Pages and one board for decisions. No ORM. No queue service. One Postgres.
3. **Easy to install.** A new team installs it with the README in less than 30 minutes: one install command, a Slack app from a manifest file, `people.yaml`, a model sign-in, one collector.
4. **Truth comes from the source system, not from the model.** A task state, an ask state, and a card are data in the ledger. The model reads them through tools. It never invents them.

## 2. The words

Each word has one meaning. Use these words in code, in docs, and in the text that Illo writes.

- **Person**: a team member, with a Slack id, a GitHub login, and email addresses. Defined in `people.yaml`.
- **Principal**: who a run acts for. One of: `person:<key>`, `team`, `outsider`. The gateway sets it. Nothing else can change it.
- **Task**: one unit of work, with a holder, state, next move, proof, and record. A piece is a task under a parent.
- **List**: workspace instructions, allowed holders, visibility, and a Done rule for one type of work.
- **Holder**: `person:<key>` or `agent:<key>`. An unassigned task has no holder.
- **Lane**: run evidence from a source (a T3 thread, a Claude Code session, a GitHub issue or pull request). It has a source, an owner, a state, a summary, and links.
- **Ask**: one thing that a person must decide or do. It has an owner, a question, options, a recommended option, proof, a due time, and a path to send the answer back.
- **Rule**: a standing answer or a standing permission from a person. It is visible, it has an owner, and the owner can remove it.
- **Connector**: an outside system that Illo can call. Its scope is `team` or `personal`.
- **Source**: a place where team context lives (a repository, GitHub, Slack, a drive, a mailbox). Illo searches most sources where they live. It stores the messages of the configured public Slack channels, and one lane for each GitHub issue and pull request that it follows.
- **Item**: one searchable text in the ledger: a saved summary, a decision, a note, an archive entry.
- **Note**: a fact that has no other home. A note is a file in the workspace folder.
- **Routine**: a prompt on a schedule, with a destination.
- **Signal**: a machine-made report (an alert, a log finding) that enters through the signal intake.
- **Draft**: an action that waits for the approval of a person (a post to a shared place, a message to an outsider, a model answer to an agent).
- **Workspace**: the folder of files that holds the setup of one team (section 9).

## 3. The five parts

```
Channels ──► Gateway ──► Ledger (Postgres)
   ▲            │
   │            ▼
   │          Brain (one run for each event, in a sandbox)
   │            │  per-run token
   │            ▼
   └──────── Tool server (three checks in code) ──► Ledger, Connectors, Channels
```

| Part | Job | Code |
|---|---|---|
| **Channels** | Receive and send. Slack (DM, channel, card), email, the MCP endpoint for agents, the signal intake. A channel has no logic about authority. | `illo/channels/` |
| **Gateway** | Receives an event once (dedupe). Finds the principal. Decides: no answer, a direct ledger action, or a brain run. Builds the grant of the run. Delivers the final text to the place the event came from. | `illo/gateway/` |
| **Ledger** | Postgres. Tasks, people, tokens, events, runs, audit, lanes, asks, rules, items, drafts, signals, claims, connector grants. Plain SQL functions, no model calls. | `illo/ledger/`, `illo/migrations/` |
| **Brain** | One agent run for each event. A standard agent runtime in a container with no host mount, no shell tool, no file tool, and no web tool. It sees only the Illo tool server, through a token that is valid for that run. | `illo/brain/` (client and run manager), `brain/` (the sandbox service) |
| **Tool server** | An MCP server for the brain. Each call passes three checks: scope, destination, risk. Each call writes one audit row. | `illo/tools/` |

**Providers and routes.** The brain service owns the providers: the address, the wire format, the API key and the capabilities (`brain/providers.py`). It reads them once at start from its own environment file. `GET /providers` describes them to the app: the name, the wire and the capabilities, never a key or an address. The workspace owns the routes (`brain.routes` in `illo.yaml`): each run kind has a provider, a model, an effort and limits. The app checks the routes against `GET /providers` at start and at each reload. A route that can fall back to `brain.chatgpt.fallback` needs a fallback that accepts the same requests: the route's effort, images and web search. The voice provider needs the `transcription` capability. Before a migration, `deploy/update.sh` runs the same check with the new images (`python -m brain.providers` piped into `illo check-routes`), so an update that the new code would refuse changes nothing.

The built-in `gemini` provider uses the Chat Completions wire.
It reads `ILLO_PROVIDER_GEMINI_API_KEY` from the brain environment file.
It accepts images and the efforts `minimal`, `low`, `medium`, `high`, and `xhigh`.
It provides no search or transcription.

The `chatgpt` provider uses each person's ChatGPT subscription. The main app keeps each ChatGPT grant encrypted in `connector_grants`, and refreshes it under a row lock.
It sends only the access token and account ID to the brain for one run.
`brain.chatgpt` is the payer policy of that provider. Person requests use that person's grant by default (`person_credential: own`).
The workspace owner can choose `person_credential: team` to use `team_credential` for all untainted person runs.
This changes only who pays for the model. The run keeps the requester's authority and personal data scope.
The requester's messages then pass through the selected person's ChatGPT account.
Runs with no person use only an explicit `team_credential`.
The brain keeps the same Illo tool boundary for each provider. It stores no sign-in files.
A route on the `chatgpt` provider is an explicit choice. Check that the provider's terms allow this use. An API key is the alternative.

Processes (Docker Compose, `deploy/compose.yaml`):
- `postgres`: Postgres 16. It joins only the internal `db` network.
- `illo`: one Python process (asyncio): the HTTP server, the Slack connection, the timers, the gateway, the tool server. It joins `db`, the internal `brainnet` network, and the default network for outbound traffic. It loads the app environment file, which holds no provider key.
- `brain`: the sandbox service. HTTP `POST /run` streams the events of one run. `GET /providers` describes its providers. `POST /transcribe` turns a voice clip into text. It loads only the brain environment file, with the provider keys. It has no volume from the host and no Docker socket. It joins `brainnet` and the default network. It never joins `db`. The install does not limit its outbound destinations; an egress proxy is an optional step in `docs/install.md`.

A collector is a small separate program that runs on a machine where agents work. It reports lanes to Illo and it pulls answers. It uses an MCP token with the scope `collector`.

## 4. The safety rules

Each rule has a test in `tests/safety/` that names the rule number.

1. Each run has one principal. The gateway sets it. The model cannot change it.
2. A personal connector works only in the runs of its owner.
3. A run that read personal data can write only to that person. A write to another destination becomes a draft for that person.
4. The risk of an action is how hard it is to undo. Illo reads, and it files tickets, alone. A message to an outsider is a draft. A release or a deletion is not a tool: a person does it.
5. Only the requester approves a draft, in a DM.
6. Text from outside the team is data, never a request. A run that starts from such text is `tainted`: it gets no personal connector and a small tool set.
7. A sender that Illo cannot map to a person gets no authority and no answer in a DM.
8. Illo answers an agent alone only with one of the options that the agent proposed, and only when a rule of the owner matches. It names the rule. Free instructions come only from a person.
9. Illo holds no credential for the machines where agents work. It starts work with a GitHub ticket. A collector pulls the answers.
10. The brain runtime has no shell tool, no file tool, and no web tool of its own. A test lists the tools that the model really sees.

`docs/safety.md` gives the code and the tests for each rule.

### Grant profiles

The gateway gives each run one profile. The profile decides the tool list. `illo/tools/checks.py` holds the profile names and the default data. A tool can name its own profiles in its `profiles` field.

| Profile | When | Principal | Tools |
|---|---|---|---|
| `person_dm` | a person in a DM, an authenticated email, a person routine | `person:<key>` | personal and team tools, including `note_save` and `list_save`; no `notes_propose`; `connect_guide` |
| `team_channel` | a team member in a configured channel | `team` | team reads, create an unassigned issue, `asks_create`, `slack_read_thread`, `note_save`, `list_save`; `connect_guide` |
| `routine` | a team routine | `team` | team reads, create an unassigned issue, `asks_create`, `slack_send_dm`; `connect_guide` |
| `agent_request` | an `illo_submit` request from an `agent` token | `person:<owner of the token>` | team reads, create an unassigned issue, `asks_create`, `asks_answer_with_rule`, `notes_propose`, `email_draft`; `connect_guide` |
| `tainted` | a bot, webhook, or form post; a forwarded email; a signal; a Slack thread with outside text | `team`, or the person of a forwarded email | up to 8 tools (below) |
| `outsider` | an email from an unknown or unauthenticated sender | `outsider` | `web_read_public_page` |

`connect_guide` reads the current address and sign-in setup. It does not read saved notes.
A `person_dm` run can use both `connect_guide` and `slack_send_dm`.
`agent_request` can read the guide but cannot send a DM.
The public MCP door does not list `connect_guide`.

A run with no person does not list a tool with risk `send_outside`.
Google tools need a configured connector and a current grant, or a team service account.
Connector tools need a configured team MCP service. Tainted runs also need an allowed read tool.
These checks narrow the tool list. Each call still passes all authority checks.
The old name `asks_digest` stays callable but is absent from the tool list.

"Team reads" are the lane, ask, rule, people, search, notes, skills, shared Drive, GitHub read, connector, and public web tools. The personal tools work only in `person_dm`. `asks_answer`, `asks_cancel_mine`, `rules_remove_mine`, `asks_digest`, `digest_snapshot`, and `slack_post_channel` also work only in `person_dm`.

The `tainted` grant permits these tools: `github_search_issues`, `github_get_issue`, `github_create_issue`, `claims_take`, `web_read_public_page`, `connectors_list`, `connectors_call`, and `skills_get`. A tool that names `tainted` but is not in this set is still refused. With these tools a tainted run reads only issues of the configured repositories, connector results, and public pages. It cannot read lanes, asks, people, rules, notes, search results, Slack, or pull requests. Its tickets keep the label `from-outside-text`. Code assigns trusted monitors to the default owner within the hourly triage limit. Other sources need an owner card. A connector tool needs `tainted: true` and risk `read`. A tainted run reads one public page: the first URL of its event text, once.

In a tainted run, `ToolContext.principal` is `team`. So a tainted run never reads private ledger rows, even for a forwarded email of a person. Drafts of that run still go to that person.

### The three checks of the tool server

Each tool declares, in code:
- `name`, `family`, `risk`, `destination` (a function of the arguments), `personal` (true if it reads personal data), its handler, and its arguments class (the JSON schema).
- `profiles`: the profiles that may call it. `None` means the default data in `illo/tools/checks.py`.
- `preflight` (optional): it reads facts from an API before the call, for example the visibility of a GitHub repository. It can only make the result stricter.
- `team_visible`: true when a ledger write becomes visible to the whole team, as a note does.
- `resolve` (optional): it turns a generic tool into the configured remote tool before the checks, as `connectors_call` does.

The checks:
- **Check 1, scope.** The tool must be in the grant of the run. A personal tool also needs an untainted `person_dm` run of its owner.
- **Check 2, destination.** A tool names its destination: `person:<key>`, `channel:<id>`, `github:<owner>/<repo>`, `outside:<key or address>`, `ledger`. A read with a `ledger` destination always passes. A read with an `outside:` destination (a connector call) sends its arguments out, so it is checked like a write. When the run has `personal_read = true`, each destination other than `person:<principal>` and `ledger` gives `draft`. In that case a `team_visible` tool also gives `draft`.
- After a personal read, a connector call gives `deny`, with no draft.
- **Check 3, risk.** `read` → allow. `write_internal` (a ticket, a note, a lane update, a message to a team member) → allow. `send_outside` → draft. There is no tool for a destructive action.

The run gets `personal_read = true` after a personal tool, and after a read result with `visibility: private`.

A check returns `allow`, `draft`, or `deny`. The strictest result wins. `draft` creates a row in `drafts` and a card for the person of the run. A run with no person cannot create a draft: the result is `deny`. The tool result tells the model what happened. Each call writes `audit` with: run, principal, tool, an HMAC digest of the arguments, the redacted arguments, the decision, the reason.

Risks of note: `asks_answer`, `email_draft`, `github_assign_issue`, and `github_comment_issue` have risk `send_outside`. Agent trigger labels also need approval. Without a person, tickets stay unassigned and drop model labels. A GitHub write to a public repository becomes `send_outside` in its preflight.

### Limits in one run

`RUN_LIMITS` in `illo/tools/registry.py` counts the `audit` rows of the run: at most 3 `asks_create`, 3 `slack_send_dm`, 3 `github_create_issue`, 5 `github_comment_issue`. All tool calls of one run stop at the `max_tool_calls` of its route, which the run stores in `runs.tool_limit`. At the limit, a normal model request can still answer.
If the model asks for more tools, the runtime makes one final request with tools disabled.
The answer has `usage.limited = true`. Claude uses a separate client with no tools and the results from the run.
The run stops at the `timeout_s` of its route.

Each Slack channel starts at most 30 tainted runs per hour. Owner notices have a cap of one per run and six per hour; remaining links wait for an hourly digest.

## 5. The ledger

Postgres 16. Plain SQL in `illo/migrations/NNNN_<area>_<what>.sql`, applied in file order by `illo migrate`. The foundation owns `0001_init.sql`. A later change uses the number block of its area: slack 0100, mcp 0200, github 0300, search 0400, asks 0500, brain 0600, collector 0700, routines 0800, email 0900, connectors 1000, notes 1100, tasks 1200. A migration never edits an older file.

The main tables (each has `id bigint generated always as identity`, `created_at timestamptz default now()` unless the text says otherwise):

- `people`: `key` (unique), `name`, `slack_id`, `github_login`, `emails text[]`, `role` (`owner`, `member`), `timezone`, `areas text[]`, `active`. The file `people.yaml` is the truth. The table is a copy that the loader keeps equal.
- `tokens`: MCP client tokens. `person_id`, `label` (the machine or the agent), `token_hash` (SHA-256), `scopes text[]` (exactly one of `collector`, `agent`; default `collector`), `last_used_at`, `revoked_at`. Verification reads the active owner and revocation state on every call, without a row lock. A separate transaction updates `last_used_at` at most once per minute. It skips busy rows and limits table lock waits to 100 milliseconds. Separate advisory locks preserve write limits and ask retries.
- `agent_oauth_clients`: public client registrations, app names, exact redirect addresses, creation time, and first approval time. No client secrets.
- `agent_oauth_requests`: pending sign-ins, PKCE challenges, resource, state, hashed pairing code, and a separate hashed browser handle. Slack identity and card IDs bind consent to the person. The row also holds the authorization code hash and its use time. Migration `1702_agent_slack_signin.sql` adds nullable `slack_state_hash`, `slack_nonce_hash`, `consent_hash`, and `notice_at`. The old app needs no values for these columns.
- `agent_oauth_access`: opaque access token hashes, stable token IDs, resource, and expiry time.
- `agent_oauth_refresh`: refresh token hashes, stable token IDs, client IDs, resource, a random seed, expiry time, and use time. Old hashes remain to detect reuse. Illo makes the next pair from the refresh token and its seed, so a retry gets the same pair and starts no second chain.
- `agent_oauth_limits`: persistent counters for registration and pairing attempts. The database clock sets their windows.

- `events`: the gateway inbox. `channel`, `external_id`, unique (`channel`, `external_id`), `kind`, `principal`, `person_id`, `tainted`, `payload jsonb`, `status` (`received`, `ignored`, `handled`, `failed`).
- `runs`: `event_id`, `principal`, `person_id`, `profile`, `tainted`, `personal_read`, `status` (`running`, `completed`, `failed`, `timeout`), `route` (the run kind), `provider`, `model`, `payer` (`api_key` or `chatgpt`), `payer_person_id`, `tool_limit`, `usage jsonb`, `error`, `started_at`, `ended_at`, `token_hash` (the per-run token), `token_expires_at`.
- `audit`: `run_id` (nullable), `token_id` (nullable), `principal`, `tool`, `args_digest`, `args jsonb` (redacted), `decision`, `reason`, `at`.
- `lane_search`: one row per lane, with primary key `lane_id` and an indexed `vector tsvector`. It has no `id` or `created_at` column. Triggers refresh the vector after lane text or event changes. The vector uses the title, summary, and last 20 events in descending time and ID order. Event writers lock the lane first. Migration `0405_search_lanes.sql` backfills existing lanes.
- `lanes`: `source` (`t3`, `claude-code`, `codex`, `github`, `manual`, `email`), `external_id`, unique (`source`, `external_id`), `title`, `owner_person_id`, `state` (`open`, `working`, `waiting_person`, `blocked`, `review`, `done`, `failed`, `unknown`), `summary`, `links jsonb`, `repo`, `machine`, `visibility` (`team`, `private`), `last_signal_at`, `updated_at`, `closed_at`, `end_summary`, `slack_channel`, `slack_ts`. A lane with state `working` and no signal for longer than the limit of its source shows as `stale`. `stale` is computed on read. It is never stored. `open` means backlog: the lane exists and is not closed, and nobody reported work on it. Default lane lists omit `open` unless a text query matches it or a state filter requests it. Active lanes appear first. The briefs read tasks instead. Source states do not decide who must act on a task.
- `lane_events`: `lane_id`, `at`, `kind`, `text`, `data jsonb`.
- A lane also has `is_step` (default `false`) and optional `parent_ref`. These fields mark steps of other work.
  These fields describe the source run. They do not add work items to a brief.
  An agent that stops is not a proof. See [the T3 rules](collectors.md#reports-and-asks) for settlement evidence.
- `asks`: `lane_id` (nullable), `kind` (`decision`, `action`, `review`, `reminder`), `requester_token_id` (nullable), `requester_person_id` (nullable), `owner_person_id`, `question`, `options jsonb` (list of `{id, label, detail}`), `recommended_option`, `proof jsonb` (text and links), `due_at`, `expires_at`, `state` (`open`, `answered`, `expired`, `cancelled`), `answer_option`, `answer_text`, `answered_by_person_id`, `answered_via` (`person`, `rule`, `approved_draft`), `rule_id`, `answered_at`, `delivery_state` (`none`, `pending`, `delivered`, `acked`, `failed`), `delivery_attempts`, `delivered_at`, `acked_at`, `slack_channel`, `slack_ts`, `reminded_at`, `source_ref` (the thing in the source system that this ask mirrors, for the stale check).
- `rules`: `owner_person_id`, `scope` (`person`, `team`), `kind` (`always_allow`, `standing_answer`), `match jsonb` (ask kind, lane source, repository, requester, a question pattern), `answer_option_id`, `answer_option_label`, `answer_option_detail`, `text`, `origin` (`card`, `file`, `proposal`), `uses`, `removed_at`.
- `items`: the search table. `kind` (`saved`, `decision`, `note`, `lane_summary`, `archive`, `slack`, `github`, `doc`), `source_ref` (unique), `title`, `body`, `summary`, `owner_person_id`, `visibility` (`team`, `private`), `links jsonb`, `meta jsonb`, `source_at`, `tsv tsvector` (generated from title, summary, body; kept for row compatibility).
- `item_search`: one row per item, with primary key `item_id` and an indexed `vector tsvector`. It has no `id` or `created_at` column. Triggers refresh the vector after insert or changes to title, summary, or body. A foreign key removes the vector after an item delete.
- `slack_event_search`: one stored vector per team Slack event of kind `message` or `channel`. Its primary key is `event_id`. It has no `id` or `created_at` column. Triggers refresh text and eligibility changes. A foreign key removes the vector after an event delete. Search still checks current channel access and excludes events with an item snapshot.
- `memory_changes`: note and list changes, with the person, run, earlier text, saved text, state, and Slack correction thread.
- `submissions`: the receipts of `illo_submit`. `event_id text` (unique, opaque), `token_id`, `idempotency_key`, unique (`token_id`, `idempotency_key`), `desired_outcome`, `envelope jsonb`, `envelope_digest`, `status`, `result jsonb`, `completed_at`, `run_id`.
- `drafts`: `run_id`, `approver_person_id`, `kind`, `destination`, `content jsonb`, `state` (`pending`, `approved`, `rejected`, `sent`, `expired`), `slack_channel`, `slack_ts`, `decided_at`.
- `signals`: `token_id`, `source`, `dedupe_key`, unique (`source`, `dedupe_key`), `severity`, `title`, `body`, `data jsonb`, `status` (`received`, `ticketed`, `ignored`), `ticket_url`.
- `claims`: `key` (unique), `run_id`, `note`. One claim for each alert or signal, so that two runs cannot file the same ticket. A model claim has the prefix `model:`. The minute timer deletes model claims older than 24 hours. Server keys, such as `routine:<name>:<time>`, have no prefix.
- `connector_grants`: `connector_key`, `person_id` (null for a team connector), `secret bytea` (encrypted), `scopes text[]`, `expires_at`, `revoked_at`.
- `secrets`: the vault. `name` (unique), `secret bytea` (encrypted with the key in `ILLO_VAULT_KEY`).
- `routine_state`: `key` (unique), `last_run_at`, `next_run_at`, `last_status`.
- `sensors`: `key` (unique), `last_ok_at`, `last_error_at`, `detail`. One row for each thing that must stay alive: the Slack connection, each collector, the GitHub poll, the brain.
- `kv`: `key` (unique), `value jsonb`.

Agent OAuth uses Slack OpenID sign-in to prove identity when the operator stores both Slack client values in the vault.
The Slack connection records its trusted `auth.test` team ID in `kv` under `slack:team`.
HTTP routes read that ID. They never take the team ID from the browser as proof.
The callback validates Slack's RS256 signature with its HTTPS JWKS endpoint.
It checks the issuer, client audience, nonce, expiry, workspace team, and active person.
The database clock checks all times. Only state and nonce hashes reach the request row.
Slack access tokens and ID tokens stay in the callback and never enter storage or logs.
The two outside calls share a three-second deadline within the existing five-second route deadline.

After sign-in, the browser shows consent. Allow and Cancel require a POST with a one-use CSRF value.
The row stores its hash. The browser handle binds it to this request and browser.
A GET never approves. This prevents silent approval in a victim's browser.
A hosted callback can otherwise send that person's identity to an attacker's app session.
The answer to that POST is a page, not a redirect. A browser can apply `form-action` to each redirect after a form POST, and the last address is the app. The page starts a new navigation to `/oauth/agent/continue`.
When the Slack card decided first, the click in the browser changes nothing. The page says what the card decided, and the person continues with a link.
The token times `iat` and `nbf` can be 60 seconds later than the database clock, because the two clocks can differ. `exp` gets no allowance.

The page also offers the code method in a closed details block.
Without Slack client values, the page keeps only the code method.
The person sends the pairing message in their own DM and decides on the Slack card.
Code catches the whole pairing message before the gateway stores an event or starts a model run.
Slack history and tool reads remove pairing codes too.
Both paths lock the request and call one ledger decision function.
They keep the active-person check, expiry check, and single stable connection.
The card path also checks its original person, DM channel, and message.
After commit, `notice_at` reserves one best-effort DM attempt. A Slack failure does not undo the connection.
Approval creates one stable `tokens` row with the `agent` scope.
Its `token_hash` starts with `!agent-oauth:`. A SHA-256 bearer hash cannot match this value.
Verification also looks in `agent_oauth_access` and checks the resource, expiry, stable token revocation, and active person.
The endpoint passes its resource address. Internal gateway checks use the already admitted bearer.
Static tokens still work on either MCP address. OAuth access tokens stay bound to the address that the client requests.
Refresh and code reuse revoke the stable row, so all secrets for that connection stop at once.
Each step commits before any Slack call. Discovery and token routes make no outside call and have a five-second application deadline.

Migration `0406_search_items.sql` backfills these tables without a source writer lock.
It records row versions, builds the GIN indexes, then briefly locks writers and checks changed rows.
It installs triggers and foreign keys before commit. Existing application write paths stay valid.
The migration drops the unused item indexes and Slack expression index. It keeps the generated item `tsv` column.
Item and inbound Slack queries rank stored vectors before the limit, then read text for the returned rows.

Access to the database: `psycopg` 3 (async, with a pool). Each ledger area is one module of small functions that take a connection: `illo/ledger/people.py`, `lanes.py`, `asks.py`, `rules.py`, `items.py`, `audit.py`, `drafts.py`, `signals.py`, `tokens.py`, `runs.py`. No ORM.

## 6. The gateway and a run

1. A channel builds an `InboundEvent` (`channel`, `external_id`, `kind`, `sender`, `text`, `payload`, `reply_to`) and gives it to `gateway.receive(event)`.
2. The gateway calls `record_event` in `illo/gateway/events.py` to insert it into `events`. A duplicate stops here.
3. The gateway finds the principal:
   - A Slack DM from a known person → `person:<key>`, profile `person_dm`.
   - A Slack DM from an unknown sender → no answer (rule 7).
   - A message in a member channel from a known person → `team`, profile `team_channel`. A message from a webhook, a bot, or a form → `team`, `tainted`. A message from an unknown user, or from a Slack Connect user of another workspace → no run. A private channel can answer a known active member. Its text stays out of shared search unless all active people can read it.
   - An email with one aligned DMARC pass from a known address → `person:<key>`, profile `person_dm`. A forwarded email → the same person, `tainted`. Any other email → `outsider`, and the `unknown_sender` claim takes it before a run.
   - A team routine → `team`, profile `routine`. A person routine → `person:<key>`, profile `person_dm`.
   - An MCP request from an agent token → `person:<owner of the token>`, profile `agent_request`.
   - A signal → `team`, `tainted`.
4. Preparers run (`register_preparer`). They change verified input before any claim: Slack reads the transcript of a clip, and the Google sign-in code removes a callback address.
5. Claims run (`register_claim`). A claim takes an event that continues a flow of its area: a thread reply to an ask, a corrected note, a pasted sign-in address. A claimed event gets no brain run.
6. The gateway shows the first sign of work itself, in code, in less than one second: in Slack it posts a short placeholder message, and later it edits that message with the answer. The model is not in this path. In Slack this step also adds the thread history. If that history has a bot or an unknown user, the gateway makes the run `tainted` (`Gateway.context_authority`).
7. `RunManager.start_run` classifies the run kind and resolves its route. It inserts a `runs` row with the route, the provider, the model and the tool limit. Then it does the setup and selects the payer. It makes a per-run token and calls `POST /run` with the prompt, the message, the tool server and the route values. Every failed run stores a category in `runs.usage.failure_category`.
8. The brain service streams events: `started`, `text`, `tool_call`, `final`, `error`. The run manager can show a tool call as a status line.
9. The gateway gives the final text to the channel that the event came from. Each other write goes through a tool and its checks.
10. The run ends. The run manager revokes the token and stores the status, the payer, the usage and the end time.

A run has a time limit and a tool-call limit from its route. A failed run gives the person a short honest message.

A deep run is a normal run on the `deep` route. The worker calls `record_event` with the stored authority and keeps `task_actor` in the event payload. It passes a `DeepContext` to `start_run` with the request ID, kind, authority, actor, delivery target and read flags. The run manager links the run to the deep request when it inserts the run. It checks that the requester is active and refreshes the repositories before it selects a payer. The route timeout covers these steps and the brain stream.

**Run kinds, routes and payers.** Each concept has one function in `illo/brain/routes.py`:

- `run_kind(event, authority, *, deep)` classifies the run from the verified event channel and the authority: `deep` for a deep run; `tainted` for a tainted authority; `slack` or `email` with profile `person_dm` or `team_channel` gives that kind; `routine` gives `routine`; `digest` and `team_digest` give `digest`; `mcp` gives `agent_request`; `signals` gives `tainted`. Any other pair is an error. The profile alone is not enough: a personal routine and a personal digest have the `person_dm` profile, but they are `routine` and `digest` runs.
- `resolve_route(workspace, kind)` gives the `default` route with the kind's route on top.
- `select_payer(ctx, route, authority)` gives who pays: the API key of the route's provider, or the ChatGPT grant of one person. It reads the authority and never changes it. It keeps every rule of `brain.chatgpt`: own or team grants, the explicit team grant for tainted runs, the allowed accounts, and the fallback before the run.
- `sends_refresh_notice(kind)` permits sign-in refresh notices for all run kinds except `digest` and `deep`.
- `raises_failure(kind)` tells the run manager to raise payer and sign-in failures for `digest` and `deep`. Other kinds get the sign-in reply.
- `web_search_allowed(workspace, route, provider, deep)` controls provider search at the start of a deep run. It checks the workspace setting, the deep kind and profile, the read flags, and the selected provider's `web_search` capability.
- `check_routes(workspace, providers)` checks each route against `GET /providers`. At start, a failure stops the app; while the brain is not reachable, the app waits. At reload, a failure keeps the last valid workspace and logs the reason.

The brain interface (`illo/brain/client.py`): `run(request)`, `providers()` and `transcribe(...)`. `RunRequest`: `run_id`, `system_prompt`, `images`, `user_message`, `mcp_url`, `mcp_token`, `provider`, `model`, `effort`, `max_tool_calls`, `timeout_s`, `max_output_tokens`, `web_search`, `provider_auth` (ChatGPT only), `fallback_provider`, `smoke`. The brain answers an unknown provider or a missing capability with the `config` category. The brain caps each run at `min(timeout_s, ILLO_BRAIN_MAX_TIMEOUT_S)`. The brain events are `started`, `text`, `tool_call`, `web_search`, `payer`, `response`, `final` and `error`. The smoke check uses the `response` event. The `payer` event marks a switch to the fallback provider during the run. The run keeps the first payer in its payer fields and records the fallback in its usage. A fake brain in `illo/brain/fake.py` plays a script of tool calls. Each test uses it, with the provider description of the brain's built-in providers. The real wire adapters live in the `brain/` service.

### The brain loop

The brain service has one agent loop, `brain/loop.py`. Each wire format has one small adapter in `brain/wire/`:

- `responses.py`: the Responses wire, for the `chatgpt` and `openai` providers.
- `chat.py` uses Chat Completions for `gemini` and custom providers.
  It keeps every assistant field when it replays a message, including fields on a tool call.
  The adapter docstring gives the rules for combining stream deltas.
- `agent_sdk.py`: the Claude Agent SDK, for the `anthropic` provider. It is the self-looping exception below.
- `fake.py`: lists the tools of the run and makes no model request (the `fake` provider).

An adapter knows only the provider format. It builds one request from the history, the tools and the options. It reads the provider stream and gives back text, tool calls, the raw usage and a stop reason. It maps a provider error to an error category. An adapter does not call a tool, does not count, and does not send a request again. An adapter never imports another adapter. The server gives the worker its provider (with its key) on standard input. The worker has no other provider key in its environment. `brain/worker.py` selects the adapter for the wire of the provider.

The loop owns the rules of a run:

- **The counting rule.** The limit (`max_tool_calls`) counts tool calls, not model turns. Each call of a batch counts. The loop does not run a call after the limit. That call gets the result "The tool limit is reached. This tool did not run."
- **The last request.** After a call over the limit, the loop sends one last request with no tools. It tells the model to answer now. The usage of the run then has `limited: true`. When the run reaches the limit exactly, the next request is a normal request.
- **The usage sum.** The loop adds the raw usage of each response (`brain/usage.py`). It keeps each numeric field and each detail object.
- **The subscription fallback.** When the ChatGPT limit stops a request and the request names a `fallback_provider` (from `brain.chatgpt.fallback`), the loop sends a `payer` event, then sends that request once again to that provider with its API key. It keeps the tool results of the run and does not run a tool again. The usage adds both parts. The brain refuses a request that the fallback cannot serve (`brain/providers.py` `unsupported`, the same rule as for the first provider).
- **The sign-in refresh.** When the provider rejects the sign-in, the brain sends an `auth` error. The brain cannot refresh the grant. The app run manager refreshes it once, if no tool call happened, and starts the run again.
- **The transient retry.** The loop waits two seconds and retries once after a `transient` error.
  It retries only if that request produced no text and no tool call. This includes text still in the adapter buffer.
- **The image retry.** When the provider refuses the images, the loop sends the request once again as text. The text tells the model that it could not see the images.

The app uses the selected provider's `images` capability from `GET /providers` to decide whether to download Slack images.
When the provider cannot show images, the model receives a note.
The brain rejects images with `config` if the provider lacks that capability.
The Chat wire writes images as `image_url` parts with data URLs.

Other owners:

- `brain/tools.py` is the Illo MCP boundary. It parses the tool arguments, maps an MCP failure to a tool result, and makes the result text. It refuses an echo of a credential or private image data before a tool call can reach MCP. The Chat adapter also checks the assembled message because an echo can span multiple deltas. On the Responses wire, it reads the run flags: a customer read, a personal read, or a result with no flag metadata closes web search for the rest of the run. The Chat Completions wire reads no run flags today, because it never sends web search. The app makes the run flags (`illo/brain/server.py`).
- `brain/errors.py` has the error categories: `auth`, `timeout`, `runtime`, `limit`, `transient`, `internal`. The server adds `config` for a request that the provider table cannot serve. The app maps it to `RunFailure("config")` and the reply "Illo is not set up for this kind of request. An operator must fix the settings." The Chat wire maps HTTP 500 and 503 to `transient`. A provider error message never enters an event or a log.
- `brain/server.py` (`redact`) removes credentials from each event line before the line leaves the service.

**The self-looping exception.** The Agent SDK runs the tools itself, so the Claude adapter cannot use the loop. The SDK starts the Claude Code CLI as a subprocess with its built-in tools off. It sees only the Illo MCP server (`tools.SDK_SERVER`), and `tests/safety/test_rule_10_brain_tools.py` checks this. The SDK counts its own turns, so the adapter gives it `max_tool_calls + 1` (the SDK parameter is `max_turns`). The adapter gives its events and one provider operation to `self_looping` in the loop: a function that sends one request with no tools (a new SDK client with no MCP server and `max_turns=1`). `self_looping` decides when the last request starts (at the SDK `limit` error), writes its prompt (the transcript as outside data and the instruction of the last request), adds its usage, marks the usage `limited` and chooses the answer (`last_answer`). The adapter maps its errors with `errors.py`.

**One wire contract in two copies.** `brain/models.py` and `illo/brain/client.py` have the same request models. The brain image has only the `brain/` folder, so the two packages cannot share one module. `tests/brain_service/test_client_contract.py` compares the JSON schema of the two copies, field by field: the names, the types, the defaults, the required fields, the literal values, the bounds and the formats. It also compares the defaults that each model makes. The copies differ in named fields, for example the sign-in models and the bounds that only the service checks. The test pins the exact schema of each side for these fields and gives the reason. Any other difference fails. `tests/brain_service/test_wire_inputs.py` pins the payloads that each copy accepts.

`tests/brain_service/test_golden.py` records the events, the provider requests and the `runs.usage` value of each wire for a fixed set of runs.

## 7. The tools that the brain sees

The operator's `illo brain smoke` command uses the run manager and the normal payer rules.
It offers only `smoke_step`, a tool that passes a random token from one response to the next.
The smoke check needs three separate tool responses and a correct image answer if the provider accepts images.
Its output includes response field names, stop reasons, and raw usage.

Tool names use `family_verb`. Each family is one file in `illo/tools/`. The registry finds each file by itself, so two builders never edit the same file.

| Family | Tools (first set) |
|---|---|
| `tasks` | `page_read`, `page_write` when pages are on; see Task pages below. |
| `lanes` | `lanes_search`, `lanes_get`, `lanes_mine` |
| `asks` | `asks_list`, `asks_get`, `asks_create` (Illo asks a person), `asks_answer` (a draft for the ask owner), `asks_answer_with_rule`, `asks_cancel_mine`, `asks_digest`, `digest_snapshot` |
| `search` | `search_all` (items, lanes, GitHub, Slack, Drive and mail of the person; filters by principal), `search_get` |
| `people` | `people_list`, `people_get` |
| `slack` | `slack_send_dm` (to a team member), `slack_post_channel`, `slack_read_thread` |
| `github` | `github_search_issues` (open and closed), `github_get_issue`, `github_create_issue`, `github_assign_issue`, `github_comment_issue`, `github_get_pull` |
| `claims` | `claims_take` (the key gets the prefix `model:`) |
| `skills` | `skills_get` |
| `rules` | `rules_list`, `rules_remove_mine` |
| `notes` | `notes_search`, `notes_propose` |
| `memory` | `note_save`, `list_save` |
| `web` | `web_read_public_page` (a URL of the event text or the root of its host; text only; no private address) |
| `connect` | `connect_guide` (current app steps), `connect_status`, `connect_start`, `connect_propose_mcp`, `connect_allow_tools`, `connect_remove` |
| `connectors` | `connectors_list`, `connectors_call` (a team connector through the MCP client, with a risk level for each tool) |
| `personal` | `mail_search`, `mail_get`, `calendar_list`, `drive_search`, `drive_get` (personal connectors, read-only), `drive_shared_search`, `drive_shared_get` (the team shared drive) |
| `email` | `email_draft` (always a draft) |

Section 4 says which profile gets which tool.
The deep kind controls access to `shell` and `web_fetch`. See [Deep work](deep.md) for the rules.

## 8. The MCP endpoint for agents

`POST /mcp` (also at `/api/mcp`). Streamable HTTP, stateless, JSON answers. Auth: `Authorization: Bearer <token>` (also `X-Illo-Bridge-Token`). A token belongs to a person and has a label for the machine. `illo token create --person <key> --label <machine> --scope collector|agent` prints it once. A token has exactly one scope:
- `collector` (the default): `lane_report`, `lane_search`, `ask_create`, `ask_pull`, `ask_ack`, `ask_cancel`, `signal_submit`.
- `agent`: the `collector` tools, plus `illo_submit`, `illo_read`, and `illo_get_result`.

A call to a tool outside the scope fails. `tools/list` shows only the tools of the scope. Caps for each token: 20 open asks and 30 new asks per hour for each owner, 60 new signals in one hour, and 60 `illo_submit` requests that start a run in one hour.

Tools. The first three keep the request and answer fields that agents use today. `tests/contracts/` holds fixtures of real calls, and these tests run on each change.

- `illo_submit`: `message` (required), `desired_outcome`, `origin`, `parts`, `source`, `constraints`, `correlation`, `response`, `idempotency_key`, `source_tool`, `repo`, `branch`, `task_title`, `files_touched`, `session_id`, `run_id`, `metadata`.
  - `desired_outcome = "preserve_knowledge"`: the server writes one `items` row (`kind = saved`, owner = the person of the token, visibility from `constraints`, default `team`) and the receipt in one transaction. No model run. The answer has `status`, `event_id`, `submission_id`, `result_id` (the three are equal), `idempotent_replay`, and `ilo_outcome` with `evidence_status: "satisfied"` and a non-empty `mutated_target_refs` (`[{kind: "item", id, source_ref, role: "preserved", visibility}]`).
  - The same `idempotency_key` with the same body returns the first receipt with `idempotent_replay: true`. The same key with another body returns `evidence_status: "replay_mismatch"` and no refs.
  - Each other outcome becomes a gateway event with the profile `agent_request`. The answer has the `event_id`, and the agent polls.
- `illo_get_result`: `event_id`, optional `compact`. Returns `event_id`, `status`, `run_status`, `handling_status`, `evidence_status`, `mutated_target_refs`, `completed_at`, `reconciled_at`, `terminal`, `final_answer`, `failure`.
- `illo_read`: `{capability, arguments}`.
  - `knowledge.search`: `query`, `kinds`, `sources`, `limit` (default 10, maximum 50). Answer: `query`, `results` (each: `id`, `source`, `kind`, `source_ref`, `title`, `summary`, `scores`, the source time), `semantic_available: false`, `requested_limit`, `effective_limit`.
  - `knowledge.get`: `source_ref`. Returns the full item if the person of the token may read it.
  - `workspace.search`: the same search as `knowledge.search`. It exists so that an old caller gets real results.
- `lane_report`: upsert of one lane. A token writes only the lanes of its own person. It cannot write a `github` lane: only the GitHub poller writes those. `source`, `external_id`, `title`, `state`, `summary`, `links`, `repo`, `machine`, `owner` (a person key, GitHub login, email, or Slack id; default: the person of the token; another person only for a new `team` lane), `visibility` (`team` or `private`), `event` (one line of what happened), `end_summary` (when the lane closes; it also becomes an item).
- `lane_search`: `query`, `state`, `owner`, `limit`.
- `ask_create`: `lane` (`source`, `external_id`), `kind`, `question`, `options`, `recommended`, `proof`, `owner` (optional), `due_at`, `expires_at`, `source_ref`. Returns `ask_id` and, if a rule answered at once, the answer.
- `ask_pull`: returns the answers that wait for the lanes of this token (`ask_id`, lane, option, text, who, when, via). `ask_ack`: `ask_id` → the answer is delivered. An answer that gets no ack is given again. After the end date the delivery state is `failed` and the owner sees it in the digest.
- `ask_cancel`: `ask_id` or `source_ref`. For a question that is not open any more.
- `signal_submit`: `source`, `dedupe_key`, `severity`, `title`, `body`, `data`. Returns a receipt at once.

Each call writes an `audit` row with the token.

## 9. The workspace folder

`ILLO_WORKSPACE_DIR` points to a folder. For a team this is a checkout of a private repository. `workspace.example/` in this repository is a complete example. Illo loads the folder at start and when the files change (`illo reload`, and a check on a timer).

```
people.yaml          # the people
illo.yaml            # the team: name, time zone, default owner, Slack channels and their mode, GitHub repositories, digest time, stale limits, brain runtime and model
rules/*.md           # team rules and playbooks. Front matter says where a rule applies. The body goes into the prompt of the matching runs.
skills/<name>/SKILL.md   # a skill: front matter (name, description), body. The brain sees the list and loads one with a tool.
routines/<name>.md   # front matter: schedule (cron), destination, principal. The body is the prompt.
connectors/<name>.yaml   # one connector: kind (mcp, google, github), scope (team, personal), URL, the tools with their risk level. No secret.
notes/*.md           # the team notes
```

A secret is never in the workspace. Boot secrets are in the environment file. Connector secrets go into the vault with `illo secret set <name>`.

## 10. Channels

**Slack** (`illo/channels/slack/`): Socket Mode, so the host needs no public address. The app comes from `deploy/slack/manifest.yaml`. Events: `message.im`, `app_mention`, `message.channels` and `message.groups` for member channels, `block_actions`, files. A private channel answers known active people who mention Illo. It enters the shared index only with proof that every active person can read it. `visibility: private` keeps its text out of the shared index. Channels need no entry. `slack.default_mode: off` accepts only listed channels. Cards use Block Kit. A click on a button comes in as `block_actions`, the handler writes the ledger, and it edits the card. A card is built from the ledger row, and a check compares each open card with its row. An audio clip uses Slack's transcript first. By default, a clip with no transcript fails at once. The optional `voice.transcription: openai` mode transcribes verified human audio with the API key. It checks the file, thread authors, duration, size, deadline, and per-person hourly limit before use. The transcript keeps the person's authority and enters the preparer before claims. See [workspace.md](workspace.md#voice-clips) and [safety.md](safety.md#voice-privacy).

Card types: the ask card (question, options as buttons, the recommended option first, proof, "Always allow" where a rule is possible), the draft card (Send, Do not send; the full text, with hidden characters shown as escapes), the morning brief (a model reply from one personal snapshot, with a plain fallback), the note card ("I noted: ...", Correct, Remove).

**Email** (`illo/channels/email/`): the mailbox is read with IMAP on a timer, so the host needs no public address. A sender has authority only with exactly one DMARC `pass` result whose `header.from` equals the From domain, in the trusted `Authentication-Results` header. The trusted header is the first one with the server name in `ILLO_EMAIL_AUTHSERV_ID`, or the topmost header with the explicit value `topmost`. A blank setting disables email. Outbound mail is always a draft card first.

**MCP**: section 8. **Signals**: `signal_submit`.

## 11. Sources and search

One function, `search(principal, query, kinds, limit)`, in `illo/sources/search.py`:
- `items` by full text (`websearch_to_tsquery`, `simple` and `english`), filtered by visibility: a private item shows only to its owner.
- `lanes` by title, summary, and events.
- GitHub issues and pull requests, live, through the GitHub client.
- Slack messages that Illo saw in verified team channels: the `slack` items and inbound events missing from that index.
  Event search checks current channel privacy. It never reads DMs or email. Tainted runs cannot use search.
- When the Google connector files exist: the shared drive, and the mail and Drive of the person (only in an untainted run of the owner).

Each result has a title, a short text, a link, the source, and the time. The brain can search several times with other words. Search uses lexical vectors. Semantic vectors remain off.

The GitHub source (`illo/sources/github.py`) reads the configured repositories on a timer. An open issue or pull request becomes a lane with the source `github`. Auth: a GitHub App (id, private key, installation) or one token (`ILLO_GITHUB_TOKEN`).

## 12. Layout of the repository

```
illo/
  __init__.py  app.py  cli.py  config.py  db.py  workspace.py  vault.py  log.py
  migrations/
  ledger/        people lanes asks rules items audit drafts signals tokens runs
  gateway/       events principal dispatch replies
  brain/         client routes run_manager smoke prompts fake
  tools/         registry checks + one file for each family
  channels/      slack/ email/ mcp/ signals/
  sources/       search github slack_index drive archive
  connectors/    mcp_client google oauth github_client
  routines/      scheduler digest reminders stale heartbeat watchdog
brain/           the sandbox service: server, providers, loop, wire adapters, Dockerfile
collectors/      t3/  hooks/ (Claude Code and Codex)
deploy/          compose.yaml  Dockerfile  install.sh  update.sh  slack/manifest.yaml
                 env.example (the app file)  brain.env.example (the brain file, with provider keys)
workspace.example/
ops/external_deadman/   the outside alarm
tests/           conftest.py  safety/  contracts/  one folder for each area
docs/            README.md (the map of the docs)  architecture.md  safety.md  …
```

Rules for builders:
- **Stay in your area.** The foundation creates a stub for each module with a `setup(ctx)` function that does nothing. `illo/app.py` calls each `setup`. A builder fills its own stub. A builder does not edit `app.py`, `0001_init.sql`, or the file of another area. If you need a change in another area, write it in your pull request text.
- **`ctx`** (`illo/app.py: AppContext`) holds the config, the database pool, the workspace, the tool registry, the gateway, the Slack client (or `None`), the brain client, and a small scheduler (`ctx.every(seconds, fn)` and `ctx.cron(expr, fn)`).
- **Python 3.12.** Dependencies: `fastapi`, `uvicorn`, `psycopg[binary,pool]`, `pydantic` 2, `pyyaml`, `httpx`, `slack_sdk`, `mcp` (the official SDK), `cryptography`, `croniter`. Ask before you add another one. Tools: `ruff`, `pytest`, `pytest-asyncio`.
- **Tests use a real Postgres** and never a real Slack, GitHub, or model. `tests/conftest.py` gives: a fresh database for each test session (created in the Postgres at `ILLO_TEST_DATABASE_URL`), a loaded example workspace, a fake Slack client that records calls, a fake GitHub client, the fake brain runtime, and helpers to replay a Slack event. `scripts/test-db.sh` starts the shared test Postgres container.
- **Each safety rule and each contract has a test** before the code that depends on it merges.
- **Small files, plain code, type hints, docstrings that say why.** The text that people read (README, docs, comments, card text, the messages of Illo) is short and plain: one idea in each sentence, active voice, the same word for the same thing.
- **Nothing private in this repository.** No name, id, channel, or address of a real team. The examples use "Acme" and invented people.
- **Commits** end with a `Co-authored-by:` line for the agent that wrote them. The `main` branch refuses a pull request without it.

## Outside alarm

The outside watcher lives in `ops/external_deadman/`. The workflow
`.github/workflows/illo-deadman.yml` runs it about every five minutes.
It reads `heartbeat.json` from the `ops/heartbeat` branch of this repository.
`illo/routines/heartbeat.py` publishes that file when `heartbeat.repo` is set
(see `docs/github.md`). The file has `ts` (an ISO-8601 UTC time), `last_run_id`
(a positive integer or null), and `last_surface` (`illo`).
The watcher alarms after 12 minutes without a fresh heartbeat. It keeps its
state in `deadman-state.json` on the same branch. It sends the notice before
it writes the new state. A failed write can cause a duplicate notice.

## Job watchdog

The watchdog checks job reports every 60 seconds on its own database connection.
It measures from the last report or the process start, whichever is later.
A job is silent after more than 10 job intervals, with a minimum of 10 minutes.
The watchdog sends one alert and one recovery message by Slack DM to active workspace owners
(`role: owner` in `people.yaml`).
If no owner gets the alert, the watchdog tries again at the next check.
`/healthz` returns HTTP 200 when the database answers and shows `ok`, `silent`, or `unknown` in `jobs`.
`illo doctor` lists silent jobs.

## The brain tool endpoint

`POST /brain/mcp` implements a small JSON-RPC subset directly: `initialize`,
`ping`, `tools/list`, `tools/call`, and initialization notices. It uses
stateless JSON responses. Each request checks the per-run bearer token, so
each tool list belongs to one authenticated run. The server counts the tool
calls of each run and stops at `runs.tool_limit`, the limit of the run's
route. A run with no stored limit gets no tool call. The brain service also
limits the model turns. Rule 10 tests check the server tool list and the tool
list of each real runtime.

The reload route is loopback-only and requires `ILLO_RELOAD_TOKEN`. File
checks reload the workspace without that route. The workspace schemas and
defaults are in `docs/workspace.md`.

## Task storage and authority

Migration `1200_tasks.sql` adds `tasks`, `task_events`, a token's `agent_key`, and the audit target.
`illo/ledger/tasks.py` owns all task changes and record events. Each change uses one transaction.
No code keeps a transaction open during a model run. The tools of the run can need the same locks.
A brief ends its transaction before its run, and takes its lock again before it sends.
Postgres ends a transaction that stays idle for one hour. This releases its locks.
`illo/tasks/checks.py` owns the named check registry. Connectors register checks without changes to task code.
`illo/tasks/lists.py` loads the team's list and agent files through the workspace models.
Task reads enforce visibility in SQL. Outsiders and tainted runs cannot call task tools.
A team-channel run keeps its team principal. Its verified sender supplies the task actor through the stored event.
This does not change the authority of other tool families.

`illo/tools/tasks.py` registers the Operator tools. Task actions use the ledger destination and need no approval card.
Person DM and team-channel profiles have the write tools. Routines read tasks and can write page blocks when pages are on.
Agent requests add capture and chat. Direct agent task tools need the `agent` token scope.
Each task tool call has an audit row. Only ledger code writes record events.

The existing scheduler sends pending task messages after their transactions commit.
`illo/channels/slack/tasks.py` builds task threads with the shared card helpers.
A failed send keeps the pending key. Stable message IDs support a retry.
The task core contains no knowledge of a specific outside tool.

Migration `1201_task_wake.sql` adds wake results, liveness stamps, the durable wake outbox, and the unique ticket mapping.
Task events queue wakes inside the task transaction. The scheduler delivers them after commit.
The release check uses one advisory lock. It queues restarts and reminders through the same task record.
The Slack sender enforces the reminder window again when it retries a send.
`illo/tasks/connectors.py` holds the wake, signal, link, and lane registries.
Installed connectors register before workspace validation. The core calls their functions without an outside-tool import.
The GitHub connector owns issue wake actions, task signals, proof checks, alert capture, and collector matching.


## Task pages

Migration `1800_task_pages.sql` adds three tables:

| Table | State |
|---|---|
| `web_signins` | Browser request and pairing hashes, expiry, person, consent, and Slack card identity. |
| `web_sessions` | Session token hash, person, creation, expiry, and last access. |
| `task_pages` | One current block list per task, author, result version, and write time. Migration `1801_task_page_event.sql` adds `last_event_id`, the last task event that the writer could see. |

A whole-page write replaces one JSON list in one transaction. The task lock serializes writes and decisions.
The page keeps no block history. The task keeps its existing event history.
The browser uses a random request cookie to exchange one Slack approval for a session.
Only the person who sent the code can approve the card. The browser session lasts 30 days.
The Slack button **Open the page** makes a request that is approved from the start, for the person who pressed it.
Its key is in the link, not in a cookie. The same one-time exchange gives the session. Each person has 20 links in one hour.
The scheduler removes expired requests and sessions every five minutes.
Global and per-host start limits and a pending-request cap bound the code requests. The limit of 20 links for each person in one hour bounds the link requests.
The existing agent OAuth flow keeps its own state and behaviour.

| Route | Purpose |
|---|---|
| `GET /web/signin` | Show the start button, the code, or the result. It makes no request. Only local task, request, and board paths are accepted destinations. With the `key` of a link, show whose link it is and a Continue form. |
| `POST /web/signin` | Make one sign-in request for this browser, then show the code. With the `key` of a link, exchange the key for a session. |
| `POST /web/signout` | Delete the session after a CSRF check. |
| `GET /web/board` | Show the signed-in person's three board columns. |
| `GET /web/board.js` | Serve the canvas script with no cache. It has no person data and needs no session. |
| `GET /web/app.css` | Serve the page stylesheet with no cache. |
| `GET /t/{id}` | Show a task visible to the signed-in person. |
| `GET /t/{id}/progress` | Show computed task progress. The frame reads itself each 15 seconds while the task is in work. |
| `GET /t/{id}/file/{position}` | Read one repository or stored picture through the same visibility check. Positions start at zero. |
| `GET /web/a/{id}/file/{position}` | Read one stored picture for a signed-in participant of the request. Positions start at zero. |
| `POST /t/{id}/decide` | Call the existing Done, reopen, or message function. |

These routes return 404 when `pages.enabled` is false. All HTML uses no-store and a strict CSP.
Task and request pages contain no script or inline style. Agent text is escaped data.
The board alone adds `script-src 'self'` to the page CSP and loads `/web/board.js`.
It contains no inline script, inline style, or outside request. The script sets its transform through CSSOM.
The server renders the full board. Forms and page scroll work without the script.

| Tool | Risk | Profiles when pages are on |
|---|---|---|
| `page_read` | `read` | `person_dm`, `team_channel`, `routine`, `agent_request`; deep runs with task reads. |
| `page_write` | `write_internal` | `person_dm`, `routine`, `agent_request`; deep requests and the current task of a wait check. |
| MCP `task_page` | `write_internal` | The `agent` token scope, for a task held by that token's agent. |

`page_read` takes `task`. The two writes take `task` and `blocks`.
The shared block schema is in `illo/tasks/pages/schema.py`.
Page tools are absent when pages are off. Direct calls also fail.
Wait checks can write only their bound task page, even after a web read.
This exception changes no task state and grants no other task write.

## Message board coordination

The `board` event channel connects the coordinator to the gateway.
A scan records an episode for each question, person mention, or turn cap that still needs help.
The coordinator claims a thread with a time-limited lease and reserves each model attempt in Postgres.
A separate job sends due Slack messages and uses Slack metadata to find an uncertain send.

Board changes lock the thread first, then episodes, escalations, and asks in that order.
Budget locks follow these row locks. Ask-only card refreshes do not take board row locks.

| Advisory lock | Use |
| --- | --- |
| `12000010` | Scan and claim coordinator work. |
| `12000011` | Claim and reconcile Slack delivery work. |
| `12000012` | Reserve coordinator and escalation budgets. |
| `12000013` | Claim board wake work. |

A card send reserves an ask ID before Slack receives it.
The ask row and the Slack receipt commit together.
A board reply checks the Slack sender, DM channel, and root message before it writes a human post.
