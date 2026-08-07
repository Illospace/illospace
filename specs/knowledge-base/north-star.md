# Illo Knowledge — North Star

*The durable statement of what the knowledge base is and the principles that
govern it. Recall evaluation scores against this document. Design rationale
lives in [`specs/done/knowledge-base/README.md`](../done/knowledge-base/README.md),
and implementation details live in the code.*

## The one-sentence test

> **No work arrives cold:** any question about Uwear — a ticket, a code change, an
> incident, a decision, a business thread — is answerable from one search surface,
> with provenance back to where the answer canonically lives.

If a question a teammate or agent reasonably asks cannot be answered through this
surface, that is a knowledge-base failure, regardless of whether the information
technically existed somewhere.

## What it is

A **queryable index over everything the company produces**, held in illospace
Postgres, navigable by any agent. Illo is the intelligence around it: Illo
distills what goes in, Illo is the default orchestrator of what comes out, and
other agents (human-driven or headless) can query the same primitives directly.

Inspired by Cerebras Knowledge ("How we built our knowledge base", 2026):
one table, many connectors, hybrid retrieval, agent as orchestrator.

## Principles

1. **Index, not truth.** Sources (GitHub, Slack, domain_records, memory, …) stay
   canonical. Every row carries a provenance pointer. Losing the index costs
   re-indexing, never data. Rows are disposable by design.

2. **Meet data where it lives.** Nobody changes where or how they work so the KB
   can see their output. Connectors extract from each platform; the platform's
   own ergonomics stay untouched.

3. **One row interface.** Any source that can emit the row shape is in — that is
   the whole malleability story. A flat index plus metadata filters holds "any
   kind of information about the company" without schema work per source.

4. **Distillation is Illo's job.** Raw content is not embedded directly; what gets
   embedded is the distilled form — the question someone would actually ask, the
   summary, the resolution, the entities involved. Distillation runs on Illo's
   own model routing, so KB quality compounds with Illo's judgment. (Structural
   sources may distill mechanically; conversational sources need the LLM pass.)

5. **Hybrid recall — weights, never gates.** Lexical search for exact tokens,
   embeddings for paraphrase, recency ranking for freshness, fused by reciprocal
   rank fusion. No scorer is trusted alone, and nothing is ever excluded from the
   search space for being unlike the past: **history sets weights, never the
   search space.** This is how the KB indexes the past without blinding Illo to
   unknown unknowns.

6. **Recall primitives are dumb and stable.** `search_knowledge` and the MCP
   `knowledge.search` capability are LLM-free, narrow, and cheap. Orchestration
   — planning which primitives to call, synthesizing answers — lives in agents,
   not in the retrieval layer. Capability over recipes.

7. **Scopes are queries, not containers.** No projects/bundles object. Scoping is
   metadata filtering (`source`, `kind`, and access scope) at query time. If a
   named bundle ever earns its existence, it is a saved filter (a
   `domain_record`), not schema. `domain` is not a knowledge-base scoping term.

8. **Additive coexistence now; unification only if measured.** Workspace-visible
   memory content mirrors into the index as one source among many. Memory retains
   its own recall and mutation paths, and the mirror never widens private or
   user-scoped visibility. Retiring that duplicate path remains the intended end
   state, but it is gated on recall measured to be at least as good — and
   permanent coexistence is a legitimate answer if the measurement never earns
   the swap. Zero blast radius now; one system only on evidence.

## Consumers, in priority order

1. **Illo's own cycles and runs** — recall at run start, evidence mid-run.
2. **Other agents** — Claude Code sessions and human-driven agents, via the illo
   MCP server, hitting the same primitives.
3. **Humans** — by asking Illo, which plans → fans out primitives → synthesizes
   with citations.

## Explicit non-goals (v1 — revisit only when pain is observed)

- Code embeddings (grep/ripgrep is the code-search tool; even Cerebras's
  `search_code` is ripgrep).
- A dedicated reranker model (RRF + recency ranking first).
- Message-level "bursting" (thread-level distillation first).
- Real-time socket ingestion (scheduler-cadence pulls; per-source freshness).
- A projects/bundles schema object (principle 7).

## Evaluation contract

- **Golden questions:** a maintained set harvested from real "Illo should have
  known" failures, each with known-best evidence. Recall is measured against
  them after connector or ranking changes.
- **Comparable evidence:** score artifacts identify the question set,
  configuration, retrieval engine, and corpus. Evaluation must reject an arm
  with no reachable expected evidence instead of treating that arm's misses as
  a ranking result.
- **Per-connector accounting:** each connector reports what it ingested, skipped,
  failed, and truncated — silent truncation reads as "covered everything" when
  it didn't.
