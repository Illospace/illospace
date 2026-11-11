# Collectors

A collector runs on the machine where agents work. It calls out to Illo.
Illo has no credential for that machine. The collector never opens an inbound port.
Each person uses their own Illo token and their own local T3 access.

The programs use Python 3.12, `httpx`, and the standard library.
They do not import the `illo` package. Copy the whole `collectors/` folder when you install them.

## Install the T3 collector

1. Copy `collectors/` to `~/.local/share/illo-collectors/collectors/`.
2. Create its Python environment:

   ```sh
   cd ~/.local/share/illo-collectors
   python3 -m venv .venv
   .venv/bin/pip install -r collectors/t3/requirements.txt
   ```

3. Create an Illo token on the Illo host:

   ```sh
   illo token create --person sam --label acme-laptop --scope collector
   ```

4. Store the token on the collector machine at `~/.config/illo/token`. Set mode `600`.
   Keep credentials outside every checkout. Never put a token in a TOML file or a shell command argument.
5. Create a T3 bearer token with the local T3 CLI. Grant only orchestration read and operate access.
   Store it at `~/.config/illo/t3-token`, with mode `600`.
   Run this command on your own machine, with your own T3 account:

   ```sh
   mkdir -p ~/.config/illo
   chmod 700 ~/.config/illo
   (umask 077
    t3 auth session issue --scope orchestration:read --scope orchestration:operate \
      --ttl 30d --label illo-collector --token-only > ~/.config/illo/t3-token)
   ```

6. Copy [collector.example.toml](../collectors/t3/collector.example.toml) to `~/.config/illo/collector.toml`.
   Set the MCP URL, T3 URL, and machine label. Use one Illo token for each collector.
   The label should match the token label. The owner defaults to the owner of that token.
7. Set the `projects` map. Its keys are T3 project IDs. Its values are repository names, such as `acme/app`.
   With no map, the collector reports all projects. Use a map to select what the team may see.
8. Test one poll:

   ```sh
   ~/.local/share/illo-collectors/.venv/bin/python \
     ~/.local/share/illo-collectors/collectors/t3/collector.py \
     --config ~/.config/illo/collector.toml --once
   ```

T3 tokens expire. Replace the local token, then restart the collector.
The collector never sends either token to the other server.

### Config

Environment values override the TOML file. All paths can use `~`.

| TOML key | Environment value | Default |
| --- | --- | --- |
| `mcp_url` | `ILLO_MCP_URL` | Required; include `/mcp` or `/api/mcp` |
| `token_file` | `ILLO_TOKEN_FILE` | Required |
| `machine` | `ILLO_MACHINE` | Host name |
| `t3_url` | `ILLO_T3_URL` | `http://127.0.0.1:3773` |
| `t3_token_file` | `ILLO_T3_TOKEN_FILE` | None; required for HTTP access and answers |
| `data_path` | `ILLO_T3_DATA_PATH` | None; optional T3 base folder or SQLite file |
| `state_file` | `ILLO_STATE_FILE` | `~/.local/state/illo/t3.json` |
| `poll_seconds` | `ILLO_POLL_SECONDS` | `30` |
| `projects` | `ILLO_PROJECTS` | All projects; environment form is a JSON object |
| `owner` | `ILLO_OWNER` | Owner of the Illo token |
| `environment_id` | `ILLO_T3_ENVIRONMENT_ID` | Local thread links; set it for cross-device links |
| `share` | `ILLO_SHARE` | `private`; use `team` to share reports with the team |

Use HTTPS for remote servers. Local loopback HTTP is supported.
The program disables proxy environment values and does not follow redirects with a bearer token.

### Run on a timer

For systemd, copy [illo-collector.service](../collectors/t3/illo-collector.service)
to `~/.config/systemd/user/illo-collector.service`:

```sh
systemctl --user daemon-reload
systemctl --user enable --now illo-collector.service
journalctl --user -u illo-collector.service
```

The unit uses the paths in the install steps. Edit the paths if you use another folder.
For cron, use one poll each minute:

```cron
* * * * * $HOME/.local/share/illo-collectors/.venv/bin/python $HOME/.local/share/illo-collectors/collectors/t3/collector.py --config $HOME/.config/illo/collector.toml --once
```

Use either systemd or cron. A file lock prevents concurrent polls for the same state file.
Keep the state file outside the checkout. It has mode `600`.
It contains hashes, request references, and delivery notes. It contains no message text or token.

### Reports and asks

Each poll sends changed lanes to `lane_report`, plus a collector liveness record.
An empty batch still reports liveness. Every 10 minutes, the collector refreshes every selected lane.
The collector sends the title, state, last assistant text, links, repository, and machine.
It cuts the summary to 500 characters. It sends no attachment, tool result, file change, or command output field.
It removes code blocks and common credentials from text. This filter cannot detect all private prose.
Reports use private visibility by default. Set `share = "team"` only for sessions that the team may read.
The next report also changes an existing lane to the configured visibility.
Use the token owner's person for private reports. Another owner requires `share = "team"`.

| T3 state, in priority order | Lane state |
| --- | --- |
| Running | `working` |
| Pending question or approval | `waiting_person` |
| Last run failed | `failed` |
| Archived, or a person settled it with no later unsettlement | `done` |
| Person-created thread, last run completed or finished | `review` |
| Agent step, last run completed or thread settled | `done`, with `is_step: true` |
| Other state | `unknown` |

A run is one agent turn. An agent that stops is not a proof that the work finished.
A `review` thread waits for the person's next move. Server and agent settlements do not close a person's work.
A running run takes priority over a pending request and an old settlement. The collector still reports each pending ask.
T3 can wait on another agent without a person request. The collector does not invent a question for that wait.
It reports `unknown` for `blockedOnThreadWait` when no request waits for a person.

T3 can settle a thread in three ways:

- `thread.auto-settle`, with a `server:auto-settle:` command ID: the server applies its own rule.
- `thread.settle`, with a `server:settle-after-run:` command ID: an agent asks to settle after its turn.
- `thread.settle`, with a plain UUID: the T3 app sends the command. This is the person's decision.

An MCP tool sends a settlement with an `mcp:` command ID. An agent sends it, so it does not close a person's work.
T3 records the actor of each event as the server. Thus the command ID is the only evidence of the sender.
The read-only SQLite source joins the latest settle or unsettle event to its command receipt.
A person settlement needs `command_type: thread.settle`, an accepted receipt, and a plain UUID.
The collector reads this evidence only for a thread that a person created.
A later unsettle event or `unsettledAt` revokes that proof.
The HTTP source has no verified actor evidence. It uses `review` for a person's completed thread, even when settled.
An archive still counts as `done` on both sources.

`createdBy: agent` marks an agent-launched thread.
`lineage.relationshipToParent: subagent` marks a delegated child thread.
Both become steps. The collector reports `is_step` and the parent thread ID as `parent_ref`, when T3 supplies it.
A person-created fork is not an agent step. A launched step can have no parent ID.
Unknown thread origins do not count as proof of completion.

The next report corrects old `done` lanes and clears their `closed_at` and saved end summaries when they reopen.
New state and step fields change the report hash, so the next poll normally sends this correction.
The full refresh sends all selected lanes every 600 seconds, plus at most one poll interval.
SQLite reads settle evidence on each poll. A change of the sender that changes the lane state changes the report hash.
No manual data repair is needed. Apply the lane migration before you update the collector.

One question with 2–5 options becomes an ask with those options.
The collector keeps a marked recommendation. Each source reference has this form:
`t3:<thread id>:<request id>`.
A plain yes/no start question gets `Continue` and `Stop`.
Other plain questions direct the person to the thread.
Several questions, multiple selection, and unsupported option counts also need the thread.

The MCP server requires at least two options. Manual asks therefore offer `Answer in the thread` and `Dismiss this card`.
Both choices leave the T3 request unchanged. A dismiss choice acknowledges only the Illo card.
When a request ends in T3, the collector calls `ask_cancel` once.

The collector calls `ask_pull` with `scope: "token"`. It checks the exact request again before delivery.
It sends this text:

```text
Answer from Sam (via Illo): Continue. Use the local checks.
```

For a live structured question, it sends that text through T3's structured answer interface.
For a plain question, it sends a queued thread message with a stable request ID.
It then records success locally and calls `ask_ack`.
A failed send gets no ack. A failed ack retries without another send.
If the request no longer waits, it records a stale note, sends nothing, and acks.
The ack contract accepts only `ask_id`, so delivery notes stay in the local state file.
An answer from a rule must select a proposed option and name the rule. It cannot add free instructions.
A free answer comes only from a person: a typed reply on the card, or a model draft that the person sent.

T3's MCP interface cannot approve a permission request. Those asks direct the person to the thread.
The collector never turns a permission approval into a plain message and claims success.
There is a crash window between remote acceptance and the local state write.
Stable message IDs and exact request checks limit duplicate delivery after a restart.

### T3 reads

The preferred source uses authenticated HTTP snapshots. T3 also exposes an MCP interface for answer delivery.
All T3 fields and routes stay in `T3Source`. Unknown schemas fail the poll and do not cancel asks or ack answers.
No live T3 delivery runs during the tests.

Set `data_path` only if you need the read-only SQLite fallback.
It reads one transaction with `mode=ro`, including the WAL.
It never writes to T3's database. Internal table names can change after a T3 update.
The fallback still needs a local T3 token for answer delivery.
Do not copy the T3 database to Illo.

## Hooks for Claude Code and Codex

Hooks report session lanes. They do not pull answers or approve permissions.
Run the installer with the collector Python environment on `PATH`:

```sh
PATH="$HOME/.local/share/illo-collectors/.venv/bin:$PATH" \
  ~/.local/share/illo-collectors/collectors/hooks/install.sh
```

The installer asks for the URL and token. In a terminal, it hides the token input.
For automation, pass `--url https://illo.example.com/mcp` and feed the token on stdin.
It stores the token at `~/.config/illo/token`, with mode `600`.
It backs up settings before a change. Existing Claude hooks stay in place.
If Codex has a notify program, the local worker also calls that program.
It preserves the previous notify value for uninstall. Repeated installation does not add duplicate hooks.
`CLAUDE_CONFIG_DIR` and `CODEX_HOME` can select other config folders.

The hook process detaches before it reads a token, calls git, or makes a network request.
It always returns success. The foreground path has a 300 ms budget.
Failed background reports do not stop the agent. Hooks do not provide the collector liveness check.

| Tool and event | Lane state |
| --- | --- |
| Claude `SessionStart`, `UserPromptSubmit` | `working` |
| Claude `Notification`: `permission_prompt`, `idle_prompt` | `waiting_person` |
| Claude `Stop` | `unknown`; the session can continue |
| Claude `SessionEnd` | `done` |
| Codex notify `agent-turn-complete` | `unknown`; the session can continue |

The title uses the first prompt when the hook supplies it. Otherwise, it uses the folder name.
Hook reports use private visibility by default, including the prompt and last assistant text.
Set `"share": "team"` in `~/.config/illo/hooks.json` to share them with the team.
Use a token with the `collector` scope. These hooks do not need the `agent` scope.
The repository comes from `git remote get-url origin`. The worker never reads a transcript.
Current Codex notify documents only `agent-turn-complete`. It does not expose permission or session-end events.
Use T3 if you need complete state and answer delivery.

Remove only this bundle's settings with:

```sh
~/.local/share/illo-collectors/collectors/hooks/uninstall.sh
```

The token stays in its file. Uninstall preserves a notify program that the person changed after installation.
The docs checked for these payloads are [Claude Code hooks](https://code.claude.com/docs/en/hooks)
and [Codex advanced config](https://developers.openai.com/codex/config-advanced/).

## Write a collector for your own tool

Implement this source interface. The rest of the T3 collector depends only on these values:

- `list_threads() -> list[Thread]`: return a complete snapshot of selected work.
- `get_thread(id) -> Thread | None`: read the current state before delivery. Return `None` only after a successful read.
- `deliver(thread, pending, answer, message) -> None`: deliver the answer or raise `CollectorError`.
- `token`: local source credential, used only to remove that exact value from outbound text.

`Thread` has an ID, title, lane state, last assistant text, link, project, activity time, and pending requests.
`Pending` has a stable request ID, question, options, recommendation, and any local delivery fields.
Mark requests `manual` when the source cannot accept an answer safely.
Keep machine access inside the source class. Reuse the MCP client, strict field lists, state lock, and delivery journal.
For another source name, also change the `source` and source-reference prefix in the small collector loop.
Never make Illo connect back to the source machine.

## Tests

Run the collector tests with HTTP mock transports:

```sh
python -m pytest tests/collectors
ruff check collectors tests/collectors
```

The tests cover state mapping, changed lanes, refresh, asks, stale answers, retry, credentials, hooks, and settings merge.
They call no real Slack, GitHub, model, or T3 agent.
