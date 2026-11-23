# Extend the foundation

Use a module's `setup(ctx)` hook. `illo/app.py` already calls each hook.
The context has `config`, `pool`, `workspace`, `registry`, `gateway`, `slack_client`,
`github_client`, `brain_client`, `app`, and `scheduler`.
The Slack and GitHub clients can be `None`. The channel builder creates its client.
Use `ctx.every(seconds, fn)` or `ctx.cron(expr, fn)` for async jobs without arguments.
The scheduler reports each job through `sensors`. It cancels jobs at shutdown.

## Add a tool

1. Fill the family file in `illo/tools/`. Export `register(registry)`.
2. Define a Pydantic arguments class from `Arguments`. Extra arguments are forbidden.
3. Register a `Tool` with its name, family, risk, destination function, personal flag, handler, and arguments class.
4. Use only `read`, `write_internal`, or `send_outside`. There is no destructive risk.
5. Read authority from `ToolContext.run`. Use `ToolContext.principal` for ledger visibility filters.
6. State the grant on the tool: `profiles=frozenset({"person_dm", ...})`. The profile names are `person_dm`, `team_channel`, `tainted`, `routine`, `agent_request`, and `outsider`. A tainted run uses only the profile `tainted`. Give `tainted` and `outsider` only to a tool that is safe when hostile text drives the run. A tool without `profiles` uses the default data in `illo/tools/checks.py`; do not edit that data.
7. For personal connectors, load the grant by `ctx.run["person_id"]`. Never accept the owner from tool arguments.

The registry discovers family files. A handler receives `(tool_context, arguments)`.
The context contains the active connection, the run, and the app context.
The HTTP boundary holds a lock on the run through the call. Calls in one run execute in order.
A failed handler rolls back its changes. The call still creates an audit row.
A draft records the action without running the handler. The asks area sends the DM card.
Audit rows redact all argument values. The digest covers the full arguments.

## Share hooks, not files

- **Slack calls.** `ctx.slack_client` is a `slack_sdk` `AsyncWebClient`, or `None`. Call its own methods (`chat_postMessage`, `chat_update`, `conversations_open`). `illo.slackapi.dm_person(ctx, person_key, text, blocks)` sends a DM to a team member.
- **Card buttons.** `ctx.slack_actions.register(prefix, handler)` routes a click by its `action_id` prefix. The handler is `async handler(ctx, person, action)`. The Slack channel calls `ctx.slack_actions.dispatch`.
- **Thread replies and other follow-ups.** `ctx.gateway.register_claim(claim)` with `async claim(ctx, event, authority) -> str | None`. A claim takes an event before any brain run. Use it for a typed answer to an ask, a corrected note, or a pasted sign-in address.
- **Approved drafts.** Put `async fn(ctx, draft_row)` into `ctx.draft_performers[kind]`. The asks area calls it when the approver clicks Send.
- **The ledger is the interface between areas.** Write a row. The area that owns the next step finds it with its own timer.

Message preparation has a fixed stage before all claims, after the gateway resolves the principal.
Use `ctx.gateway.register_preparer(fn)` with `async fn(ctx, event, authority) -> InboundEvent | None`.
Return a changed event or `None`. Preserve the event identity and authority metadata.
Slack reads clip transcripts here. It adds thread history only after claims pass.

## Add a migration

Create a new SQL file in `illo/migrations/`. Do not edit `0001_init.sql` after it merges.
Use your area's number block from `docs/architecture.md`. The runner sorts files by name.
Each file has its own transaction. A database session lock prevents two runners from racing.
Ledger functions take a psycopg async connection. The caller owns the transaction.
`db.one` and `db.all_rows` return mappings. JSON parameters use `psycopg.types.json.Jsonb`.

## Add a channel

Implement `show_working(event)`, `deliver_final(event, text)`, and `deliver_error(event)`.
Register the adapter with `ctx.gateway.register(channel, adapter)` in the setup hook.
Pass `InboundEvent` to `ctx.gateway.receive(event)`.
Adapters must verify channel signatures and derive bot, webhook, form, and mention metadata from the channel API.
They must not copy these flags from message text.
MCP events put the verified client bearer in `sender`. The gateway verifies it again.
The gateway never stores that field. Do not put a token in `payload`.
A direct action uses `ctx.gateway.register_direct(channel, kind, handler)`.
The handler receives `(ctx, event, authority)`. It must check action ownership.
Unknown senders stop before direct actions. A direct action never runs the brain.

## Write a test

Use `conn` for ledger tests. It supplies the example people.
Use `app` or `http` for app tests. Seed data through `app.state.ctx.pool.connection()` and commit before an HTTP call.
Do not hold an uncommitted `conn` fixture while another pool connection needs the same rows.
Use `rpc_token` and `rpc` for brain tool requests.
Use `replay` to send a Slack DM through the gateway. Override any `InboundEvent` field with a keyword argument.
`channel` records working, progress, final, and error calls.
`fake_brain` calls the real HTTP tool endpoint. Set its script or final text for the test.
`slack_client` and `github_client` record calls. Extend `tests/fakes.py` when your area needs another method.
The `pool` fixture truncates the session database before a test. The session creates a random database and drops it at shutdown.
Never stop or delete `illo-test-pg` from a test script.

## Outside text

An outsider has no team authority. Its tool list contains only the public-page
tool. Do not expose ledger data through an outside reply.
A tainted run has an exact tool set in `GRANT_PROFILES["tainted"]`. A new tool
that names `tainted` in `profiles` stays refused. Only a reviewed safety change
adds a tool to that set.

## Keep brain tool results small

List tools use the row helpers in `illo/tools/_format.py`. A handler can return
`Compact(text, private=True)`. The registry reads this flag before it removes it.
SQL must still filter every private row by the principal. Counts need the same
filter and private flag, including rows beyond the page.

The registry caps the encoded result at 12,000 characters, including JSON escapes.
The brain MCP endpoint puts data only in `content`. Its `structuredContent` has
only `decision` and `reason`. The agent MCP endpoint keeps its existing contract.
The OpenAI runtimes forward only text content. Claude uses the MCP result through
the Agent SDK. Outside text must keep its data marker after a format change.
Google list offsets select rows from one source page. Mail reads at most 50 rows.
Narrow the query or the date range when the source has more results.

`digest_snapshot` reads the person's task brief and open asks. Only a workspace owner gets service faults.
It has risk `read`, uses the `person_dm` profile, and marks a personal read.
The team snapshot reads only team-visible tasks. Lane tools supply raw run evidence.

Run `scripts/test-db.sh`, then `.venv/bin/python scripts/measure_tool_results.py`.
The script creates and deletes its own database. It uses invented text and fakes.
Use `--output before.json`, then `--before before.json` after a change to compare.
Estimates use characters / 4. They measure tool results, not model billing.
