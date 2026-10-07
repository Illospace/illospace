# Backlog closeout — 2026-10-07

The starting queue contained26 issues and seven PRs. PR#930 combines the source work from#904,#906,#909,#910,#911 and#921 with the remaining fixes. The user will merge and redeploy. Passing tests are pre-deploy evidence; runtime acceptance stays open until its original symptom is verified on the new image.

## Disposition of every starting issue

| Issue | Resolution and remaining acceptance |
|---|---|
| [#670](https://github.com/Illospace/illospace/issues/670) | Extract identifiers and subject phrases before generic prose, with bounded scanner work. Preserve the established content identity. Reingest historical924 through the existing ingestion service after deploy to attach new cues without replacing its content. |
| [#723](https://github.com/Illospace/illospace/issues/723) | Measured16 source-verified live cases through both recall engines. Retain memory; the deletion gate does not cover documents, Domain records or private owner memory. See [decision](memory-recall-decision.md). |
| [#770](https://github.com/Illospace/illospace/issues/770) | Carry documented caller metadata through storage. Work intake owns model-policy parsing; caller data cannot supply Cycle authority. Verify stored event and admitted run policy after deploy. |
| [#817](https://github.com/Illospace/illospace/issues/817) | Retired by Reda. All eight old laptop routines are paused in Claude. No migration is planned. |
| [#819](https://github.com/Illospace/illospace/issues/819) | The old routine architecture migration was retired, not executed. Active Illo coordinator and Reflex Cycles continue with their current guidance. |
| [#821](https://github.com/Illospace/illospace/issues/821) | Release radar remains paused; its executor migration was retired. |
| [#822](https://github.com/Illospace/illospace/issues/822) | Retired migration epic. Past definitions and results remain available. |
| [#829](https://github.com/Illospace/illospace/issues/829) | Still reproduces on the public hostnames. Private MCP works. Requires host administrator and Cloudflare access; [repair steps](public-endpoint-repair.md). |
| [#869](https://github.com/Illospace/illospace/issues/869) | Persist credential expiry on its connection, stop repeated refresh, alert one transition and clear on replacement. Transient failure leaves health unchanged. Verify reauthentication and a real expired episode after deploy. |
| [#874](https://github.com/Illospace/illospace/issues/874) | Historical502 incident recovered. This session received an event ID and a satisfied preservation receipt with mutated refs. Closed with that evidence. |
| [#897](https://github.com/Illospace/illospace/issues/897) | Validate the complete submission before acknowledgement; reject oversize messages, parts or file references with numeric size diagnostics. No accepted text is silently clipped. |
| [#900](https://github.com/Illospace/illospace/issues/900) | Apply every configured projection of an inbound event. Reconcile old tracker rows from current GitHub state after deploy; dry-run replay does not repair them. |
| [#903](https://github.com/Illospace/illospace/issues/903) | Carry artifact paths through the submission prompt and preservation attribution. Verify a deployed receipt for supplied files. |
| [#905](https://github.com/Illospace/illospace/issues/905) | Serialize external identity upserts, use the canonical earliest record and archive duplicates. Repair existing identities through the same service after deploy. |
| [#907](https://github.com/Illospace/illospace/issues/907) | Current workspace.search, cycles.inspect and seven knowledge.get reads succeeded. Historical transport incident closed; public routing remains separately tracked. |
| [#908](https://github.com/Illospace/illospace/issues/908) | Use non-blocking per-run advisory admission so one busy run cannot starve the deadline sweep. Verify sweep progress after deploy. |
| [#914](https://github.com/Illospace/illospace/issues/914) | Return an authorized exclusion reason instead of claiming missing data. Read-only audit:4214,4458,5366,5549 are active private content nodes, with no shared mirrors and no supersession. Their satisfied receipts did not lose the nodes. |
| [#915](https://github.com/Illospace/illospace/issues/915) | Read-time visibility and canonical eligibility checks block stale shared mirrors after a node becomes private. Verify both search and get after deploy. |
| [#916](https://github.com/Illospace/illospace/issues/916) | Preserve intentional first-sentence reuse, enforce owner and visibility scope, and disclose reused/stored text. The corrected acceptance is authoritative; different same-heading text does not change the identity rule. |
| [#917](https://github.com/Illospace/illospace/issues/917) | Preserve bounded tool-failure diagnostics, test first-call batches in fast context, and clarify the safe-tool allowlist. The historical failing payload was not recovered; deploy verification must name its actual rejected tool or argument. |
| [#918](https://github.com/Illospace/illospace/issues/918) | Return one canonical final answer by default; detailed payloads are explicit. Verify compact get_result with a large deployed answer. |
| [#919](https://github.com/Illospace/illospace/issues/919) | Resolve inbound thread handles through their owner; reject malformed UUIDs before querying and redact SQL error diagnostics. Verify an inbound thread through MCP after deploy. |
| [#920](https://github.com/Illospace/illospace/issues/920) | Only completed Cycle runs advance the feed watermark. Migration0068 adds the missing-record rule when later comments hide an opened event. Backfill website rows from current GitHub snapshots after deploy. |
| [#926](https://github.com/Illospace/illospace/issues/926) | Own pending event tasks, provide flush_event_writes, drain during API shutdown and handle cancellation without an unhandled callback. One-shot callers must await flush before closing the loop. |
| [#928](https://github.com/Illospace/illospace/issues/928) | Inherit launch provenance only from the persisted occurrence's own lineage, for generic and chantier continuations. Two hops retain one occurrence and its existing notification ledger; unrelated runs fail closed. |
| [#929](https://github.com/Illospace/illospace/issues/929) | Emit a structured heartbeat failure and redact/bound command exception diagnostics before alerts. The original cause was upstream GitHub500; the retry succeeded. |

## Runtime retirement

Cycle9, Uwear Backend Promotion Readiness, is disabled. The scheduler job `uwear_staging_promotion_pr` is paused on the server and removed from the source catalog, so startup catalog sync cannot enable it again. Historical rows and results remain.

Claude shows dispatcher, SEO, usage digest, R3 worker, R2 evaluator, release radar, outbound and ads all paused. The latter five were already paused; the first three were stopped in this session. No scheduled task was deleted.

## Validation

The imported base passed5670 fast tests. Runtime additions passed104 event/heartbeat/scheduler checks and29 continuation/Cycle-gate checks. The current integrated base passed5678 fast tests in the independent review. A clean PostgreSQL instance ran the full Alembic chain through0068 and93 database tests. New slice results and final integrated gates are recorded below when complete.

No production image was changed, no PR was merged, and no credential or raw private corpus was committed. The public outage is an open operator dependency; pending runtime verification is not zero operational backlog.
