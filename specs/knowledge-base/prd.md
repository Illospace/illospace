# Illo Knowledge — implementation PRD (temporary)

*Temporary build plan. Principles and evaluation contract live in
[north-star.md](north-star.md) — read it first; every decision below serves it.
After shipping, this doc gets closed into a rationale (close-spec) and the code
becomes the reference.*

Status: slice 1 ready to implement. Slices 2–3 sketched, not scoped.

## What we're building

A derived knowledge index in illospace Postgres: one flat table of distilled
rows from many sources, each with provenance back to the canonical system, plus
versioned embeddings, ingested by scheduled connector jobs, recalled through one
hybrid search primitive exposed to Illo runs and over MCP.

Non-goals (v1): code embeddings, reranker model, bursting, real-time ingest,
projects/bundles object. See north-star.md.

## Terminology

"Source" = where a row came from (`domain_records`, `github`, `slack`,
`memory`). "Kind" = what the row is (`doc_page`, `ledger_row`, `issue`, `pr`,
`slack_thread`, `memory`, `incident`). Do NOT use the word "domain" for KB
scoping — it collides with both `Domain`/`DomainRecord` (user data spaces) and
`TaskDomain` (brain/systems/task_domain.py).

## Schema (migration `0047_knowledge_index`)

New file `brain/platform/db/alembic/versions/0047_knowledge_index.py`,
`down_revision = "0046_cycle_failure_guard"`. Follow the defensive
idempotency style of `0011`/`0029` (`_table_exists`, drop-index-if-exists).
After writing it, `alembic heads` MUST show exactly one head.

New model module `brain/platform/db/models/knowledge.py` (pgvector import
try/except-guarded exactly like `reconstructive_memory.py` so SQLite tests
import cleanly):

**`knowledge_items`**
- `id` (pk), `source` (String, indexed), `kind` (String, indexed)
- `source_ref` (String) — stable canonical pointer, e.g.
  `domain_record:1155`, `github:uwear-ai/illospace#543`. Unique on
  `(source, source_ref)`.
- `title` (Text), `summary` (Text), `resolution` (Text, nullable),
  `entities` (JSONB, default list) — the distilled fields
- `raw_text` (Text) — bounded excerpt of the original (cap ~20k chars; record
  truncation in `extra`)
- `search_text` (Text) — denormalized lexical blob (title + summary +
  resolution + entities + raw_text), built in Python on write, with a GIN
  `gin_trgm_ops` index. Copy the exact model+migration template from
  `DomainRecord.search_text` (`models/domain.py:179` + migration `0011`
  lines 268–275). **Decision: trigram, not tsvector** — the repo has zero FTS
  precedent, trigram handles exact-token matching (error strings, flag names)
  well, and it keeps the SQLite fallback story identical to reconstructive
  memory. Revisit tsvector only if trigram precision measurably hurts.
- `extra` (JSONB, default dict) — per-source metadata (labels, state, channel,
  truncation flags). Filters beyond source/kind live here, unindexed until
  needed.
- `content_digest` (String 128), `source_created_at`, `source_updated_at`,
  `ingested_at` (tz-aware), `archived_at` (nullable — soft delete when the
  canonical item disappears)

**`knowledge_item_embeddings`** — mirror `MemoryNodeEmbedding`
(`models/reconstructive_memory.py:146`) column-for-column in spirit:
`item_id` (FK CASCADE), `embedding_kind` (String 40 — `"summary"` for v1),
`model` (String 120 — written via `embedding_model_identity(runtime)` from
`brain/systems/reconstructive_memory/embeddings.py`), `dimension` (Integer),
`embedding` (`Vector(KNOWLEDGE_EMBEDDING_DIM)`, nullable), `content_digest`
(String 128). Unique `(item_id, embedding_kind, model, content_digest)` — same
re-embed-is-a-no-op / model-upgrade-writes-new-row semantics. In
`brain/kernel/config.py`, `KNOWLEDGE_EMBEDDING_DIM` aliases the shared
`EMBEDDING_DIM` like every other semantic family (do not pin an independent
dim; inherit the typmod drift check).

**`knowledge_sync_state`** — one row per source: `source` (unique), `cursor`
(JSONB — per-source watermark shape), `last_run_at`, `last_status`,
`last_stats` (JSONB: `{ingested, skipped, failed, truncated}`). The stats are
load-bearing: north-star requires per-connector accounting, no silent
truncation.

## Connector contract

`brain/systems/knowledge/` (new package):

- `connectors/base.py` — a small protocol:
  `source_key: str`;
  `async enumerate_changed(session, cursor) -> (drafts, new_cursor)` where a
  draft is a plain dataclass carrying every `knowledge_items` field except
  digest/timestamps-of-ingest. Enumeration must be watermark-incremental and
  bounded per run (cap items/run, carry the cursor so the next run resumes —
  backfill happens by repeated runs, not one giant pull).
- `service.py` — the shared pipeline (connector-agnostic):
  for each draft → compute `content_digest` over the distilled fields →
  upsert by `(source, source_ref)` (skip if digest unchanged) → rebuild
  `search_text` → embed `title + "\n" + summary` via
  `brain/systems/memory/embeddings.embed_document` with
  `async_get_embedding_runtime_config(session)` threaded through → write the
  embedding row (no-op if digest row exists) → accumulate stats → write
  `knowledge_sync_state`. Embedding failure degrades loudly: the item row still
  lands (lexical-searchable), failure counted in stats — mirroring how recall
  degrades to lexical-only in `reconstructive_memory/embeddings.py`.

### Slice-1 connectors (structural distillation — no LLM pass yet)

**`connectors/domain_records.py`** — enumerate `DomainRecord` via
`AsyncDomainService` (`brain/systems/user_domains/service.py`) ordered by
update, cursor = last (updated marker, id). Distill structurally: `title` from
the record, `summary` from `serialize_record_compact`, `entities` = [domain
slug, object type], `raw_text` from `search_text`/payload. `kind` = `doc_page`
for Domain-37-style pages, else `record`. `source_ref = f"domain_record:{id}"`.
Archived records → set `archived_at`.

**`connectors/github.py`** — issues + PRs. Auth: mint a read token via the
vault github_app path (`brain/systems/vault/runtime_secrets.py` /
`github_app_mint.async_mint_installation_token`); call the API through the
existing `brain/systems/cortex/project_context/github.py` helpers (they already
do bounded-payload trimming) — extend that module if a needed call is missing,
don't fork it. Enumerate by `updated_since` watermark per repo (GitHub
issues API returns PRs too; `kind` = `issue` | `pr`). Repos: reuse the existing
project-context repo configuration if one exists; otherwise a runtime
setting/env with safe default `["uwear-ai/illospace"]` (no feature gates —
config-with-safe-defaults only). Distill structurally: `title`, `summary` =
state + labels + body excerpt, `resolution` = close/merge info when closed,
`entities` = labels + linked issue/PR refs parsed from body, `extra` = state,
author, labels, url.

The LLM distillation pass (Illo headless runs via `admit_work`, shaped like
`brain/jobs/pipelines/aws_health_scan.py`) is **slice 2** — the `distill` seam
in the draft dataclass is where it will slot in.

## Scheduler wiring

One catalog entry appended to `SCHEDULER_CATALOG`
(`brain/app/scheduler/catalog.py`): `job_key="knowledge_index_sync"`,
`handler_kind="scheduler_builtin"`, cron every 30 minutes, conservative
timeout, `max_concurrency=1`. One entry in `SINGLE_COMMAND_PROGRAM_REGISTRY`
(`brain/app/scheduler/programs.py`) → `python3 -m
brain.jobs.pipelines.knowledge_index_sync`, which runs every registered
connector sequentially and logs per-source stats. Catalog sync is automatic at
daemon startup — no migration, no manual activation.

## Recall: `search_knowledge`

`brain/systems/knowledge/search.py` —
`async search_knowledge(session, query, *, sources=None, kinds=None, limit=10)`.

Three channels over non-archived items, fused:

1. **Lexical**: trigram `word_similarity()` of the query against `search_text`
   (`%>` operator — best-matching-substring semantics, so short queries match
   inside long blobs; plain `similarity()` is length-sensitive and starves
   exact-token matches in long docs), top-K ranked list. SQLite/test fallback: Python term-overlap
   scoring reusing the `_query_terms`/`_lexical_relevance` approach from
   `repositories/reconstructive_memory.py:404`.
2. **Semantic**: `embed_query` → cosine via
   `embedding.cosine_distance`, filtered by **both `model` AND `dimension`**
   (the co-location safety pattern from `search_content_nodes`,
   `repositories/reconstructive_memory.py:259`). Numpy fallback off-Postgres.
   Embedding unavailable → lexical-only, loudly (log, and flag in the result
   envelope).
3. **Recency**: rank by `source_updated_at`, restricted to items already
   surfaced by channels 1–2 (recency is a prior over candidates, never a
   generator of them — and never a filter: old items stay fully reachable).

Fusion: reciprocal rank fusion in Python — `score = Σ w/(60+rank)` per list,
default weights 1.0/1.0/0.5 (lexical/semantic/recency) as module constants.
Dedup by item. Return rows with `source_ref` provenance + per-channel debug
scores (the eval harness will need them). No reranker. Context expansion is the
caller's job: follow `source_ref` to the canonical system for the full thing.

## Exposure

1. **MCP capability** (external agents, humans' agents): in
   `brain/app/api/routers/agent_mcp.py`, add `knowledge.search` to
   `READ_CAPABILITIES` (description + arguments: `query`, optional `sources`,
   `kinds`, `limit`) and one `if capability == "knowledge.search":` branch in
   `_tool_read` delegating to `search_knowledge`. Inherits
   `SCOPE_WORKSPACE_READ`, scope filtering, and the capability catalog for
   free. No new tool, no schema change.
2. **In-run tool** (Illo's own runs — the primary consumer): one definition in
   `brain/systems/runs/tool_catalog/definitions/` + handler in `handlers/`
   following the existing github.py pair's shape, same arguments.

## Slice 1 — deliverables checklist

- [ ] Migration `0047_knowledge_index` (+ `alembic heads` == 1)
- [ ] `models/knowledge.py` (3 tables, guarded pgvector, trgm index)
- [ ] `brain/systems/knowledge/`: base protocol, pipeline service, search
- [ ] Connectors: `domain_records`, `github`
- [ ] `brain/jobs/pipelines/knowledge_index_sync.py` + catalog + program entries
- [ ] MCP `knowledge.search` capability + in-run tool catalog entry
- [ ] Tests (SQLite-runnable): upsert idempotency by digest; watermark advance
      + resume; RRF fusion ordering (incl. recency-as-prior-not-filter);
      embedding-failure degradation; MCP capability list + call; connector
      stats accounting; migration head check
- [ ] No feature gates: merged = live; config only as safe defaults

Out of scope for slice 1: any change to the memory subsystem, Slack, LLM
distillation, backfill tuning, deploy.

## Slice 2 (sketch)

Slack connector (thread-level re-pull, one row per thread) + LLM distillation
via headless Illo runs (`admit_work`, effort from the routing ladder) for
Slack and upgraded GitHub resolution-extraction + memory mirror connector.

## Slice 3 (sketch)

Golden-question eval harness + R2 routine retarget (scores against
north-star.md) + strangler swap of memory recall onto `search_knowledge` when
it demonstrably wins.
