# Illo: architecture and build contract

This file is the contract of Illo. Each builder reads it first. It is public: it has no data of any team.

## 1. The product

Illo is one assistant for a whole team. People talk to it in Slack and by email. It knows each lane of agent work on each machine. It answers "give me an update on X". It sends a person a card only for a real blocker, and it sends the answer back to the agent. It carries requests between people. It starts clear work through a GitHub ticket. It finds the team context where that context lives, and it keeps the decisions.

Design goals, in order:
1. **Safe by code, not by prompt.** The ten safety rules of section 4 live in server code and have tests.
2. **Small.** A person can read the whole engine. No web app. No ORM. No queue service. One Postgres.
3. **Easy to install.** A new team installs it with the README in less than 30 minutes: one install command, a Slack app from a manifest file, `people.yaml`, a model sign-in, one collector.
4. **Truth comes from the source system, not from the model.** A lane state, an ask state, and a card are data in the ledger. The model reads them through tools. It never invents them.

## 2. The words

Each word has one meaning. Use these words in code, in docs, and in the text that Illo writes.

- **Person**: a team member, with a Slack id, a GitHub login, and email addresses. Defined in `people.yaml`.
- **Principal**: who a run acts for. One of: `person:<key>`, `team`, `outsider`. The gateway sets it. Nothing else can change it.
- **Lane**: one stream of agent work (a T3 thread, a Claude Code session, a GitHub issue or pull request). It has a source, an owner, a state, a summary, and links.
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
| **Ledger** | Postgres. People, tokens, events, runs, audit, lanes, asks, rules, items, drafts, signals, claims, connector grants. Plain SQL functions, no model calls. | `illo/ledger/`, `illo/migrations/` |
| **Brain** | One agent run for each event. A standard agent runtime in a container with no host mount, no shell tool, no file tool, and no web tool. It sees only the Illo tool server, through a token that is valid for that run. | `illo/brain/` (client and run manager), `brain/` (the sandbox service) |
| **Tool server** | An MCP server for the brain. Each call passes three checks: scope, destination, risk. Each call writes one audit row. | `illo/tools/` |

Processes (Docker Compose, `deploy/compose.yaml`):
- `postgres`: Postgres 16. It joins only the internal `db` network.
- `illo`: one Python process (asyncio): the HTTP server, the Slack connection, the timers, the gateway, the tool server. It joins `db`, the internal `brainnet` network, and the default network for outbound traffic.
- `brain`: the sandbox service. HTTP `POST /run` streams the events of one run. It has no volume from the host and no Docker socket. It joins `brainnet` and the default network. It never joins `db`. The install does not limit its outbound destinations; an egress proxy is an optional step in `docs/install.md`.

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
| `person_dm` | a person in a DM, an authenticated email, a person routine | `person:<key>` | all tools |
| `team_channel` | a team member in a configured channel | `team` | team reads, tickets, `work_start`, `asks_create`, `slack_read_thread` |
| `routine` | a team routine | `team` | team reads, tickets, `work_start`, `asks_create`, `slack_send_dm` |
| `agent_request` | an `illo_submit` request from an `agent` token | `person:<owner of the token>` | team reads, tickets, `work_start`, `asks_create`, `asks_answer_with_rule`, `notes_propose`, `email_draft` |
| `tainted` | a bot, webhook, or form post; a forwarded email; a signal; a Slack thread with outside text | `team`, or the person of a forwarded email | exactly 8 tools (below) |
| `outsider` | an email from an unknown or unauthenticated sender | `outsider` | `web_read_public_page` |

"Team reads" are the lane, ask, rule, people, search, notes, skills, shared Drive, GitHub read, connector, and public web tools. The personal tools work only in `person_dm`. `asks_answer`, `asks_cancel_mine`, `rules_remove_mine`, `asks_digest`, and `slack_post_channel` also work only in `person_dm`.

The `tainted` profile has exactly these tools: `github_search_issues`, `github_get_issue`, `github_create_issue`, `claims_take`, `web_read_public_page`, `connectors_list`, `connectors_call`, and `skills_get`. A tool that names `tainted` but is not in this set is still refused. With these tools a tainted run reads only issues of the configured repositories, connector results, and public pages. It cannot read lanes, asks, people, rules, notes, search results, Slack, or pull requests. Its tickets have no assignee and the label `from-outside-text`. A person assigns them. A connector tool needs `tainted: true` and risk `read`. A tainted run reads one public page: the first URL of its event text, once.

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
- **Check 3, risk.** `read` → allow. `write_internal` (a ticket, a note, a lane update, a message to a team member) → allow. `send_outside` → draft. There is no tool for a destructive action.

The run gets `personal_read = true` after a personal tool, and after a read result with `visibility: private`.

A check returns `allow`, `draft`, or `deny`. The strictest result wins. `draft` creates a row in `drafts` and a card for the person of the run. A run with no person cannot create a draft: the result is `deny`. The tool result tells the model what happened. Each call writes `audit` with: run, principal, tool, an HMAC digest of the arguments, the redacted arguments, the decision, the reason.

Risks of note: `asks_answer`, `email_draft`, `github_assign_issue`, and `github_comment_issue` have risk `send_outside`. Assignments through `work_start` and agent trigger labels also need approval. Without a person, tickets stay unassigned and drop model labels. A GitHub write to a public repository becomes `send_outside` in its preflight.

### Limits in one run

`RUN_LIMITS` in `illo/tools/registry.py` counts the `audit` rows of the run: at most 3 `asks_create`, 3 `slack_send_dm`, 3 `github_create_issue`, 5 `github_comment_issue`, and 1 `work_start`. All tool calls of one run stop at `brain.max_turns`. The run stops at `brain.timeout_s`.

Each Slack channel starts at most 30 tainted runs per hour. Owner notices have a cap of one per run and six per hour; remaining links wait for an hourly digest.

## 5. The ledger

Postgres 16. Plain SQL in `illo/migrations/NNNN_<area>_<what>.sql`, applied in file order by `illo migrate`. The foundation owns `0001_init.sql`. A later change uses the number block of its area: slack 0100, mcp 0200, github 0300, search 0400, asks 0500, brain 0600, collector 0700, routines 0800, email 0900, connectors 1000, notes 1100. A migration never edits an older file.

The main tables (each has `id bigint generated always as identity`, `created_at timestamptz default now()` unless the text says otherwise):

- `people`: `key` (unique), `name`, `slack_id`, `github_login`, `emails text[]`, `role` (`owner`, `member`), `timezone`, `areas text[]`, `active`. The file `people.yaml` is the truth. The table is a copy that the loader keeps equal.
- `tokens`: MCP client tokens. `person_id`, `label` (the machine or the agent), `token_hash` (SHA-256), `scopes text[]` (exactly one of `collector`, `agent`; default `collector`), `last_used_at`, `revoked_at`.
- `events`: the gateway inbox. `channel`, `external_id`, unique (`channel`, `external_id`), `kind`, `principal`, `person_id`, `tainted`, `payload jsonb`, `status` (`received`, `ignored`, `handled`, `failed`).
- `runs`: `event_id`, `principal`, `person_id`, `profile`, `tainted`, `personal_read`, `status` (`running`, `completed`, `failed`, `timeout`), `runtime`, `model`, `usage jsonb`, `error`, `started_at`, `ended_at`, `token_hash` (the per-run token), `token_expires_at`.
- `audit`: `run_id` (nullable), `token_id` (nullable), `principal`, `tool`, `args_digest`, `args jsonb` (redacted), `decision`, `reason`, `at`.
- `lanes`: `source` (`t3`, `claude-code`, `codex`, `github`, `manual`, `email`), `external_id`, unique (`source`, `external_id`), `title`, `owner_person_id`, `state` (`open`, `working`, `waiting_person`, `blocked`, `review`, `done`, `failed`, `unknown`), `summary`, `links jsonb`, `repo`, `machine`, `visibility` (`team`, `private`), `last_signal_at`, `updated_at`, `closed_at`, `end_summary`, `slack_channel`, `slack_ts`. A lane with state `working` and no signal for longer than the limit of its source shows as `stale`. `stale` is computed on read. It is never stored. `open` means backlog: the lane exists and is not closed, and nobody reported work on it. Default lane lists omit `open` unless a text query matches it or a state filter requests it. Active lanes appear first. Digest work counts include only fresh `working` lanes and `review` lanes.
- `lane_events`: `lane_id`, `at`, `kind`, `text`, `data jsonb`.
- `asks`: `lane_id` (nullable), `kind` (`decision`, `action`, `review`, `reminder`), `requester_token_id` (nullable), `requester_person_id` (nullable), `owner_person_id`, `question`, `options jsonb` (list of `{id, label, detail}`), `recommended_option`, `proof jsonb` (text and links), `due_at`, `expires_at`, `state` (`open`, `answered`, `expired`, `cancelled`), `answer_option`, `answer_text`, `answered_by_person_id`, `answered_via` (`person`, `rule`, `approved_draft`), `rule_id`, `answered_at`, `delivery_state` (`none`, `pending`, `delivered`, `acked`, `failed`), `delivery_attempts`, `delivered_at`, `acked_at`, `slack_channel`, `slack_ts`, `reminded_at`, `source_ref` (the thing in the source system that this ask mirrors, for the stale check).
- `rules`: `owner_person_id`, `scope` (`person`, `team`), `kind` (`always_allow`, `standing_answer`), `match jsonb` (ask kind, lane source, repository, requester, a question pattern), `answer_option_id`, `answer_option_label`, `answer_option_detail`, `text`, `origin` (`card`, `file`, `proposal`), `uses`, `removed_at`.
- `items`: the search table. `kind` (`saved`, `decision`, `note`, `lane_summary`, `archive`, `slack`, `github`, `doc`), `source_ref` (unique), `title`, `body`, `summary`, `owner_person_id`, `visibility` (`team`, `private`), `links jsonb`, `meta jsonb`, `source_at`, `tsv tsvector` (generated from title, summary, body; GIN index).
- `submissions`: the receipts of `illo_submit`. `event_id text` (unique, opaque), `token_id`, `idempotency_key`, unique (`token_id`, `idempotency_key`), `desired_outcome`, `envelope jsonb`, `envelope_digest`, `status`, `result jsonb`, `completed_at`, `run_id`.
- `drafts`: `run_id`, `approver_person_id`, `kind`, `destination`, `content jsonb`, `state` (`pending`, `approved`, `rejected`, `sent`, `expired`), `slack_channel`, `slack_ts`, `decided_at`.
- `signals`: `token_id`, `source`, `dedupe_key`, unique (`source`, `dedupe_key`), `severity`, `title`, `body`, `data jsonb`, `status` (`received`, `ticketed`, `ignored`), `ticket_url`.
- `claims`: `key` (unique), `run_id`, `note`. One claim for each alert or signal, so that two runs cannot file the same ticket. A model claim has the prefix `model:`. The minute timer deletes model claims older than 24 hours. Server keys, such as `routine:<name>:<time>`, have no prefix.
- `connector_grants`: `connector_key`, `person_id` (null for a team connector), `secret bytea` (encrypted), `scopes text[]`, `expires_at`, `revoked_at`.
- `secrets`: the vault. `name` (unique), `secret bytea` (encrypted with the key in `ILLO_VAULT_KEY`).
- `routine_state`: `key` (unique), `last_run_at`, `next_run_at`, `last_status`.
- `sensors`: `key` (unique), `last_ok_at`, `last_error_at`, `detail`. One row for each thing that must stay alive: the Slack connection, each collector, the GitHub poll, the brain.
- `kv`: `key` (unique), `value jsonb`.

Access to the database: `psycopg` 3 (async, with a pool). Each ledger area is one module of small functions that take a connection: `illo/ledger/people.py`, `lanes.py`, `asks.py`, `rules.py`, `items.py`, `audit.py`, `drafts.py`, `signals.py`, `tokens.py`, `runs.py`. No ORM.

## 6. The gateway and a run

1. A channel builds an `InboundEvent` (`channel`, `external_id`, `kind`, `sender`, `text`, `payload`, `reply_to`) and gives it to `gateway.receive(event)`.
2. The gateway inserts it into `events`. A duplicate stops here.
3. The gateway finds the principal:
   - A Slack DM from a known person → `person:<key>`, profile `person_dm`.
   - A Slack DM from an unknown sender → no answer (rule 7).
   - A message in a configured channel from a known person → `team`, profile `team_channel`. A message from a webhook, a bot, or a form → `team`, `tainted`. A message from an unknown user, or from a Slack Connect user of another workspace → no run. A private channel → no run.
   - An email with one aligned DMARC pass from a known address → `person:<key>`, profile `person_dm`. A forwarded email → the same person, `tainted`. Any other email → `outsider`, and the `unknown_sender` claim takes it before a run.
   - A team routine → `team`, profile `routine`. A person routine → `person:<key>`, profile `person_dm`.
   - An MCP request from an agent token → `person:<owner of the token>`, profile `agent_request`.
   - A signal → `team`, `tainted`.
4. Preparers run (`register_preparer`). They change verified input before any claim: Slack reads the transcript of a clip, and the Google sign-in code removes a callback address.
5. Claims run (`register_claim`). A claim takes an event that continues a flow of its area: a thread reply to an ask, a corrected note, a pasted sign-in address. A claimed event gets no brain run.
6. The gateway shows the first sign of work itself, in code, in less than one second: in Slack it posts a short placeholder message, and later it edits that message with the answer. The model is not in this path. In Slack this step also adds the thread history. If that history has a bot or an unknown user, the gateway makes the run `tainted` (`Gateway.context_authority`).
7. The run manager inserts a `runs` row, makes a per-run token (random, stored as a hash, valid until the run ends), and calls the brain service: `POST /run` with the system prompt, the message, the URL of the tool server, and the token.
8. The brain service streams events: `started`, `text`, `tool_call`, `final`, `error`. The run manager can show a tool call as a status line.
9. The gateway gives the final text to the channel that the event came from. Each other write goes through a tool and its checks.
10. The run ends. The token stops to work. `runs` gets the status, the usage, and the end time.

A run has a time limit and a turn limit from the config. A failed run gives the person a short honest message.

The brain interface (`illo/brain/client.py`): `async def run(request: RunRequest) -> AsyncIterator[BrainEvent]`. `RunRequest`: `run_id`, `system_prompt`, `user_message`, `mcp_url`, `mcp_token`, `runtime`, `model`, `max_turns`, `timeout_s`. A `fake` runtime in `illo/brain/fake.py` plays a script of tool calls. Each test uses it. The real runtimes live in the `brain/` service.

## 7. The tools that the brain sees

Tool names use `family_verb`. Each family is one file in `illo/tools/`. The registry finds each file by itself, so two builders never edit the same file.

| Family | Tools (first set) |
|---|---|
| `lanes` | `lanes_search`, `lanes_get`, `lanes_mine` |
| `asks` | `asks_list`, `asks_get`, `asks_create` (Illo asks a person), `asks_answer` (a draft for the ask owner), `asks_answer_with_rule`, `asks_cancel_mine`, `asks_digest` |
| `search` | `search_all` (items, lanes, GitHub, Slack, Drive and mail of the person; filters by principal), `search_get` |
| `people` | `people_list`, `people_get` |
| `slack` | `slack_send_dm` (to a team member), `slack_post_channel`, `slack_read_thread` |
| `github` | `github_search_issues` (open and closed), `github_get_issue`, `github_create_issue`, `github_assign_issue`, `github_comment_issue`, `github_get_pull` |
| `claims` | `claims_take` (the key gets the prefix `model:`) |
| `skills` | `skills_get` |
| `rules` | `rules_list`, `rules_remove_mine` |
| `notes` | `notes_search`, `notes_propose` |
| `web` | `web_read_public_page` (a URL of the event text or the root of its host; text only; no private address) |
| `connectors` | `connectors_list`, `connectors_call` (a team connector through the MCP client, with a risk level for each tool) |
| `personal` | `mail_search`, `mail_get`, `calendar_list`, `drive_search`, `drive_get` (personal connectors, read-only), `drive_shared_search`, `drive_shared_get` (the team shared drive) |
| `email` | `email_draft` (always a draft) |
| `work` | `work_start` (a GitHub issue plus a lane plus one Slack status message) |

Section 4 says which profile gets which tool.

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

**Slack** (`illo/channels/slack/`): Socket Mode, so the host needs no public address. The app comes from `deploy/slack/manifest.yaml`. Events: `message.im`, `app_mention`, `message.channels` and `message.groups` for the configured channels, `block_actions`, files. A private channel starts no run and enters no index. A channel is private when Slack says so or when `illo.yaml` sets `visibility: private`. Cards use Block Kit. A click on a button comes in as `block_actions`, the handler writes the ledger, and it edits the card. A card is built from the ledger row, and a check compares each open card with its row. An audio clip becomes text (Slack gives a transcript for a clip; if it does not, Illo says so).

Card types: the ask card (question, options as buttons, the recommended option first, proof, "Always allow" where a rule is possible), the draft card (Send, Do not send; the full text, with hidden characters shown as escapes), the digest ("3 need you, 14 run fine", one line for each sensor that is not healthy), the note card ("I noted: ...", Correct, Remove).

**Email** (`illo/channels/email/`): the mailbox is read with IMAP on a timer, so the host needs no public address. A sender has authority only with exactly one DMARC `pass` result whose `header.from` equals the From domain, in the trusted `Authentication-Results` header. The trusted header is the first one with the server name in `ILLO_EMAIL_AUTHSERV_ID`, or the topmost header with the explicit value `topmost`. A blank setting disables email. Outbound mail is always a draft card first.

**MCP**: section 8. **Signals**: `signal_submit`.

## 11. Sources and search

One function, `search(principal, query, kinds, limit)`, in `illo/sources/search.py`:
- `items` by full text (`websearch_to_tsquery`, `simple` and `english`), filtered by visibility: a private item shows only to its owner.
- `lanes` by title, summary, and events.
- GitHub issues and pull requests, live, through the GitHub client.
- Slack messages that Illo saw in the configured public channels (stored as items of kind `slack`).
- When the Google connector files exist: the shared drive, and the mail and Drive of the person (only in an untainted run of the owner).

Each result has a title, a short text, a link, the source, and the time. The brain can search several times with other words. Vectors are not in the first version.

The GitHub source (`illo/sources/github.py`) reads the configured repositories on a timer. An open issue or pull request becomes a lane with the source `github`. Auth: a GitHub App (id, private key, installation) or one token (`ILLO_GITHUB_TOKEN`).

## 12. Layout of the repository

```
illo/
  __init__.py  app.py  cli.py  config.py  db.py  workspace.py  vault.py  log.py
  migrations/
  ledger/        people lanes asks rules items audit drafts signals tokens runs
  gateway/       events principal dispatch replies
  brain/         client run_manager prompts fake
  tools/         registry checks + one file for each family
  channels/      slack/ email/ mcp/ signals/
  sources/       search github slack_index drive archive
  connectors/    mcp_client google oauth github_client
  routines/      scheduler digest reminders stale heartbeat
brain/           the sandbox service: server, runtimes, Dockerfile
collectors/      t3/  hooks/ (Claude Code and Codex)
deploy/          compose.yaml  Dockerfile  install.sh  update.sh  slack/manifest.yaml  env.example
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

## The brain tool endpoint

`POST /brain/mcp` implements a small JSON-RPC subset directly: `initialize`,
`ping`, `tools/list`, `tools/call`, and initialization notices. It uses
stateless JSON responses. Each request checks the per-run bearer token, so
each tool list belongs to one authenticated run. The server counts the tool
calls of each run and stops at `brain.max_turns`. The brain service also
limits the model turns. Rule 10 tests check the server tool list and the tool
list of each real runtime.

The reload route is loopback-only and requires `ILLO_RELOAD_TOKEN`. File
checks reload the workspace without that route. The workspace schemas and
defaults are in `docs/workspace.md`.
