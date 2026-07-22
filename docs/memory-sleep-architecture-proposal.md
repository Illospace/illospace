# Nightly memory sleep architecture proposal

This document answers issue #412 from the checked-out `main` tree and from the reporter's verified 2026-07-22 observations of `illo-dev`. It does not query the live database. Counts and run outcomes attributed to deployment are reporter-provided ground truth.

The short answer is: **the literal `memories_created = 0` / `edges_created = 0` result is intentional for the source-backed reconstructive consolidation entrypoint (high confidence); the absence of any replacement nightly maintenance, supersession, or archival pass is architectural drift.** A recorder-only consolidation phase is coherent. Calling that recorder the whole consolidation subsystem is not.

## 1. Ground truth — what is actually built

### Version boundary and the deployment mismatch

There are three code states that must not be collapsed into one:

1. The checked-out `main` commit is `2765a30` (2026-07-21). Its scheduler includes the await fix from `e72515d`; `nightly_heuristic_review_command()` now runs the coroutine before indexing its result (`brain/app/scheduler/programs.py:226-234`).
2. The reporter says the deployed checkout is frozen at 2026-07-20T15:15Z. Its four latest `nightly_sleep` runs fail at `nightly_heuristic_review` with `'coroutine' object is not subscriptable`. The executor stops the run at the first failed step, so every later phase is blocked (`brain/app/scheduler/executor.py:627-669`). Deploying #403 is therefore a precondition owned by #401/#403, not work for #412.
3. The ticket's “19 pipelines / 18 memory modules” inventory matches commit `0220514`, immediately before the legacy trim in `bae4002` (#355). Current `main` has 15 substantive pipeline modules plus an empty `__init__.py`, and five substantive `brain/systems/memory` modules plus an empty `__init__.py`. The three missing pipelines and twelve missing memory modules are still audited below from `0220514`; historical citations use `path@REV:line` to mean `git show REV:path` followed by that line number. `REV` is `0220514` for the pre-#355 inventory and `00216ed^` for the pre-#230 flat-memory implementation.

There is a second, more consequential mismatch. The reporter verified that deployed `nightly_dream.py` queries the nonexistent `memories` table. That exact SQL exists in the pre-reconstructive file (`brain/jobs/pipelines/nightly_dream.py@00216ed^:30-60`). Current `main` instead queries `memory_nodes` (`brain/jobs/pipelines/nightly_dream.py:30-69`) and writes via the graph-backed compatibility API (`brain/jobs/pipelines/nightly_dream.py:120-157`; `brain/app/cli/memory.py:29-68`). Migration 0020 deliberately drops `memories` and the other flat-memory tables (`brain/platform/db/alembic/versions/0020_reconstructive_memory.py:82-92,332`). Because #230 predates the reported deployment freeze, the repository alone cannot explain how deployed code retained the old query while its schema lost the table. The deployed commit SHA, image digest, and the deployed file's checksum/content would settle that provenance question.

The reporter-provided deployed database snapshot is the operational baseline for this proposal: there is no `memories` table; there are 710 `memory_nodes`, 3,244 `memory_edges`, and populated `memory_assertions`, `memory_sources`, `memory_spans`, `memory_node_embeddings`, `memory_health_log`, and `consolidation_runs`. The three consolidation rows (July 19–21) are all completed with zero memories/edges created and the same source-backed summary. These values were not re-derived here because this worktree has no database access.

### How scheduling actually dispatches

The catalog declares only `nightly_sleep` at 03:00 and `curiosity_cron` at 22:00 (`brain/app/scheduler/catalog.py:26-83`). For `nightly_sleep`, the actual planner uses `NIGHTLY_SLEEP_STEP_KEYS` (`brain/app/scheduler/programs.py:11-25,119-171`), and the executor maps those keys to commands (`brain/app/scheduler/executor.py:275-302`). A second `_nightly_steps()` list mentions `nightly_context_eval` (`brain/app/scheduler/programs.py:237-267`), but the executor imports and invokes `build_scheduler_step_plan()` instead (`brain/app/scheduler/executor.py:617-618`); repository search finds no caller of `_nightly_steps()`. The budget's work-type names are advisory intent, not dispatch (`brain/app/scheduler/catalog.py:38-47`).

Status terms below are deliberately strict:

- **Live (deployed):** reporter evidence shows the step ran in the deployed sequence.
- **Main-reachable:** current code dispatches it, but the deployed sequence does not reach it because of the earlier heuristic failure.
- **Live on demand:** a current API/CLI code path invokes it, but not the scheduler.
- **Dormant:** no current scheduler or runtime caller was found, or the only caller is blocked/deleted.
- **Broken:** its known deployed path cannot execute against the reported schema or fails before completing.

### Pipeline audit (the ticket's 19 entries, including the package marker)

| Pipeline | Scheduler reachability today | Data model | Verdict and evidence |
|---|---|---|---|
| `__init__.py` | No; it is not one of the two catalog jobs or any dispatched command (`brain/app/scheduler/catalog.py:26-83`; `brain/app/scheduler/executor.py:275-325`). | None. | **Dormant / not a pipeline.** It is a zero-byte package marker, included only to reconcile the ticket's count of 19. |
| `consolidate.py` | Yes: first memory step and later wake-up index (`brain/app/scheduler/executor.py:275-302`). | Reconstructive graph. It imports `MemoryNode` and counts active nodes (`brain/jobs/pipelines/consolidate.py:14-17,42-49`). | **Live (deployed), intentional recorder.** It inserts literal 0/0/0 and the source-backed summary (`brain/jobs/pipelines/consolidate.py:20-40`); reflection and synthesis are explicitly retired (`brain/jobs/pipelines/consolidate.py:53-60`). |
| `cortex_emerge.py` | No scheduler command (`brain/app/scheduler/executor.py:275-325`). | Reads graph nodes/edges, then writes Cortex ideas, not memories (`brain/jobs/pipelines/cortex_emerge.py:145-160,291-323,363-427`). | **Live on demand in main.** The Cortex API invokes emergence and optimization (`brain/app/api/routers/cortex_intel.py:27-48`); no scheduler route was found. |
| `cortex_encode_digest.py` | No scheduler command (`brain/app/scheduler/executor.py:275-325`). | Graph writer via Cortex encoding and `add_memory` (`brain/jobs/pipelines/cortex_encode_digest.py:31-84`; `brain/systems/cortex/encode.py:177-192`). | **Dormant/manual.** It exposes a CLI main path, but no recurring or runtime caller appears in the repository (`brain/jobs/pipelines/cortex_encode_digest.py:87-100`). |
| `cortex_optimize.py` | No scheduler command (`brain/app/scheduler/executor.py:275-325`). | Adjacent Cortex idea/edge tables, neither memory model (`brain/jobs/pipelines/cortex_optimize.py:43-83,88-118,157-179`). | **Live on demand in main** through the Cortex API (`brain/app/api/routers/cortex_intel.py:47-48`), not a memory consolidation phase. |
| `curiosity.py` | Yes, as the separate 22:00 `curiosity_cron` (`brain/app/scheduler/catalog.py:61-81`; `brain/app/scheduler/executor.py:318-320`). | Graph writer through the graph-backed memory CLI (`brain/jobs/pipelines/curiosity.py:368-425`; `brain/app/cli/memory.py:29-68`). | **Scheduler-live by code/catalog; deployed outcome unknown.** It is independent of the failing 03:00 sequence, but no run evidence was supplied. |
| `divergence.py` | No current file or command (`brain/app/scheduler/executor.py:275-325`). | Historical graph reader/writer (`brain/jobs/pipelines/divergence.py@0220514:31-43,111-128`). | **Dormant/deleted.** The historical module could ingest a divergence result, but it had no scheduler registration; #355 removed it. |
| `experiment_tracking.py` | No scheduler command (`brain/app/scheduler/executor.py:275-325`). | Graph writer through `add_memory` (`brain/jobs/pipelines/experiment_tracking.py:15-63`). | **Dormant.** Its docstring says nightly implementation calls it, but the actual implementation flow does not (`brain/jobs/pipelines/nightly_implement.py:260-303`); repository search finds no caller. |
| `nightly_assess.py` | Yes, after the failing heuristic step (`brain/app/scheduler/programs.py:11-25`; `brain/app/scheduler/executor.py:298`). | Reads and updates graph nodes (`brain/jobs/pipelines/nightly_assess.py:68-95,239-261`). | **Main-reachable; dormant in deployment.** The executor returns on the earlier failure (`brain/app/scheduler/executor.py:627-669`). |
| `nightly_context_eval.py` | No in the actual plan. It appears only in the uncalled `_nightly_steps()` list (`brain/app/scheduler/programs.py:237-267`), not `NIGHTLY_SLEEP_STEP_KEYS` or the command mapping (`brain/app/scheduler/programs.py:11-25`; `brain/app/scheduler/executor.py:275-302`). | Neither: its source collection and persistence functions are no-ops (`brain/jobs/pipelines/nightly_context_eval.py:22-41`). | **Dormant/incomplete.** It can construct an evaluation payload (`brain/jobs/pipelines/nightly_context_eval.py:44-106`) but cannot gather or persist real results. |
| `nightly_dream.py` | Yes, after the failing heuristic step (`brain/app/scheduler/executor.py:293-295`). | **Split verdict:** reported deployment uses the dead flat model (`brain/jobs/pipelines/nightly_dream.py@00216ed^:30-60`); current `main` reads and writes the graph (`brain/jobs/pipelines/nightly_dream.py:30-69,120-157`). | **Broken in the reporter-observed deployment** because `memories` does not exist, and independently blocked by the earlier scheduler failure. **Main-reachable after #403**, but not demonstrated live. Its `decay_eligible=True` signal is currently discarded by the compatibility API (`brain/app/cli/memory.py:38-54`). |
| `nightly_guardian.py` | No scheduler command (`brain/app/scheduler/executor.py:275-325`). | Skills/violations tables, neither memory model (`brain/jobs/pipelines/nightly_guardian.py:25-88`). | **Dormant/manual.** It has a CLI path but no current caller (`brain/jobs/pipelines/nightly_guardian.py:91-99`). |
| `nightly_implement.py` | Yes, after the failing heuristic step (`brain/app/scheduler/executor.py:299`). | Reads graph memories; direct autonomous writes are disabled (`brain/jobs/pipelines/nightly_implement.py:113-130,201-221`). | **Main-reachable, review-only; dormant in deployment.** Its current main flow writes proposals/artifacts, not memory changes (`brain/jobs/pipelines/nightly_implement.py:260-303`). |
| `nightly_memory_quality.py` | No current file or command (`brain/app/scheduler/executor.py:275-325`). | Historical graph plan (`brain/jobs/pipelines/nightly_memory_quality.py@0220514:19-22,64-99`). | **Dormant/deleted and incomplete.** It was non-mutating and its contradiction loader returned an empty list (`brain/jobs/pipelines/nightly_memory_quality.py@0220514:102-104`); #355 removed it. |
| `nightly_reflect.py` | Yes, after the failing heuristic step (`brain/app/scheduler/executor.py:293`). | Reads graph nodes and retrieval feedback (`brain/jobs/pipelines/nightly_reflect.py:75-81,108-121`). | **Main-reachable but partly dormant; dormant in deployment.** `apply_reflection()` contains mutation logic (`brain/jobs/pipelines/nightly_reflect.py:312-480`) but `run_reflection()` never calls it and only saves output (`brain/jobs/pipelines/nightly_reflect.py:483-563`); the later quality sweep uses an import that is caught on failure (`brain/jobs/pipelines/nightly_reflect.py:565-579`). |
| `nightly_repo_refresh.py` | No current file or command (`brain/app/scheduler/executor.py:275-325`). | Historical graph-intent payload, but no storage model is touched (`brain/jobs/pipelines/nightly_repo_refresh.py@0220514:2-7,30-64`). | **Dormant/deleted.** It returned a refresh request rather than persisting a summary; #355 removed it. |
| `nightly_skill_quality.py` | No actual scheduler command (`brain/app/scheduler/executor.py:275-325`). | Neither memory model; plan-only skill analysis (`brain/jobs/pipelines/nightly_skill_quality.py:1-6,20-63`). | **Dormant.** The module says it never mutates and repository search finds no caller. |
| `project_draft_cleanup.py` | Yes, after the failing heuristic step (`brain/app/scheduler/executor.py:297`). | Project drafts, neither memory model (`brain/jobs/pipelines/project_draft_cleanup.py:16-31`). | **Main-reachable; dormant in deployment** because the executor stops earlier (`brain/app/scheduler/executor.py:627-669`). |
| `sync_brain_to_files.py` | Yes, after the failing heuristic step (`brain/app/scheduler/executor.py:296`). | Reads the reconstructive graph (`brain/jobs/pipelines/sync_brain_to_files.py:23-35`). | **Main-reachable; dormant in deployment** because the executor stops earlier (`brain/app/scheduler/executor.py:627-669`). |

### `brain/systems/memory/` audit (the ticket's 18 entries, including the package marker)

The current active graph package identifies itself as `brain.systems.reconstructive_memory`, not `brain.systems.memory` (`brain/systems/reconstructive_memory/__init__.py:1-5`). The surviving `brain/systems/memory` files are shared runtime utilities or compatibility-era policy helpers.

| Module | Scheduler reachability today | Data model | Verdict and evidence |
|---|---|---|---|
| `__init__.py` | No direct or indirect scheduler behavior (`brain/app/scheduler/executor.py:275-325`). | None. | **Dormant / package marker.** It is zero bytes and is included only to reconcile the count of 18. |
| `attention_controller.py` | Not directly scheduled; used by recall finalization (`brain/app/mcp/server.py:490-526`). | Graph-era candidates and retrieval decisions, not flat `memories`. | **Live on the recall path.** The MCP server imports it at startup (`brain/app/mcp/server.py:51`); it is not a nightly maintenance component. |
| `conflict_resolver.py` | No current file; its only nightly-era companion was removed. | Historical deterministic graph-resolution plan (`brain/systems/memory/conflict_resolver.py@0220514:1-5,29-59`). | **Dormant/deleted.** It planned reversible actions rather than applying deletes; #355 removed it. |
| `conflict_scout.py` | Not in the actual nightly plan. It is imported by context policy (`brain/systems/context/policy.py:28-29`), while the context-eval pipeline is not dispatched (`brain/app/scheduler/programs.py:11-25`). | Pure graph-shaped claim comparison; no storage writes (`brain/systems/memory/conflict_scout.py:1-6,80-113`). | **Live library capability, not nightly maintenance.** Exact deployed feature use is not inferable from static imports. |
| `dedup.py` | No current file or scheduler caller. | Historical graph retrieval-result deduplication (`brain/systems/memory/dedup.py@0220514:1-4,17-95`). | **Dormant/deleted.** It deduplicated returned candidates, not stored nodes; #355 removed it. |
| `embeddings.py` | Indirectly reachable when scheduled curiosity/dream ingestion embeds nodes; the reconstructive adapter imports it (`brain/systems/reconstructive_memory/embeddings.py:12-20`). | Model-neutral embedding client used by graph embeddings and other systems. | **Live shared service.** It is not a consolidation phase; query embedding degrades to lexical retrieval on failure (`brain/systems/reconstructive_memory/embeddings.py:80-109`). |
| `encoder.py` | No current file or scheduler caller. | Historical graph compatibility writer through `add_memory` (`brain/systems/memory/encoder.py@0220514:19,53-78`). | **Dormant/deleted.** #355 removed it. |
| `harvest.py` | No current file or scheduler caller. | Historical extraction contract intended to feed graph ingestion (`brain/systems/memory/harvest.py@0220514:1-7,40-89`). | **Dormant/deleted.** It defined candidates but had no surviving scheduled owner; #355 removed it. |
| `integrity.py` | No current file or scheduler caller. | Historical graph integrity intent (`brain/systems/memory/integrity.py@0220514:1-5`). | **Dormant/deleted and incomplete.** Its integration entrypoint gathered no graph data and returned an empty report (`brain/systems/memory/integrity.py@0220514:135-158`). |
| `lessons.py` | No current file or scheduler caller. | Historical graph writer plus guardian/lesson tables (`brain/systems/memory/lessons.py@0220514:23-38`). | **Dormant/deleted.** #355 removed it. |
| `narratives.py` | No current file or scheduler caller. | Separate `ProjectNarrative` tables, neither flat memory nor the core graph (`brain/systems/memory/narratives.py@0220514:1-19`). | **Dormant/deleted.** It had session-era callers but no current scheduler owner; #355 removed it. |
| `repo_summary.py` | No current file. The catalog budget still names `repo_summary_refresh`, but budget names do not dispatch code (`brain/app/scheduler/catalog.py:38-47`; `brain/app/scheduler/executor.py:275-302`). | Historical graph-intent refresh payload, with no persistence (`brain/systems/memory/repo_summary.py@0220514:1-7,173-252`). | **Dormant/deleted.** #355 removed it; the advertised work type now has no executable phase. |
| `retrieval.py` | No current file or scheduler caller. | Historical query preprocessing and `retrieval_log` statistics, not the current graph reconstruction path (`brain/systems/memory/retrieval.py@0220514:1-9,62-128`). | **Dormant/deleted.** Current recall is owned by `brain/systems/reconstructive_memory/controller.py:23-145`. |
| `retrieval_feedback.py` | Indirectly present in scheduled reflection, but that phase is blocked in deployment (`brain/jobs/pipelines/nightly_reflect.py:75-81`; `brain/app/scheduler/executor.py:293`). It is also called by live MCP recall (`brain/app/mcp/server.py:206-217`). | Reconstructive run/evidence feedback and graph salience (`brain/systems/memory/retrieval_feedback.py:45-106,320-331`). | **Live on recall; main-reachable nightly; nightly-dormant in deployment.** Its automatic “hit” signal is a proxy for a nonempty selected evidence pack, not verified answer correctness (`brain/systems/memory/retrieval_feedback.py:45-106`). |
| `retrieval_pools.py` | No current file or scheduler caller. | Historical graph `MemoryNode` pools plus project narratives (`brain/systems/memory/retrieval_pools.py@0220514:80-160,206-310`). | **Dormant/deleted.** #355 removed it. |
| `scope.py` | No current file or scheduler caller. | Historical graph queries and node updates (`brain/systems/memory/scope.py@0220514:80-115`). | **Dormant/deleted.** #355 removed it. |
| `source_freshness.py` | No current file or scheduler caller. | Historical deterministic checks over source metadata, no direct flat-memory dependency (`brain/systems/memory/source_freshness.py@0220514:1-6,78-115`). | **Dormant/deleted.** #355 removed the most directly reusable freshness policy. |
| `truth_maintenance.py` | Not in the actual nightly plan; imported by conflict scouting/context policy (`brain/systems/context/policy.py:28-29`). | Graph-shaped claim normalization, truth-state construction, and resolution plans (`brain/systems/memory/truth_maintenance.py:373,705,1025,1140`). | **Live library capability, no active nightly applier.** Static code proves the policy functions exist, but not that a deployed hot path enables them. |

### What is genuinely live versus merely intended

The scheduler's intent is broad—conflict resolution, repo summaries, skill evaluation, context evaluation, reflection/dream (`brain/app/scheduler/catalog.py:38-47`)—but the actual 03:00 memory behavior observed in deployment is narrow: it records a zero-change reconstructive pass, proceeds through unrelated learning steps, then dies before reflection and dream. Current `main` fixes that earlier crash, but no successful post-fix deployed run exists in the supplied evidence.

**Action from section 1:** deploy #403 under its existing ticket, capture one complete step-by-step `nightly_sleep` run, and record the deployed SHA/image/file checksum. Do not edit memory architecture until the old-`nightly_dream`/new-schema provenance mismatch is resolved.

## 2. What the reconstructive graph is for

### The five core records in plain language

- A **`memory_source` is immutable source material**: the conversation, agent run, curation action, file, or generated artifact from which a memory was derived. It carries ownership/visibility, a digest, raw or structured content, observed time, valid time, authority, sensitivity, and retention metadata (`brain/platform/db/models/reconstructive_memory.py:70-95`).
- A **`memory_span` is an addressable excerpt of a source**: the particular text and locator that support a node, edge, or assertion (`brain/platform/db/models/reconstructive_memory.py:98-113`). It is the evidence handle, not another free-floating memory.
- A **`memory_node` is a retrievable concept or content item**: cue, tag, episode/content, summary, procedure, policy, and similar kinds. It holds display text plus scope, visibility, confidence, truth/freshness status, valid time, and soft-archive state (`brain/platform/db/models/reconstructive_memory.py:116-143`).
- A **`memory_edge` says how two nodes relate**: for example cue-to-tag, tag-to-content, or `superseded_by`. It has direction, confidence/weight, activation data, and the IDs of spans that justify the relationship (`brain/platform/db/models/reconstructive_memory.py:164-186`).
- A **`memory_assertion` is the truth-bearing claim behind a content node**. Claim text and optional subject/predicate/object are separated from node display text, with truth/review status, confidence, validity, and supporting span IDs (`brain/platform/db/models/reconstructive_memory.py:189-213`).

Normal ingestion proves why all five matter. One incoming item creates an immutable source and full-content span, a content node, an assertion pointing back to the span, deterministic tag/cue nodes, and evidence-backed edges in one transaction (`brain/systems/reconstructive_memory/ingestion.py:95-156,158-207`). Embeddings are a replaceable derivative, stored separately from node identity (`brain/platform/db/models/reconstructive_memory.py:146-161`).

### How recall answers a query today

The old `brain/systems/memory/retrieval.py` is not the active path. Current recall proceeds as follows:

1. `brain_recall` delegates to `memory_reconstruct`; recall without a user or org context fails closed (`brain/app/mcp/server.py:221-264,321-367`).
2. The controller starts a persisted reconstruction run, computes a query embedding, and asks the node repository for candidates (`brain/systems/reconstructive_memory/controller.py:23-58`). If embedding fails, it logs the degradation and uses lexical retrieval (`brain/systems/reconstructive_memory/embeddings.py:80-109`).
3. Candidate search enforces visibility and excludes archived nodes, nodes whose truth status is `superseded`, and nodes with an outgoing `superseded_by` edge (`brain/platform/db/repositories/reconstructive_memory.py:259-341`). It does **not** currently filter `valid_until`.
4. Ranking blends semantic similarity, lexical term coverage, and only a small storage-confidence tie-break: 72/25/3 when an embedding exists, or 95/5 lexical/confidence without one (`brain/platform/db/repositories/reconstructive_memory.py:404-489`). These scores are query-local annotations, not new rows.
5. The controller loads assertions and their source spans, materializes an evidence item for each candidate, persists reconstruction steps/evidence, and returns a source-backed evidence pack (`brain/systems/reconstructive_memory/controller.py:59-145`).
6. The compatibility response maps that pack to “memories,” then the attention controller selects, suppresses, or lazy-loads candidates (`brain/app/mcp/server.py:267-275,457-526`). Retrieval feedback is logged, but a selected nonempty pack is only a hit proxy; it does not prove the final answer cited or used the evidence correctly (`brain/systems/memory/retrieval_feedback.py:45-106`).

Today, then, “reconstructive” means **assemble the best evidence at query time from stored claims and source excerpts**, while preserving a trace of what was considered. It is not graph traversal in the strong sense—the retrieval seed is a ranked content-node search, followed by assertion/span hydration—but it has the data model needed for richer reconstruction later.

### What “source-backed; no derived rows” means

The phrase is a design stance against manufacturing untraceable semantic blobs during consolidation. The current job documents that stance directly (`brain/jobs/pipelines/consolidate.py:2-6`) and its tests assert both that the pass manufactures no rows and that it ignores workspace daily logs (`tests/test_nightly_pipeline.py:92-113`). A database regression test also asserts the persisted phase name (`tests/test_consolidation_restoration.py:16-46`). The migration from the flat model explicitly dropped `memories` (`brain/platform/db/alembic/versions/0020_reconstructive_memory.py:82-92,332`).

It does **not** mean “only raw rows may exist.” Ingestion deterministically derives cue/tag nodes and edges from a source (`brain/systems/reconstructive_memory/ingestion.py:158-207`), and `nightly_dream` in current `main` can ingest generated text as a new source-backed graph item (`brain/jobs/pipelines/nightly_dream.py:120-157`). The defensible invariant is narrower: every truth-bearing or synthesized memory must retain evidence lineage and a declared epistemic status.

### Is 0/0 coherent or drift?

**Position: the literal 0/0 result is intended design, with high confidence.** The code inserts literal zero values and the exact operator summary (`brain/jobs/pipelines/consolidate.py:20-40`), retires the old reflection/synthesis phases (`brain/jobs/pipelines/consolidate.py:53-60`), and tests the no-manufactured-row behavior (`tests/test_nightly_pipeline.py:92-113`). This is too explicit and too well covered to call accidental drift.

**The subsystem-level outcome is still drift.** Before the reconstructive replacement, the 666-line job promised import, association, decay, hierarchical consolidation, reflection, and synthesis (`brain/jobs/pipelines/consolidate.py@00216ed^:3-11,61-137`). The replacement preserved the job name and run ledger but retired those behaviors without installing a graph-native owner for freshness, supersession, archival, or synthesis. The repository contains good manual primitives: supersession marks old nodes/assertions, adds an evidence-backed `superseded_by` edge, and records the curation reason as a source/span (`brain/systems/reconstructive_memory/curation.py:92-196,262-289`); archival is soft and also records a reason (`brain/systems/reconstructive_memory/curation.py:199-239`). But the compatibility repository's scheduled-looking decay query returns `[]` unconditionally (`brain/platform/db/repositories/reconstructive_memory.py:1203-1205`). There is one manual, narrowly targeted cleanup script for `[auto-encoded]` exhaust, explicitly preserving provenance (`scripts/archive_auto_encoded_memories.py:1-13,197-207`), not a general nightly policy.

This distinction resolves the audit confusion:

- **Coherent:** consolidation does not create derived memories merely to make counters nonzero.
- **Drift:** `consolidation_runs` remains the public evidence of “sleep,” while its only memory phase is an operator heartbeat and the graph grows without scheduled stewardship.

The code settles intent for the 0/0 row. What it could not settle is whether product leadership intended “never synthesize any source-backed summary” or merely “do not synthesize without evidence” — no ADR is encoded next to the implementation, so this needed an owner.

**Settled by the owner (2026-07-22): a derived assertion with complete span lineage *does* count as source-backed.** The stance is therefore “do not synthesize **without evidence**,” not “never synthesize,” and the recommendation below rests on a decision rather than an assumption. The condition attached to that yes makes the invariant stated above binding rather than advisory: a derived assertion is admissible only if it carries **complete span lineage and a declared epistemic status**, enforced at write time rather than asserted in review. A derived row that cannot name its spans is not source-backed and must not be written — otherwise “derived assertion” becomes the hole through which the untraceable semantic blobs of the pre-#230 flat model re-enter, which is precisely what the original stance existed to prevent.

**Action from section 2:** rename the product concept mentally before changing code: today's phase is a **consolidation-run recorder**, not consolidation. Preserve the source-backed invariant, then add graph-native stewardship as a separately measurable phase.

## 3. Prior art — the web-search step

This section uses only the supplied prior-art dossier. The sandbox did not independently open the linked sources, so claims below are conservative paraphrases of search-result summaries. Confidence is marked accordingly; no URL has been invented.

1. **Sleep-time/offline compute — [Letta, “Sleep-time Compute”](https://www.letta.com/blog/sleep-time-compute/) (medium confidence).** The dossier describes agents using idle time to reorganize and pre-reason over already collected data, without new environment interaction. **Applies to Illo because** its 03:00 scheduler is already the right control plane for bounded offline work. **Does not apply literally because** Illo should not permit unconstrained self-rewriting: every mutation must be source-backed, auditable, tenant-scoped, and reversible.

2. **Reflection and synthesis — [Generative Agents](https://ar5iv.labs.arxiv.org/html/2304.03442) (medium confidence).** The dossier describes a memory stream containing observations, plans, and reflective insights, with reflection clustering observations into higher-order inferences. **Applies to Illo because** daily episodes can be clustered into evidence-backed lessons or rolling summaries that improve broad recall. **Does not apply without modification because** stored reflections can become self-confirming; Illo must distinguish evidence, derived summary, and tentative dream, and must prevent a generated item from becoming its own independent authority.

3. **Temporal knowledge and supersession — [Zep / Graphiti temporal knowledge graph](https://arxiv.org/abs/2501.13956) (medium confidence).** The dossier highlights validity intervals and explicit handling of facts that change over time. **Applies to Illo because** its nodes, sources, and assertions already have `valid_from`/`valid_until`, and its curation path already models `superseded_by`; the missing piece is a scheduled policy that uses them. **Does not imply deletion because** a former fact may be false now but essential to reconstruct what Illo knew at an earlier time.

4. **Hierarchical/rolling summarization — [MemGPT overview](https://www.emergentmind.com/topics/memgpt) (low-to-medium confidence).** The dossier describes virtual-context tiers and recursive summarization to manage bounded active context. **Applies to Illo because** a current, evidence-bound rolling summary can answer broad queries before falling back to raw episodes. **Does not justify a separate opaque memory hierarchy because** Illo already has node kinds, spans, and reconstruction traces; adding an independent tier store would duplicate identity and lineage.

5. **Forgetting and eviction — [Mem0, “Memory Eviction and Forgetting in AI Agents”](https://mem0.ai/blog/memory-eviction-and-forgetting-in-ai-agents) (medium confidence).** The dossier frames forgetting as policy rather than simple age-based deletion, using relevance/importance and decay. **Applies to Illo because** archive candidates should combine age, confidence, use, validity, and dependency checks. **Does not apply as hard deletion because** the graph's main advantage is auditability; routine forgetting should hide/soft-archive while preserving source lineage.

6. **Auditable consolidation — [Hindsight memory consolidation](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation) (low-to-medium confidence).** The dossier describes retaining raw experiences while deriving more compact knowledge and separating consolidation from retrieval. **Applies to Illo because** it supports the exact compromise the current system needs: keep raw evidence and add derived, replaceable summaries. **Does not license unsupported synthesis because** an Illo summary/assertion should be admitted only when its supporting span set is complete enough to audit.

7. **Governed forgetting — [SSGM governance framework](https://arxiv.org/pdf/2603.11768) (low confidence).** The dossier identifies risks and governance needs when agent memory mutates over time. **Applies to Illo because** automatic supersession and archival need a reviewable rule version, mutation reason, provenance, and rollback story. **Does not supply Illo's concrete thresholds because** the dossier summary does not expose its detailed controls; Illo's retention, privacy, user-intent, and content-kind rules must be decided locally.

Together these sources point to five design rules, not a new product-sized subsystem:

1. Run heavy comparison and summarization offline.
2. Preserve raw evidence and temporal history.
3. Treat reflection and dream output as derived, not authoritative by default.
4. Prefer rolling summaries and soft archival over destructive compaction.
5. Make forgetting policy explicit, staged, reversible where legally possible, and measurable.

**Action from section 3:** use prior art to constrain Illo's existing graph rather than copy another agent's memory stack. The graph already has most required primitives; the missing product is the nightly policy and its evidence contract.

## 4. Architecture options

### Option A — Graph Steward (minimal change)

**What runs nightly.** After the recorder, run deterministic graph maintenance only: expire objectively out-of-validity nodes, identify exact duplicates, apply only high-certainty supersessions, archive eligible low-value nodes, backfill missing embeddings, and write health metrics. No LLM-generated memory is created.

**What it writes.** Existing node/assertion truth and freshness fields; `valid_until`/`archived_at`; evidence-backed `superseded_by` edges; curation source/span rows containing the rule, version, and reason; embedding rows; and real `consolidation_runs` counts. Existing manual supersession already supplies the audit shape (`brain/systems/reconstructive_memory/curation.py:162-195,262-289`).

**What it removes or supersedes.** It soft-archives expired or policy-eligible material and supersedes exact/certain replacements. It never hard-deletes for normal forgetting. Raw sources, spans, old assertions, and supersession edges remain.

**How retrieval changes.** Superseded and archived nodes are already excluded (`brain/platform/db/repositories/reconstructive_memory.py:284-298`), so most benefits arrive without a new retrieval architecture. Add an explicit validity-time filter or guarantee the nightly pass archives expired items before recall.

**Migration cost.** **Low.** Reuse current tables and curation operations; add a scheduler phase, candidate queries, metrics, and tests. No legacy-memory migration is needed because the deployed graph is already canonical.

**Most likely failure mode.** False supersession or over-aggressive archival, especially because many current assertions are whole-text claims without normalized subject/predicate/object. The safe response is conservative rules, dry-run reporting first, idempotency, and a review threshold—not an LLM deciding deletion.

**Value.** This is the cheap path requested by the ticket: make the existing graph maintain itself—dedup, supersede, decay/archive—without inventing a second memory subsystem. It directly stops unbounded active growth but does not create higher-level organization.

### Option B — Evidence-Bound Sleep (maintenance plus rolling synthesis)

**What runs nightly.** Run Option A's deterministic stewardship, then cluster the day's active episodes by scope/topic, update a rolling summary or lesson for clusters with enough evidence, and optionally generate a small dream set from distant clusters. Summary assertions must cite source spans; dreams must be labeled tentative and cannot support factual recall until promoted by later evidence or review.

**What it writes.** The same graph primitives: `summary`/`lesson`/`dream` content nodes, assertions with complete `source_span_ids`, typed derivation edges to supporting content, embeddings, a curation source containing prompt/model/policy version, and reconstruction/health metrics. Each new rolling summary supersedes the previous summary for the same scope/window instead of overwriting it.

**What it removes or supersedes.** It applies the cleanup policy below, supersedes old rolling summaries, archives expired transient items and stale unpromoted dreams, and retains raw episodes as cold/auditable evidence. It does not hard-delete ordinary memory.

**How retrieval changes.** Broad or planning queries prefer the current evidence-backed summary, then hydrate raw supporting assertions when needed. Fact lookups continue to prefer current assertions/source spans. Tentative dreams are excluded from normal factual recall and exposed only to creative/ideation retrieval. A reconstruction trace records which layer answered.

**Migration cost.** **Moderate.** No new storage system is required, but Illo needs derivation-edge conventions, epistemic/content-kind policy, summary-window identity, candidate/evidence validation, a scheduler phase, and evaluation fixtures. Existing dream rows may need a one-time classification/archive review because `decay_eligible` is currently discarded (`brain/app/cli/memory.py:38-54`).

**Most likely failure mode.** A fluent but wrong summary becomes a privileged retrieval shortcut and ossifies hallucination. Admission gates must require source coverage, separate fact from interpretation, cap recursive depth, retain the superseded summary chain, and compare retrieval outcomes before/after rollout.

**Value.** This is the first option that makes recall better organized over time rather than merely smaller, while preserving the reconstructive system's defining lineage.

### Option C — Tiered Dreaming Memory OS

**What runs nightly.** Move items through hot episodic, warm semantic/summary, cold archive, and separate dream/hypothesis tiers; recursively compact old tiers; replay retrieval failures; and promote/demote memories by learned utility.

**What it writes.** Tier assignments, recursive summary lineage, learned salience/utility, promotion history, hypothesis state, and possibly a cold-store index in addition to the current graph.

**What it removes or supersedes.** It compacts hot episodes into summaries, archives older raw items to a cold tier, supersedes summaries recursively, and hard-deletes only under explicit retention/erasure policy.

**How retrieval changes.** A router chooses tiers and budgets, expands summaries into raw evidence when confidence is low, and treats dreams as a separate retrieval pool. This is closer to a virtual-memory manager than today's ranked node search.

**Migration cost.** **High.** It introduces lifecycle state, tier routing, promotion/demotion, cold storage or partitioning, recursive lineage, new observability, and a large evaluation surface. It also risks duplicating current node kinds and visibility semantics.

**Most likely failure mode.** Summary collapse and tier thrash: repeated compression loses a decisive detail while learned utility overfits yesterday's query distribution. Operational complexity can also make provenance harder, not easier, to inspect.

**Value.** It is a plausible long-term direction only after Illo demonstrates that Option B's simpler graph-native summaries improve measured recall.

### Cleanup and staleness policy shared by all options

The first production policy should be explicit and conservative:

**Supersede, do not overwrite.** A claim may be automatically superseded only when it has the same tenant/scope and normalized subject/predicate, and either (a) an explicit correction names it or (b) a newer, at-least-as-authoritative source covers an overlapping validity period and directly contradicts it. Set the old node and assertion to `superseded`/`stale`, close `valid_until`, and create `old --superseded_by--> new`. The edge must cite a persisted curation-reason span. This mirrors the existing curation path (`brain/systems/reconstructive_memory/curation.py:162-195,262-289`). Ambiguous contradictions go to review; they are not auto-resolved.

**Archive in stages.** Soft-archive, with an evidence-backed reason:

- Objective transient facts whose `valid_until` passed more than **7 days** ago, unless they are a policy/procedure/lesson or support a current durable summary.
- Low-value episodes/research/dreams only when all are true: older than **90 days**, confidence below **0.4**, not selected as reconstruction evidence in the last **60 days**, and no active policy/procedure/current-summary dependency.
- Unpromoted tentative dreams after **30 days**.
- Exact duplicate content after choosing a canonical node and recording a supersession edge; semantic “near duplicates” are report-only initially.

Age alone never archives a policy, procedure, user preference, lesson, or a node with an active durable dependent. Retrieval feedback's current hit proxy is not strong enough by itself to protect or evict a node (`brain/systems/memory/retrieval_feedback.py:45-106`), so first-generation utility should be “selected as evidence,” not “answer was correct.”

**Hard-delete only for erasure and retention obligations.** Normal forgetting never hard-deletes nodes, assertions, spans, sources, or edges. Hard deletion is reserved for authenticated user/tenant erasure, expired retention policy, legal/security requirements, or corrupt test data under an operator runbook. Where the obligation permits, retain a non-content tombstone/audit event. Where privacy requires full erasure, privacy correctly overrides reconstructability.

**Guarantee auditability.** A superseded fact stays auditable because its original source and spans remain, the old node/assertion remain marked stale/superseded, the replacement remains source-backed, and the `superseded_by` edge points to a curation-reason span. Current retrieval already hides the old fact without destroying it (`brain/platform/db/repositories/reconstructive_memory.py:284-298`). Existing curation tests demonstrate that the reason/source are retained while old content disappears from recall (`tests/test_reconstructive_memory.py:550-655`).

### Option comparison

| Option | Active graph shrinks | Recall gets organized | New subsystem | Migration | Primary risk |
|---|---:|---:|---:|---:|---|
| A — Graph Steward | Yes | Slightly | No | Low | False archival/supersession |
| B — Evidence-Bound Sleep | Yes | Yes | No; extends graph conventions | Moderate | Derived summary hallucination |
| C — Tiered Dreaming Memory OS | Yes | Yes, if routing works | Effectively yes | High | Summary collapse/tier thrash |

**Action from section 4:** treat Option A as the safe foundation, Option B as the target architecture, and Option C as deferred. Do not revive the flat `memories` model or the old 666-line pipeline.

## 5. Recommendation

Choose **Option B — Evidence-Bound Sleep**, delivered in stages. It is the only option that satisfies both halves of the ticket: remove stale active material **and** organize retained material so recall can improve. It preserves the deliberate source-backed stance, reuses the graph's validity/assertion/span/supersession primitives, and avoids the cost and identity confusion of a parallel tiered store.

Option A is the foundation, not a competing dead end. Ship its safest deterministic slice first, measure it, then add rolling synthesis. Do not make dream generation the first milestone: current deployment cannot run its reported SQL, current `main` silently drops its decay flag, and generated insights need an epistemic boundary before they can safely influence recall (`brain/jobs/pipelines/nightly_dream.py@00216ed^:30-60`; `brain/app/cli/memory.py:38-54`).

### First buildable slice: auditable expiry maintenance

One PR, after #403 is deployed:

1. Add one `nightly_memory_maintenance` step immediately after `memory_consolidation` in the actual scheduler plan and command mapping.
2. In one transaction, select only content nodes whose explicit `valid_until` is at least seven days past. Exclude policy/procedure/lesson kinds and nodes that support an active current summary. Do not attempt semantic deduplication, contradiction resolution, salience decay, or synthesis in this slice.
3. For each archived node, use the existing soft-archive mechanism and persist a `memory_curator` source/span stating the rule, policy version, target node, run ID, and reason. Delete nothing.
4. Record candidates, archived count, excluded-by-rule counts, errors, and duration in the consolidation run summary; use `memories_decayed` as the archived count until a better-named metric is migrated.
5. Leave retrieval logic unchanged initially: it already excludes `archived_at` nodes (`brain/platform/db/repositories/reconstructive_memory.py:284-298`).

The slice is successful when:

- The scheduler reaches and settles the new phase in staging.
- A first run archives exactly the eligible fixtures and a second run archives zero: it is idempotent.
- An expired eligible node is absent from recall, while an expired policy/procedure/lesson and a node supporting a current summary remain retrievable.
- The archived node's source, span, assertion, and existing edges still exist, and a curation source/span explains the action.
- No hard deletes occur, counts in `consolidation_runs` match the affected rows, and the run can report/dry-run before apply.

This first PR is intentionally narrower than the full policy. It establishes end-to-end scheduler reachability, audit evidence, safe forgetting semantics, metrics, and rollback-by-unarchive. The next slices would add report-only duplicate/supersession candidates, then evidence-bound rolling summaries, then isolated/expiring dreams.

**Decision:** adopt Evidence-Bound Sleep; begin with auditable expiry maintenance; retain source-backed reconstruction as the invariant; and make every later summary, supersession, archive, and dream visible in provenance and evaluation metrics.
