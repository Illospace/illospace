# Illo handoff packets — built, proven, then deliberately retired

*Closed 2026-08-19. All seven slices shipped 2026-07-13 (PR #308 — the
squash commit's subject says "slices 01-02" but it contains all seven); the
automatic lane was retired in two deliberate steps: Slack briefs on
2026-07-30 (#619, "the ticket IS the handoff") and silent minting on
2026-08-06 (PR #706, merged in the #711 train). This record exists so nobody
rebuilds the lane, re-alarms on its designed zero, or re-walks its dead ends.*

## What it was

"No work arrives cold": at every routing moment Illo attached a gathered
dossier and a brief the assignee could hand to their own agents — a
`brain/systems/briefing/` package (gather → compose → mint → deliver →
outcomes, ~4,100 lines) that minted a **launch handoff** per completed triage
run and posted the brief into the Slack thread.

## What survives (and who owns it now)

- **The launch-handoff atom, whole.** `brain/systems/launch_handoffs.py`
  (`create_launch_handoff`, `codex_prompt_for_handoff`,
  `claude_prompt_for_handoff`), the REST routes in
  `brain/app/api/routers/launch_handoffs.py` (dark launch page, strict CSP,
  codex redirect, `/launched` counter), the `handoff.get`/`handoff.create`
  MCP capabilities, the `create_launch_handoff` agent tool, and the
  `launch_handoffs` table. An agent can still create a launch link **on
  demand**; only the automatic lane is gone. Status vocabulary stayed
  `open/launched/claimed/expired/archived` (`brain/contracts/statuses.py`) —
  the "do not invent `superseded`" invariant held to the end.
- **Infrastructure the feature built, re-owned by other consumers:**
  `collect_result_refs` (`brain/systems/inbound/attribution.py`) keeps
  attribution and preservation evidence from going blind
  (`brain/systems/runs/tools.py`); `slack_web_client_from_runtime` (the
  Vault-first token fix, #333) now serves the scheduler monitors; the
  `body_total_chars` honesty keys on GitHub reads now feed the knowledge
  connectors.
- **Tests:** `tests/test_launch_handoffs_idempotency.py`,
  `tests/test_thread_url_references.py`, `tests/test_external_agent_routes.py`,
  `tests/test_inbound_attribution.py`, `tests/test_inbound_reconciliation.py`
  (which gained direct receipt-transition coverage in #706 when the packet
  tests that had been covering it were deleted).
- **Migrations:** `0026_packet_brief_deliveries` still runs (it is
  load-bearing in the chain); `0052_drop_packet_brief_deliveries` drops the
  table again. Both are registered reviewed-destructive.

## Why it was retired

The delivery moment died first: Reda, 2026-07-30 — **the ticket IS the
handoff** (#619). A well-gathered GitHub issue already carries everything the
brief re-packaged, and the brief read "like an internal leak" in human
threads (#605). Minting then ran silently for a week (249 rows, 97 in the final week,
**zero ever launched by a human** — including before the brief retirement),
which settled the question with data: nobody used the launch links even when
they were delivered. #706 removed the lane.

The retirement is also a cautionary tale recorded in Illo knowledge (source
357 / node 987): a QA routine read the designed zero as a breakage, filed
#666, built an alarm (#675), and the alarm double-paged #alerts on a
condition that is permanently true by design. **A metric that worked and then
stopped on a specific day is a decision until proven otherwise.**

## Dead ends (do not re-walk)

- **Receipt-lane mint hooks.** The first mint hook fired only on
  `illo_triage` receipts — a lane that had been dormant for four days when
  the feature deployed, so production minted zero packets while every test
  was green (#332 later rewired it to the real lanes). Merge-is-live culture
  ships dormant gates silently; only a live E2E catches them.
- **Post-commit brief delivery via an outbox** (`packet_brief_deliveries`,
  #336): correct engineering — skip-locked claims, `claimed_at` fencing,
  after-commit fast path — for a message nobody wanted. Built 2026-07-16,
  bypassed 2026-07-30, dropped 2026-08-06.
- **An alarm on a designed-zero metric** (#675): built one day, fired wrong
  the next, deleted the same day.
- **The on-demand "brief me" caller was never built** — the package docstring
  claimed four callers (triage, notify, digest, on-demand) but no on-demand
  capability ever existed. The claim outlived the code.

## Provenance kept in-tree

- [`assets/launch-page-render-04.html`](assets/launch-page-render-04.html) —
  the slice-04 launch page rendered from a fixture row; the accepted
  screenshot-gate evidence (2026-07-13).
- [`assets/pre-merge-probe-05.md`](assets/pre-merge-probe-05.md) — the
  illo-dev read-only probe: three verbatim sample briefs plus the three
  real-data gaps the probe caught (bogus slack notes on GitHub-origin
  events, private-repo authority context, thin briefs).

## Leftovers ticketed at close

Three orphans survived #706 with no production caller —
`github_read_ref_for_backend` (no caller at all, not even a test), the
`parse_member_agent_targets` / `agent_target_for_member` pair with their
`ILLO_MEMBER_AGENT_TARGETS` env var, and `durable_work_refs` in
`attribution.py` (mint's predicate; #706 rewrote its docstring but gave it no
new owner — today only its own tests call it). Gather and mint were their
only consumers. Filed for removal as
[#842](https://github.com/Illospace/illospace/issues/842) rather than fixed
here.
