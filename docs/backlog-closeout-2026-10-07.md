# Backlog closeout — 2026-10-07

The starting queue contained 26 issues and seven PRs. PR #930 combines the source work from #904, #906, #909, #910, #911 and #921 with the remaining fixes. Each earlier PR head is an ancestor of the published branch; those six PRs are closed as superseded. Eight tickets are closed after completed decisions, recovery evidence or explicit retirement. The user will merge and redeploy. Passing tests are pre-deploy evidence; runtime acceptance stays open until its original symptom is verified on the new image.

## Disposition of every starting issue

| Issue | Resolution and remaining acceptance |
|---|---|
| [#670](https://github.com/Illospace/illospace/issues/670) | Extract identifiers and subject phrases before generic prose, with bounded scanner work. Preserve the established content identity. The reported dev924 is not the current production924: the named company/requester facts are absent from the production text. Verify the reported shape in a synthetic ingest fixture; only repair an old note after checking its canonical source, never by copying a dev node number into production. |
| [#723](https://github.com/Illospace/illospace/issues/723) | Measurement and decision completed; ticket closed. Measured 16 source-verified live cases through both recall engines. Retain memory; the deletion gate does not cover documents, Domain records or private owner memory. See [decision](memory-recall-decision.md). |
| [#770](https://github.com/Illospace/illospace/issues/770) | Carry documented caller metadata through storage. Work intake owns model-policy parsing; caller data cannot supply Cycle authority. Verify stored event and admitted run policy after deploy. |
| [#817](https://github.com/Illospace/illospace/issues/817) | Retired by Reda. All eight old laptop routines are paused in Claude. No migration is planned. |
| [#819](https://github.com/Illospace/illospace/issues/819) | The old routine architecture migration was retired, not executed. Active Illo coordinator and Reflex Cycles continue with their current guidance. |
| [#821](https://github.com/Illospace/illospace/issues/821) | Release radar remains paused; its executor migration was retired. |
| [#822](https://github.com/Illospace/illospace/issues/822) | Retired migration epic. Past definitions and results remain available. |
| [#829](https://github.com/Illospace/illospace/issues/829) | Reda retired the old public addresses. Closed as not planned. Private MCP works; no repository client configuration uses the retired addresses. See [retirement decision](public-endpoint-repair.md). |
| [#869](https://github.com/Illospace/illospace/issues/869) | Persist credential expiry on its connection, stop repeated refresh, alert one transition and clear on replacement. Transient failure leaves health unchanged. Verify reauthentication and a real expired episode after deploy. |
| [#874](https://github.com/Illospace/illospace/issues/874) | Historical 502 incident recovered. This session received an event ID and a satisfied preservation receipt with mutated refs. Closed with that evidence. |
| [#897](https://github.com/Illospace/illospace/issues/897) | Validate the complete submission before acknowledgement; reject oversize messages, parts or file references with numeric size diagnostics. No accepted text is silently clipped. |
| [#900](https://github.com/Illospace/illospace/issues/900) | Apply every matching configured projection of an inbound event. Source sync updates GitHub state and timestamps; it preserves editorial status, assignee and release evidence. In particular, closing a GitHub issue cannot mark pending production work Done. Reconcile old tracker rows from current GitHub state after deploy; dry-run replay does not repair them. |
| [#903](https://github.com/Illospace/illospace/issues/903) | Carry artifact paths through the submission prompt and preservation attribution. Verify a deployed receipt for supplied files. |
| [#905](https://github.com/Illospace/illospace/issues/905) | Writer identified: completed Cycle run 26331, occurrence 5141, used agent manage_domain actions 55398–55404 to create 6854–6860. Domain events 22435–22441 match the reported timestamps. Serialize external identity upserts, use the canonical earliest record and archive duplicates through the same service after deploy. Duplicate-only repair preserves the canonical editorial fields. |
| [#907](https://github.com/Illospace/illospace/issues/907) | Current workspace.search, cycles.inspect and seven knowledge.get reads succeeded. Historical transport incident closed; the old public addresses were separately retired. |
| [#908](https://github.com/Illospace/illospace/issues/908) | Use non-blocking per-run advisory admission so one busy run cannot starve the deadline sweep. Verify sweep progress after deploy. |
| [#914](https://github.com/Illospace/illospace/issues/914) | Return an authorized exclusion reason instead of claiming missing data. Read-only audit: 4214, 4458, 5366, 5549 are active private content nodes, with no shared mirrors and no supersession. Their satisfied receipts did not lose the nodes. |
| [#915](https://github.com/Illospace/illospace/issues/915) | Read-time visibility and canonical eligibility checks block stale shared mirrors after a node becomes private. Verify both search and get after deploy. |
| [#916](https://github.com/Illospace/illospace/issues/916) | Preserve intentional first-sentence reuse, enforce owner and visibility scope, and disclose reused/stored text. The corrected acceptance is authoritative; different same-heading text does not change the identity rule. |
| [#917](https://github.com/Illospace/illospace/issues/917) | Preserve bounded tool-failure diagnostics, test first-call batches in fast context, and clarify the safe-tool allowlist. The historical failing payload was not recovered; deploy verification must name its actual rejected tool or argument. |
| [#918](https://github.com/Illospace/illospace/issues/918) | Return one canonical final answer by default; detailed payloads are explicit. Verify compact get_result with a large deployed answer. |
| [#919](https://github.com/Illospace/illospace/issues/919) | Resolve inbound thread handles through their owner; reject malformed UUIDs before querying and redact SQL error diagnostics. Verify an inbound thread through MCP after deploy. |
| [#920](https://github.com/Illospace/illospace/issues/920) | The live feed has a GitHub Events projection but no Domain 1 tracker projection. Add validated issue/PR tracker projections while preserving the feed, then backfill website rows from current GitHub snapshots after deploy. The setup and recovery commands default to preview; applying recovery requires an unchanged reviewed plan. Run duplicate-only repair separately from source reconciliation. Only completed Cycle runs advance the feed watermark; migration 0068 also adds the missing-record rule when later comments hide an opened event. |
| [#926](https://github.com/Illospace/illospace/issues/926) | Own pending event tasks, provide flush_event_writes, drain during API shutdown and handle cancellation without an unhandled callback. One-shot callers must await flush before closing the loop. |
| [#928](https://github.com/Illospace/illospace/issues/928) | Inherit launch provenance only from the persisted occurrence's own lineage, for generic and chantier continuations. Two hops retain one occurrence and its existing notification ledger; unrelated runs fail closed. |
| [#929](https://github.com/Illospace/illospace/issues/929) | Emit a structured heartbeat failure and redact/bound command exception diagnostics before alerts. The original cause was upstream GitHub 500; the retry succeeded. |

## Runtime retirement

Cycle 9, Uwear Backend Promotion Readiness, is disabled. The scheduler job `uwear_staging_promotion_pr` is paused on the server and removed from the source catalog, so startup catalog sync cannot enable it again. Historical rows and results remain.

Claude shows dispatcher, SEO, usage digest, R3 worker, R2 evaluator, release radar, outbound and ads all paused. The latter five were already paused; the first three were stopped in this session. No scheduled task was deleted.

## Validation

The imported base passed 5670 fast tests. Runtime additions passed 104 event/heartbeat/scheduler checks and 29 continuation/Cycle-gate checks. The current integrated base passed 5678 fast tests in the independent review. A clean PostgreSQL instance ran the full Alembic chain through 0068 and 93 database tests. New slice results and final integrated gates are recorded below when complete.

The tracker and submission slice passed 670 tests with 4 skips. Its final independent review found no actionable issue and passed 440 selected tests. The credential concurrency regression passed against real PostgreSQL: expiry waiters share one connection episode and one alert; a stale failed refresh cannot replace a committed new sign-in.

After deployment, use [the tracker recovery runbook](github-tracker-recovery.md) to add the missing projections, repair duplicates, and then reconcile fresh GitHub state. Inspect each original symptom before closing its ticket. Deployment alone does not repair historical rows.

No production image was changed, no PR was merged, and no credential or raw private corpus was committed. The old public addresses were retired; source fixes still require deployment and runtime verification.
