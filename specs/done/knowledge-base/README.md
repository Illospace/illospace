# Illo Knowledge

## Overview

Illo Knowledge is a disposable, source-backed index over company knowledge. It
brings Domain Records, GitHub, Slack, skills, and shared reconstructive memory
into one retrieval surface while each source remains canonical. Every result
keeps provenance that lets a caller return to that source for the full record.

The index uses lexical, semantic, and recency ranking and exposes the same
search contract to Illo runs and MCP clients. The durable product principles
and evaluation contract remain in
[`specs/knowledge-base/north-star.md`](../../knowledge-base/north-star.md).
This document records why the shipped system has its current shape. The code is
the source of truth for its mechanics.

## Why it has this shape

### The index is derived, flat, and source-backed

The index is not another system of record. Connector rows can be rebuilt from
their canonical sources, and archived source material leaves the retrieval set
without erasing its provenance. A flat connector contract also lets a new
source join the same ingestion and search paths without adding a source-specific
retrieval system.

`brain.systems.knowledge.connectors.base` owns the connector boundary.
`brain.systems.knowledge.service` owns shared indexing behavior, and
`brain.platform.db.models.knowledge` owns persistence. Migration
`0047_knowledge_index` establishes the derived index.

### Source and kind answer different questions

`source` identifies where an indexed item came from, such as GitHub, Slack, or
memory. `kind` identifies what the item is, such as an issue, pull request,
Slack thread, or memory. These terms must stay distinct because callers often
need to filter by origin and content type independently.

Do not use `domain` as a knowledge-base scoping term. In this repository,
`Domain` and `DomainRecord` already name user data spaces, while `TaskDomain`
names execution classification in `brain.systems.task_domain`. Reusing the word
for retrieval scope makes APIs and authorization rules ambiguous. Access scope
is represented by `brain.systems.knowledge.scope`; source and kind remain
retrieval filters.

### Lexical search uses trigram matching, not tsvector

The repository had no full-text-search precedent when the index was designed.
Trigram matching already fit its database patterns, handles exact tokens such
as error strings and flag names, and permits an equivalent Python term-overlap
fallback in SQLite. That fallback keeps local and unit-test behavior aligned
with reconstructive-memory recall. A tsvector pipeline would add a second text
normalization model without a measured retrieval benefit.

`brain.systems.knowledge.search` owns the PostgreSQL word-similarity channel,
the SQLite fallback, and reciprocal-rank fusion. The trigram index is declared
by `brain.platform.db.models.knowledge` and migration
`0047_knowledge_index`. Reconsider this choice only if measured lexical
precision shows that trigram matching is the cause.

### Raw conversation stays searchable, but embeddings stay distilled

Exact wording in Slack and GitHub can be the evidence a caller needs, so
bounded source text remains available to lexical search. Embedding that raw
conversation would give repetition, formatting, and side discussions too much
influence. Semantic retrieval therefore uses the distilled question, summary,
resolution, and entities, not the raw transcript. Structural sources can
produce those fields mechanically; conversational sources use Illo.

`brain.systems.knowledge.service` keeps lexical and embedding text separate.
`brain.systems.knowledge.distillation` owns the conversational distillation
contract, and the GitHub and Slack policies live in their modules under
`brain.systems.knowledge.connectors`.

### Distillation uses the durable work intake path

Distillation is regular Illo work, not an inline scheduler model call. It enters
through `admit_work`, uses the source content digest in its idempotency identity,
and leaves the connector watermark behind the item until the admitted work has
a terminal result or an explicit degraded fallback. This makes retries safe per
source version and lets scheduler restarts resume the same work instead of
silently losing or duplicating it.

The durable admission and harvest policy is in
`brain.systems.knowledge.distillation`; `brain.systems.knowledge.service` owns
the connector cursor and accounting around it.

### A Slack thread is one knowledge item

Message-level indexing would fragment decisions and make retrieval return
replies without their question or resolution. Slack therefore uses one row per
thread. A refresh replaces that row with a complete canonical thread, never a
partial reply delta. Deleted source threads are archived instead of pinning the
connector cursor.

This boundary is owned by
`brain.systems.knowledge.connectors.slack` and is pinned by
`tests/test_knowledge_slack_connector.py`.

### Memory mirroring is additive and workspace-safe

The knowledge index has workspace-level retrieval, while reconstructive memory
has finer visibility rules. The mirror therefore admits only workspace-visible
memory content with an organization identity. Private and user-scoped memory
must not enter the index, and a visibility withdrawal must scrub and archive a
previous mirror. A failed mirror write must not change the outcome of canonical
memory ingestion.

`brain.systems.knowledge.connectors.memory` owns eligibility and the repair
sweep. `brain.systems.reconstructive_memory.ingestion` requests an immediate
best-effort mirror only after canonical ingest is durable. The contract is
pinned by `tests/test_knowledge_memory_contract.py` and the memory cases in
`tests/test_knowledge_index.py`.

### Retrieval combines signals without turning them into gates

Lexical ranking finds exact language and semantic ranking finds paraphrases.
Recency reorders candidates found by those channels; it never generates a
candidate or excludes an old one. Reciprocal-rank fusion keeps each signal
independent and inspectable, and semantic failure degrades loudly to lexical
search. No LLM participates in the retrieval primitive.

`brain.systems.knowledge.search` owns ranking. Its request and response
boundary is centralized in `brain.systems.knowledge.search_contract`, including
the per-channel scores used by evaluation.

### Connector progress is bounded and observable

A connector must not turn first sync into one unbounded pull. Durable cursors
let repeated runs complete backfills and resume after faults. Per-source
ingested, skipped, failed, pending, distilled, and truncation accounting is
part of correctness: without it, a partial corpus looks complete.

The connector protocol is in `brain.systems.knowledge.connectors.base`; shared
cursor and accounting policy is in `brain.systems.knowledge.service`. The
scheduled entry point is `brain.jobs.pipelines.knowledge_index_sync`.

### PostgreSQL vector support must remain optional at import time

Production uses pgvector, but the repository also imports the knowledge model
and its migration in SQLite environments. Both must therefore guard the
pgvector import and provide a SQLAlchemy fallback type when the dependency is
absent.
This follows the reconstructive-memory convention and prevents a database
extension from becoming a test-collection dependency.

Knowledge embeddings also inherit the shared embedding dimension and runtime
identity instead of defining an independent configuration. Search and writes
check that shared identity so a model change cannot silently mix incompatible
vectors. This keeps the existing semantic-family drift check authoritative.

The convention is implemented in `brain.platform.db.models.knowledge` and
migration `0047_knowledge_index`, following
`brain.platform.db.models.reconstructive_memory`. The shared dimension is owned
by `brain.kernel.config`; the write and read checks are in
`brain.systems.knowledge.service` and `brain.systems.knowledge.search`.

### Recall quality is measured against provenance

The evaluation asks whether ranked results contain known-best source pointers,
not whether generated prose sounds correct. Artifacts record the question set,
retrieval configuration, engine, and corpus fingerprint. Evaluation rejects an
arm with no reachable expected evidence, while offline comparison rejects
mismatched runs instead of reporting a misleading score.

`brain.systems.knowledge.recall_eval` owns evaluation,
`brain.systems.knowledge.recall_eval_contract` owns its artifact boundary, and
`brain.systems.knowledge.recall_eval_comparison` owns offline comparison.

## Invariants

- Canonical systems remain the source of truth; each knowledge result retains
  a stable provenance pointer.
- Knowledge scope is explicit. `source` and `kind` are filters; `domain` is not
  a knowledge-base scoping concept.
- Conversational raw text remains lexically searchable, but only distilled
  fields are embedded.
- Semantic failure leaves lexical recall available and reports the degraded
  state.
- Recency changes the weight of retrieved candidates; it never narrows the
  search space.
- Each Slack thread produces one knowledge item, not one item per message.
- Distillation enters through `admit_work`, survives scheduler restarts, and is
  idempotent for a source version.
- Memory keeps its own recall and mutation paths; the knowledge index mirrors
  workspace-visible memory additively.
- A memory visibility change cannot expose private or user-scoped content
  through the workspace-wide knowledge index.
- Connector work is bounded, resumable, and explicit about failures and
  truncation.
- The knowledge model and its migration remain importable without pgvector so
  SQLite tests can collect and run.
- Knowledge embeddings use the shared semantic dimension and active model
  identity; the knowledge index does not define a separate embedding runtime.

## Deliberate v1 boundaries

Code embeddings are excluded because repository code search is served by
grep-based tools. A dedicated reranker is excluded until reciprocal-rank fusion
shows a measured limit. Slack remains thread-level instead of using
message-level bursting because the thread is the smallest useful decision
context. General real-time connector ingestion is excluded in favor of bounded
scheduled pulls; the memory post-commit mirror is a narrow freshness path with
the periodic sweep as repair. Projects or bundles are excluded because scopes
are queries over metadata, not containers; a durable named grouping can be a
saved record instead of new knowledge schema.

These are deliberate constraints, not missing implementation. They should be
revisited only when observed retrieval or operating evidence shows that the
simpler design is insufficient.

## Code and test map

- Storage: `brain/platform/db/models/knowledge.py` and
  `brain/platform/db/alembic/versions/0047_knowledge_index.py`.
- Ingestion: `brain/systems/knowledge/service.py`,
  `brain/systems/knowledge/distillation.py`, and
  `brain/systems/knowledge/connectors/`.
- Retrieval and exposure: `brain/systems/knowledge/search.py`,
  `brain/systems/knowledge/search_contract.py`,
  `brain/systems/runs/tool_catalog/definitions/knowledge.py`, and
  `brain/app/api/routers/agent_mcp.py`.
- Evaluation: `brain/systems/knowledge/recall_eval.py`,
  `brain/systems/knowledge/recall_eval_contract.py`,
  `brain/systems/knowledge/recall_eval_comparison.py`, and
  `tests/test_knowledge_recall_eval.py`.
- Connector and mirror behavior: `tests/test_knowledge_index.py`,
  `tests/test_knowledge_slack_connector.py`, and
  `tests/test_knowledge_memory_contract.py`.
