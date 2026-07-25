# PRD: Model And Effort Routing Layer

Status: SHIPPED — all slices deployed to illo-dev 2026-07-25 except Slice 5,
which is tracked in issue #482 and gated on a quiet coercion-log window. See
"Implementation Status" immediately below for the per-slice record.
Date: 2026-07-24
Owner: Reda
Related docs:

- `docs/cycles.md`
- `docs/configuration.md`
- `docs/architecture.md`

## Implementation Status

**Every slice is shipped and deployed to illo-dev except Slice 5.** The design
sections below describe the intended end state; this section is the record of
what actually landed and what is left. Read this first.

### Shipped And Deployed (2026-07-24 / 2026-07-25)

| Slice | PR | What landed |
|---|---|---|
| 1 | #463 | Cycle `model_override`/`thinking_override` finally route scheduled runs, sourced from the run's bound revision snapshot; one canonical `EFFORT_TIERS` vocabulary + contract test; model-override validation at the shared cycle command layer; migration `0037` (which also merged two pre-existing alembic heads) |
| 1.5 | #465 | One model: org default `openai/gpt-5.6-sol` in code and config (migration `0038`, which also cleared every cycle model pin); `required_openai_auth_mode` consolidated from three copies into one |
| 1.6 | #466 | Provider-generic effort rendering: `brain/platform/effort.py` owns the canonical ladder and per-provider renderings; transports render at the request boundary (`xhigh → max` on Anthropic, `none` → omit) |
| — | #467 | Cleared model pins in the **revision ledger**, not just the cycle row — without this, Slice 1's fix routed nothing |
| — | #469 | Ship `provider-alert-severity.json` in the image (silent alerts channel) + a guard that fails if any `deploy/compose` runtime asset is missing a `COPY` |
| — | #471 | Stop asking the Codex backend for `prompt_cache_retention`; every call had been paying a rejected round trip |
| — | #472 | `ILLO_AGENT_RUNNER_CONCURRENCY` passed into the worker container (was unreachable); set to 10 in `.env` |
| — | #486 | Worker restarts never leave a second worker behind; exactly-one-worker invariant asserted |
| 2 | #489 | `spawn_worker` gains `effort` and `model` (bare provider name = that provider's default); child policy **materialized at spawn**; requested + effective routing recorded and echoed |
| 3 | #488 | Caller-suppliable `effort` on headless asks (typed param, not metadata, so routing cannot be injected); skill-anchored runs fall back to `skills.thinking_tier` |
| 4 | #492 | fast/deep retired: central admission coercion turns any inbound `deep` into `fast` with a `deep_run_coerced` log; composer Mode group removed; model/effort default to a **workspace-default sentinel** that omits the keys so org defaults apply |
| — | #491 | Generic worker join primitive (`run.worker_continuation_queued`), opt-in per spawn via `join_parent` — the replacement for deep's fan-out → join → synthesize |
| 6 | #490 | One provider-aware catalog owner (`brain/platform/model_catalog.py`) replacing four drifted lists; dead marketplace/resolvers deleted; `normalize_model_name` name collision resolved; migration `0040` |
| 7 | #493 | Budget visibility from the real `agent_api_calls` ledger (the audit endpoint had been summing a `metadata->usage` key nothing writes); `effort` column added, migration `0041` |

### Not Shipped

**Slice 5 — remove the deep machinery (issue #482).** Its structural blocker is
gone: #491 shipped the join primitive, so deleting deep no longer removes a
capability. The remaining precondition is a **quiet window on the
`deep_run_coerced` log**, which needs real elapsed time after #492's deploy.
Check that log before proceeding; the deletion scope is in "Fast/Deep
Deprecation Plan" below.

### Corrections To Earlier Assumptions In This Document

Two beliefs that shaped the original design turned out to be wrong, and the
text below has been updated but is worth calling out:

- **GPT-5.6 Sol is not slow.** A controlled A/B against the live backend showed
  it equal to or faster than GPT-5.5 (2.2s vs 2.6s at medium, 1.7s vs 2.7s at
  high on ~30k-token requests). An earlier reading that suggested otherwise
  compared 5.5 on an idle system against 5.6-sol on a saturated one. Never
  infer model speed from production averages spanning a config change —
  workload mix and saturation dominate.
- **Effort is not a latency knob.** Slow production calls were genuine
  reasoning on heavy multi-turn work, not a tier problem.

## Problem Statement

Illo runs everything strong and hot, and almost none of it is intentional:

- **Interactive runs are pinned to the ceiling by the frontend.** The composer
  defaults to `openai/gpt-5.6-sol` at `xhigh`
  (`frontend/src/lib/features/cortex/controllers/runSettingsController.ts:19-30`)
  and always sends `model` + `effort` as explicit per-run metadata
  (`frontend/src/lib/stores/cortex.svelte.ts:1535-1559`), sticky in
  localStorage — so org defaults never apply to a browser run.
- **Scheduled runs ignore their configured routing.** Every production cycle
  carries a `thinking_override`, and the Reflex lane pins `gpt-5.4-mini`/`low`
  — none of it is honored (Finding 1). Cycle runs execute on org defaults:
  `gpt-5.5` at the implicit code default `high`.
- A GitHub webhook classification therefore burns like a coordinator digest,
  and the levers that should distinguish them are broken, fragmented, or dead.

The intended end state, mirroring the director/workhorse routing Reda uses in
Claude Code: **one model — `gpt-5.6-sol` — everywhere**, with **effort as the
only routed knob**: `xhigh` for judgment-heavy work, `high` for standard work,
`medium`/`low` for execution-heavy or reflex work, plus the ability to spawn
workers at explicitly lower effort, and escalation on failure instead of
defaulting to the ceiling. Cheaper models are not part of the design; the
availability fallback to `gpt-5.5` exists only for a 5.6-sol outage.

The stale "fast/deep mode" predates this thinking and should be retired, and
the audit shows a meaningful amount of model-selection code is dead and should
go with it.

## Current State Audit (2026-07-24)

Every claim verified against source in this repo; production claims verified
read-only against the illo-dev database on 2026-07-24; the whole audit was
re-verified by an adversarial cross-family review pass.

### How A Run Chooses Its Model And Effort Today

Model selection lives in one JSONB column, `agent_runs.model_policy`
(`brain/platform/db/models/agent_run.py:88`). Two funnels fill and consume it:

1. **Admission.** `admit_work` builds the run request;
   `model_policy_from_metadata` (`brain/systems/runs/work_intake.py:180-202`)
   reads only these metadata keys: model from `model`/`model_name`, thinking
   from `thinking_tier`/`effort`/`effort_level`/`thinking` (validated against
   `{none,low,medium,high,xhigh}`), provider from
   `provider`/`preferred_provider`/`model_provider`. Alternatively a caller may
   pass `payload["model_policy"]`, taken verbatim — it replaces metadata
   routing wholesale, not field-by-field (`work_intake.py:358-363`). Two
   sharp edges: `metadata_choice` silently skips an invalid higher-priority
   key and accepts a lower-priority one (`work_intake.py:164-177`), and the
   model value is not provider-qualified — only `:` is normalized to `/`
   (`work_intake.py:191-193`), so bare names like `gpt-5.4-mini` stay bare.
2. **Execution.** Recipes read the stored policy and fall back to org defaults:
   `recipes/fast.py:142-149` and `recipes/workers.py:93-99` use
   `model_policy.model/.thinking` else `default_run_model()` /
   `default_run_thinking()` (`recipes/shared.py:227-259`), which resolve org
   `memory_model_config.default_model` / `.default_thinking` and finally
   `DEFAULT_PROVIDER_MODELS` (`openai → gpt-5.5`, `anthropic →
   claude-sonnet-4-6`) and `DEFAULT_THINKING_TIER = "high"`
   (`model_policy.py:15-19`). The `provider` key is parsed at admission but
   read by no recipe — the effective provider is re-inferred from the model
   string at execution (`direct_agent.py:408,1427-1432`).

The resolved tier flows to both providers as an effort string — OpenAI
`reasoning={"effort": tier}` (`transports/openai_responses.py:598-604`),
Anthropic adaptive thinking + `output_config={"effort": tier}`
(`transports/anthropic.py:34-36`) — with caveats that matter to this PRD:
`none` is translated to omission (`direct_agent.py:499-503`), which on
Anthropic means the provider default, not a genuine floor; the tier string is
otherwise passed through unvalidated per provider (Anthropic's documented
effort values are `low/medium/high/max` — no `xhigh`); and non-reasoning
catalog entries (`gpt-4o*`, `gpt-4.1*`) would still get `reasoning` kwargs
attached. Today's `THINKING_MAP` is an identity map
(`model_policy.py:46-52`), i.e. there is no per-provider translation layer.
Model availability fallback exists and works: `gpt-5.6`/`gpt-5.6-sol →
gpt-5.5` (`brain/systems/runs/direct_loop/model_fallback.py:9-12`). Models
`gpt-5.5` and `gpt-5.6*` force the ChatGPT/Codex auth backend
(`brain/systems/runs/direct_agent.py:200-201`,
`brain/platform/integrations/llm.py:595-597,675-677`).

The effort vocabulary `{none,low,medium,high,xhigh}` is redeclared in at least
a dozen places: `model_policy.py:46-52`, `work_intake.py:20`,
`brain/systems/cycles/common.py:7`, `brain/systems/runtime_settings/models.py:30-36`,
`brain/systems/context/budget.py:41-47`, `brain/platform/db/schemas/skills.py:23,100,126`,
`brain/systems/runs/tool_catalog/handlers/common.py:48`,
`brain/systems/skills/bundles.py:32`,
`brain/systems/runs/tool_catalog/definitions/brain.py:226-250,904`, plus the
frontend copies (`runSettings.ts:33-39`, types). One canonical source must own
it, with contract tests covering the sites that cannot import it.

### Finding 1 — Cycle Overrides Are Configured But Not Honored (Live Bug)

Cycles carry `model_override` + `thinking_override` columns
(`brain/platform/db/models/cycle.py:52-53`), revisioned, validated
(thinking only — `cycles/common.py:29`), editable via API
(`app/api/routers/cycles.py:102-103,153-154`) and MCP `manage_cycle`. The
scheduler puts them into run metadata as keys `model_override` /
`thinking_override` (`cycles/prompts.py:97-98`), and admission passes metadata
only (`cycles/service.py:140`). But `model_policy_from_metadata` never reads
`*_override` keys — so **the overrides never reach `model_policy` and every
scheduled cycle run executes on org defaults.** The only live consumers are
narrow: auth preflight reads `model_override` to pick which credential to
validate (`cycles/auth_preflight.py:71-79`) — so preflight can validate a
credential for a model the run will not use — and the contract-repair pass
reads `model_override` for its repair call but pins `reasoning_effort=None`
(`cycles/contract_gate.py:495-543`). `thinking_override` steers nothing at
all.

Production receipts (illo-dev, 2026-07-24): all 9 cycles carry a
`thinking_override`; cycle 8 "GitHub Reflex" carries
`model_override=gpt-5.4-mini`, `thinking_override=low`. The latest runs of
cycles 2, 8, 9 (runs 2529, 2536, 2535 — all 2026-07-24) show
`model_policy = {}`. The Reflex lane believed to run on mini/low has been
running `gpt-5.5`/`high` every 15 minutes. The Coordinator configured `xhigh`
is running `high`. Older runs (June–Jul 7) show constant
`{"thinking": "high"}` even for cycles configured `medium`, so overrides were
likely never reliably honored; a further regression (between Jul 7 and Jul 24)
emptied the policy entirely. Also: cycle 5 stores the literal string
`"default"` as `model_override` — the field is free-text with no validation.

A related race: `cycle_run_metadata` reads overrides off the **live** `Cycle`
row at execution time (`cycles/prompts.py:89`), not from the revision snapshot
attached to the queued `CycleRun` — editing a cycle while a run is queued
changes that run's route. Slice 1 derives routing from the run's persisted
revision snapshot instead.

### Finding 2 — The Composer Pins Interactive Runs To The Ceiling

`DEFAULT_CORTEX_RUN_SETTINGS = {model: "openai/gpt-5.6-sol", effortLevel:
"xhigh"}` (`runSettingsController.ts:26-30`), persisted per browser in
localStorage, and the store stamps `model` + `effort`/`thinking_tier` into
every run's metadata unconditionally (`cortex.svelte.ts:1535-1559`). Explicit
metadata beats org config at admission, so **browser-initiated runs never
inherit workspace defaults, and `xhigh` is the ambient interactive tier.**
A returning user's stored picks also survive any later org-default change.
The fix (Slice 4) is a "workspace default" sentinel state that omits the keys
unless the human explicitly picks a model/effort.

### Finding 3 — Fast/Deep Selects The Loop, Not The Model

`execution_profile` selects the run recipe. Resolution: `profile_from_metadata`
(`work_intake.py:205-217`) → `RunProfile.FAST|DEEP`
(`brain/systems/runs/domain.py:13-15`) → recipe dispatch keyed on the stored
**recipe** (`brain/systems/runs/engine.py:330-334`) — note a `recipe` metadata
key overrides the profile independently (`work_intake.py:220-227`):

- `fast` → `FastRecipe` (`recipes/fast.py:128`): direct single-session loop,
  live streamed reply. This is what everything actually uses.
- `deep` → `DeepRecipe` (`recipes/deep.py:90`): plans a run graph (scout node,
  worker nodes, coordinator synthesis, phase barriers), waits for child
  completion and synthesizes in-run (`recipes/deep.py:465`), applies blocking
  verification (`verification/policy.py:23`, consumed only by
  `recipes/deep.py:535`), and runs phase-specific tool policies (synthesis is
  toolless with `max_turns=2`, `recipes/deep.py:603`).

It does **not** change model or thinking. Live `deep` senders are exactly two:
the frontend composer Mode toggle
(`frontend/src/lib/features/composer/domain/runSettings.ts:10-23`, default
`fast`; mode display also in `ThreadStageScreen.svelte:399,970`) and the idea
self-critique audit endpoint (`app/api/routers/cortex/_misc.py:850`). Every
backend sender hardcodes `"fast"` (`external_agents/service.py:1648`,
`inbound/service.py:1072,1256`, `slack/triggers.py:52`, `onboarding.py:101`,
`jobs/pipelines/aws_health_scan.py:82`), and chantier continuation propagates
the source run's profile forward (`chantier_continuation.py:267-276`).
`agent_runs.profile`/`.recipe` are the run-type discriminator columns
(`agent_run.py:82-83`); there is no dedicated `execution_profile` column and
no pydantic field — the value rides free-form metadata, e.g. through `/notify`
(`app/api/routers/cortex/_ideas.py:847`), so old clients can keep sending it
and retirement needs an admission-side coercion, not just sender cleanup.

One whole parallel resolver is dead code referenced only by the first test in
one file: `run_profile_policy` / `select_run_runtime` / `RunProfilePolicy` /
`RunRuntimeSelection` / `run_execution_profile` / `requested_run_profile`
(`brain/systems/runs/__init__.py:38-99` + `__all__` entries;
`tests/test_agent_runtime_modules.py:9-16` — the remaining ~890 lines of that
file are unrelated live regression guards and must stay).

**What deep still uniquely provides:** a generic blocking fan-out→join→
synthesize primitive. `spawn_worker` is fire-and-forget
(`definitions/workers.py:9`, handler returns a queued child,
`handlers/workers.py:328`); the only automatic rejoin is chantier-specific
(`chantier_continuation.py:180`), and the fast-recipe prompt explicitly defers
multi-wave synthesis to deep (`recipes/fast.py:44`). Retiring deep therefore
requires generalizing the join (Slice 5 precondition), not just deleting code.

### Finding 4 — spawn_worker Cannot Route

The `spawn_worker` tool schema
(`brain/systems/runs/tool_catalog/definitions/workers.py`) has no
model/effort/thinking parameter. The handler copies the parent's **stored**
policy verbatim: `model_policy=dict(parent.model_policy or {})`
(`tool_catalog/handlers/workers.py:289-309`; store fallback
`store.py:401,414-418`). Two consequences: a coordinator on `xhigh` fans out
workers at `xhigh` (no cheap readers, no cross-provider verifier — no
`worker_model` concept exists; `coordinator_model`/`coordinator_thinking`
exist only inside the deep/phase recipes, `recipes/deep.py:877-889`); and the
copy is of the *unresolved* policy, so when the parent ran on defaults the
child re-resolves org defaults at its own execution time — an org-config
change or availability fallback between spawn and execution silently changes
the child's route. Requested vs effective routing is invisible today, which
is how Finding 1 went unnoticed.

### Finding 5 — Headless Asks Force Medium And Carry A Dead Key

`create_headless_ask` (`external_agents/service.py:1591`) hardcodes
`payload["model_policy"] = {"tier": "standard", "thinking": "medium"}`
(`:1642`) — taken verbatim at admission, so callers cannot raise or lower
effort, and the `"tier"` key is read by nothing in the codebase. The bridge
ask schema has no effort field (`app/api/schemas/external_agents.py:116`;
route `agent_bridge.py:393-401`). The post-spread stamps
(`:1643-1655`) also force `execution_profile`/`recipe`/`headless`/
`tool_policy` — the security stamps are correct and must stay forced; only
routing fields should become caller-suppliable, via whitelist. `medium` is a
reasonable default for bridge asks; the forcing and the dead key are the
problems. The one scheduled path that does thread effort correctly is the AWS
health scan, which passes `thinking_tier: skill.thinking_tier`
(`aws_health_scan.py:82-84`) — proof the per-skill tier pattern works when a
run has a skill anchor.

### Finding 6 — Dead And Stale Model-Selection Code

- **Routing marketplace** (`brain/systems/routing/marketplace.py`):
  `apply_marketplace_route` / `resolve_marketplace_routing` (:1179, :1239) are
  referenced only by exports and their own tests; the
  `selected_reasoning_effort` DB column (`db/models/routing.py:68`) is written
  by nothing live.
- `brain/systems/runs/modeling.py:resolve_model` — zero callers.
- Skill runtime resolvers `resolve_skill_model` / `resolve_skill_runtime` /
  `async_resolve_skill_runtime` (`model_policy.py:441-573`) feed only the dead
  marketplace; the sync skill-row loader is a stub returning `None`
  (`model_policy.py:188-190`). The `skills.thinking_tier` column is live data
  used by exactly one caller (AWS health scan).
- Model catalogs disagree: `PROVIDER_MODEL_OPTIONS` (`model_policy.py:20-44`)
  still lists `o3-mini`, `gpt-4o*`, `gpt-4.1*`, `gpt-5.2`, `gpt-5.3-codex*`;
  the context-window table (`context/budget.py:18-39`) carries
  `gpt-5.1-codex-mini` which the catalog and pricing table don't; the
  runtime-settings UI list (`runtime_settings/models.py:15-28`, unprefixed,
  OpenAI-only, rejects non-OpenAI at `:39-51`) and the composer list
  (`runSettings.ts:25-31`, provider-prefixed) are two more hand-maintained
  copies with different value shapes. The frontend default model was
  `openai/gpt-5.6-sol` while the backend default was `gpt-5.5` (resolved in
  Slice 1.5 — both are `gpt-5.6-sol` now).
- The Anthropic side is stale: default `claude-sonnet-4-6`
  (`model_policy.py:17`), a hardcoded `claude-sonnet-4-6` key-verify ping
  (`cortex/_key_utils.py:125`), and no Anthropic-valid effort translation
  (see resolution caveats above).
- Usage ledger confusion: the audit endpoint sums `metadata["usage"]` per run
  (`_misc.py:773-782`), but nothing under `brain/` writes that key. The real
  durable ledger is `agent_api_calls`
  (`brain/platform/db/models/agent.py:62`) with on-demand aggregation in
  `brain/systems/runs/token_usage.py` — which does not record effort.
- `memory_model_config` still gets legacy keys scrubbed on every save
  (`runtime_settings/models.py:88-96`), the tail of the removed
  intelligence-tier system (alembic `0021_remove_intelligence_tiers`).

### What Already Works (Preserve)

- Effort-string plumbing to both providers (with the `none`/`xhigh` caveats
  above to fix via translation).
- Org `memory_model_config` (`default_provider`/`default_model`/
  `default_thinking`) and the runtime-settings card writing it.
- Availability fallback `gpt-5.6* → gpt-5.5` and auth-mode gating.
- The composer's Model + Effort pickers as an explicit-override surface
  (`runSettings.ts:25-39`) — minus their ambient-default behavior (Finding 2).
- Utility calls already at reflex effort: title generation `low`
  (`title_generation.py:20`), final-reply checker `low`
  (`final_reply_checker.py:981`), memory truth adjudication `low`
  (`truth_maintenance.py:837`), nightly dream `low` / reflect `high`.
- The durable per-call token ledger (`agent_api_calls` + `token_usage.py`
  aggregation) and pricing in `MODEL_PRICING_PER_MILLION`
  (`model_policy.py:131-145`).

## Solution

### Principles

1. **One model; effort is the only routed knob.** Illo runs `gpt-5.6-sol`
   everywhere and varies reasoning effort. Cheap work is cheap because it
   thinks less, not because it runs on a weaker model: a mini model on a
   reflex lane trades quality for a saving the effort tiers already deliver,
   and it splits the fleet across two credential paths (only `gpt-5.5` and
   `gpt-5.6*` reach the ChatGPT/Codex backend — anything else needs an API
   key). Model overrides survive for one genuine exception: routing a
   verifier to the *other provider* for independent review. No cycle pins a
   model (Reda, 2026-07-24).
2. **Declarative routing, no LLM router.** Tiers come from configuration
   (org default, cycle override, skill tier) and from the acting agent's
   explicit choice at spawn time. We never spend a model call deciding which
   model to call.
3. **Escalate on failure, don't default to the ceiling.** Start at the tier the
   work class deserves; rerun higher when the result fails or disappoints.
   Availability fallback stays mechanical; quality escalation stays in the
   coordinator's hands, and both requested and effective routing are recorded
   so escalation is auditable.
4. **One vocabulary, one owner, validated per provider.** The canonical tier
   set is defined once; providers get an explicit translation (not an identity
   pass-through); invalid declarations are rejected at admission, not silently
   skipped.
5. **Provider-generic core (Reda, 2026-07-24).** Illo happens to run on the
   ChatGPT/Codex backend today, but nothing outside the provider layer may
   know that. Routing surfaces (cycles, spawn_worker, headless asks, org
   config, composer) speak canonical effort tiers and provider-prefixed model
   ids only. All provider-specific knowledge — native effort vocabulary and
   its rendering, model catalog, auth requirements, availability fallbacks,
   pricing, context windows — lives in the provider layer: the effort
   renderings in `brain/platform/effort.py` (transports render at the request
   boundary) and, as Slice 6 lands, the per-model catalog contract.
   Connecting a new provider = a transport + an effort rendering + catalog
   entries; zero changes to routing surfaces. The completeness contract test
   fails any provider whose rendering doesn't cover the full ladder.
6. **Compatibility constraints.** The two-provider architecture
   (`providers.py`) and `memory_model_config` schema are untouched; cycle
   `model_override`/`thinking_override` semantics are preserved (and finally
   honored). No feature gates: each slice is live behavior on merge, verified
   pre-merge on the branch; rollback is revert.

### The Effort Ladder

| Tier | Meaning | Illo work classes |
|---|---|---|
| `xhigh` | Maximum reasoning; judgment where a wrong call is expensive | Coordinator digest/triage verdicts (cycle 2), self-critique audit analysis, adversarial review, prioritization calls |
| `high` | Default for real work | Interactive chat/thread runs, chantier implementation, promotion-readiness checks (cycle 9), contract repair |
| `medium` | Execution-heavy, clear-spec | Headless bridge asks (status quo), tracker writes, sweeps, data pulls, formatting, readbacks |
| `low` | Reflex: classify, filter, ack | GitHub Reflex event lane (cycle 8 — `low` effort on the one model, no mini pin), Slack monitored-channel triage, title generation, final-reply checker, truth adjudication |
| `none` | No reasoning pass | Pure templating/exports |

Org default stays `high` (made explicit in config rather than implicit in
code). `xhigh` is claimed by declaration (cycle override, skill tier, spawn
parameter, composer pick) — never the ambient default, which also means the
composer stops defaulting to it (Finding 2 fix).

**Per-provider rendering** (shipped — replaced the identity `THINKING_MAP`):
`brain/platform/effort.py` owns the canonical ladder and a per-provider
rendering table; transports render at the request boundary. Today: OpenAI
`reasoning.effort` `low/medium/high/xhigh`, `none` → omit; Anthropic
`output_config.effort` `low/medium/high` with `xhigh → max`, `none` → omit.
Provider-native values pass through untranslated so deliberate native usage
keeps working, and a contract test fails any provider whose rendering doesn't
cover the full ladder. Per-model effort support (non-reasoning models get no
reasoning kwargs at all) lands with the Slice 6 catalog contract.

### Routing Surfaces

Per-field precedence — `model` and `effort` resolve independently, highest
wins, invalid values rejected at admission with an actionable error (no silent
fall-through): explicit per-run declaration (composer explicit pick, cycle
override via revision snapshot, spawn parameter, headless-ask effort) →
skill-declared tier (where a run has a skill anchor) → org default → code
default. Resolution provenance is recorded on the run (requested source and
value per field), and the effective post-fallback route is recorded at
execution. The `provider` policy key is retired: provider follows the
canonical provider-prefixed model id, as execution already re-infers it.

- **Org default** — unchanged mechanism (`memory_model_config`), now
  `openai/gpt-5.6-sol` with an explicit `default_thinking: high` (migration
  `0038`); the availability fallback to `gpt-5.5` covers a 5.6-sol outage.
- **Cycles** — `model_override`/`thinking_override` keep their exact meaning
  and start working (Slice 1), sourced from the run's immutable revision
  snapshot. In practice only `thinking_override` is used: reflex cycles
  declare `low`, judgment cycles `xhigh`, everything else inherits.
  Migration `0038` clears every existing model pin.
- **Per-spawn worker overrides** (Slice 2) — `spawn_worker` gains an `effort`
  parameter (the everyday knob: a coordinator at `xhigh` fans out `low`/
  `medium` readers on the same model) plus an optional `model` reserved for
  the cross-provider verifier — both validated, merged field-by-field over
  the inherited parent policy.
- **Headless asks** (Slice 3) — caller-suppliable `effort` through a
  whitelist; default `medium`; the dead `tier` key dies; security stamps stay
  forced.
- **Skill tiers** (Slice 3) — `skills.thinking_tier`, today honored only by
  the AWS scan, becomes the standard fallback for skill-anchored headless
  runs (anchor = explicit skill reference in the spawn, as in the scan).
- **Humans** — the composer keeps Model + Effort pickers with a new
  "workspace default" state that omits the keys entirely; explicit picks send
  them (Slice 4). Only the Mode (fast/deep) group retires.

### Escalate On Failure, Not Default To Ceiling

Mechanics in this PRD deliberately stop at "make escalation possible and
visible" — no automatic tier bumps in v1:

- Runs persist **requested** routing (with provenance) at admission and
  **effective** routing (post-fallback, per provider) at execution;
  `spawn_worker` results echo the child's effective model + effort. Today
  effective values are invisible, which is how Finding 1 went unnoticed.
- Guidance (spawn_worker tool description in-repo; doc 1155 / cycle missions at
  runtime) instructs: respawn failed or weak work one tier higher, or on the
  stronger pattern (other provider for review); never start a fleet at the
  ceiling.
- Availability fallback (`model_fallback.py`) remains the only automatic
  substitution.

Automatic quality escalation (e.g. auto-rerun at +1 tier on run failure) is
explicitly deferred until the visibility above produces real failure/retry
data.

### Run Budget Visibility (Optional Slice)

The durable ledger already exists: `agent_api_calls` rows per call, aggregated
on demand by `token_usage.py`. Slice 7 builds on that (not on the unwritten
`metadata->usage` key the audit endpoint currently reads): worker results
include child usage; `manage_cycle` gains a usage-summary read (per-cycle
tokens/cost over a window); the cycle self-review line includes the run's
burn; `agent_api_calls` optionally gains an `effort` column so burn can be
broken down by tier. This gives Illo sight of its own spend and gives Reda a
before/after measure of this whole PRD.

## Fast/Deep Deprecation Plan

What retires, in dependency order:

1. **Dead parallel resolver** — delete `brain/systems/runs/__init__.py:38-99`
   block + exports and the first test only in
   `tests/test_agent_runtime_modules.py` (the rest of that file is unrelated
   live guards). Zero-risk.
2. **Central admission coercion** — `profile_from_metadata` /
   `recipe_for_profile` coerce any inbound `deep` profile/recipe to `fast`
   with a structured warning log. This is the actual off-switch: the value
   rides free-form metadata (`/notify`), so old clients and stale localStorage
   can keep sending it regardless of sender cleanup.
3. **The user-facing mode** — remove the composer Mode group
   (`runSettings.ts:10-23,55-60`), `setExecutionProfile`/localStorage
   persistence (`runSettingsController.ts`, `cortex.svelte.ts:122,195-218`),
   mode display in `ThreadStageScreen.svelte:399,955-993`, and stop sending
   `execution_profile` from the frontend. Model + Effort pickers stay (gaining
   the workspace-default state).
4. **The last deep sender** — the audit endpoint (`_misc.py:850`) becomes a
   fast run with explicit `thinking_tier: xhigh` metadata (single-analysis
   task; the run graph adds nothing).
5. **The deep machinery** (Slice 5, decision- and precondition-gated) — after
   a quiet period on the coercion log AND a generic join primitive replacing
   deep's fan-out→join→synthesize (generalize chantier continuation to any
   parent whose workers complete, or an explicit join/continuation tool):
   delete `DeepRecipe`, `ScoutRecipe`, `phase_barrier`,
   `verification/policy.py` (deep-only consumer); simplify `engine.py`
   dispatch; fix the fast prompt language that defers synthesis to deep
   (`recipes/fast.py:44`); remove frontend run-graph presentation branches
   (`cortexRunPresentation.ts`, `threadStreamAdapter.ts`) and local-preview
   mocks. **Back-compat:** keep `RunProfile`/`RunRecipe` legacy values
   readable — store conversion constructs enums from stored strings
   (`store.py:130`) and historical `deep`/`scout` rows must not break reads;
   drain or cancel nonterminal deep runs before removing registered recipes
   (`engine.py:330` fails hard on an unregistered recipe). The
   `agent_runs.profile`/`recipe` columns stay (worker runs still use
   `recipe="worker"`); backend senders stop stamping `execution_profile`/
   `recipe`; the coercion shim then logs anything unexpected until removed.
6. **Docs** — update the fast-mode reference at
   `prd-universal-thread-context-ingress.md:367`.

Migration risk is contained because the audit found no external senders of
`deep` beyond the two listed, no dedicated DB column, and no schema field —
but the free-form metadata path is exactly why step 2 precedes everything
user-visible.

What fast/deep was *for* is replaced by the routing layer: "deep" work is a
coordinator run (at `xhigh` if declared) that fans out `spawn_worker` calls at
appropriate tiers and rejoins via the generalized continuation — the pattern
chantier execution already proves out.

## Implementation Slices

Each slice is a separate PR, independently shippable and verifiable, no
feature gates. Suggested order; 1–3 are the core value.

### Slice 1 — Honor Cycle Overrides + One Validated Vocabulary (bug fix)

- Cycle admission passes an explicit `payload.model_policy` built from the
  **run's revision snapshot** (not the live cycle row): `{model, thinking}`
  from the snapshotted overrides, model canonicalized to provider-prefixed
  form via `normalize_model_name`. `*_override` metadata keys remain for
  provenance/display. (Rejected alternative: teaching
  `model_policy_from_metadata` the `*_override` keys — keeps two vocabularies
  alive in the generic parser.)
- Validate `model_override` at every write path (cycles API, `manage_cycle`
  MCP + tool handler) against the catalog; normalize `""`/`"default"` to
  NULL; one-time data fix for cycle 5's literal `"default"`.
- Single canonical `EFFORT_TIERS` source in `model_policy.py` with the
  per-provider rendering table; importable sites import it; non-importable
  sites (tool JSON schemas, frontend) get a contract test asserting they match.
- Invalid values stop passing silently: hard validation at the sources we own
  (cycle write paths, the cycle-built `payload.model_policy`); the generic
  metadata parser warn-logs invalid explicit values instead of silently
  skipping to a lower-priority key (`metadata_choice`) — arbitrary chat
  metadata must not start hard-failing admissions.
- Tests: snapshot-sourced policy lands on the run; queued-run isolation from
  live cycle edits; absent overrides fall through to org defaults; invalid
  model rejected at write; vocabulary contract test.
- Live receipt after deploy (with Slice 1.5 applied): bump cycle 8
  `next_run_at`, confirm the new run's `model_policy = {"thinking": "low"}`
  on the org-default model, and cycle 2 lands `xhigh`.

### Slice 1.5 — Single Model, Shared Auth-Mode Rule (shipped)

Shipped alongside Slice 1 after Reda's single-model call (2026-07-24):

- Org default model is `openai/gpt-5.6-sol` in code
  (`DEFAULT_PROVIDER_MODELS`) and in org config (migration `0038`, which also
  writes `default_thinking: high` explicitly and clears every cycle
  `model_override`).
- `required_openai_auth_mode` moved into `model_policy.py` as the single
  owner, replacing three copies. The cycle-preflight copy recognized only
  `gpt-5.5`, so on a GPT-5.6 default it validated an interchangeable
  credential while the run required ChatGPT/Codex — the preflight's actionable
  "reconnect OpenAI" block would have stopped firing exactly when the default
  moved to 5.6-sol. A contract test now fails if any module redeclares the
  rule.

### Slice 1.6 — Provider-Generic Effort Rendering (shipped)

Shipped after Reda's provider-genericity direction (2026-07-24):

- Canonical ladder + per-provider renderings moved to the dependency-free
  leaf `brain/platform/effort.py` (`model_policy` re-exports, so importers
  are unchanged); both transports render canonical tiers at the request
  boundary (`render_reasoning_effort`), fixing the latent `xhigh`-on-Anthropic
  failure (`xhigh → max`, `none` → omit).
- The misleading identity `THINKING_MAP` is gone; its uses were membership
  checks, now against `EFFORT_TIER_SET`.
- Completeness contract test: every provider rendering must cover the full
  canonical ladder; native values pass through untranslated.

### Slice 2 — spawn_worker Effort Overrides (SHIPPED, #489)

- Optional `model` + `effort` parameters on the tool schema; handler validates
  (catalog + tier set + provider-supported efforts) and merges field-by-field
  over the inherited policy. The `model` value may be a provider-prefixed id
  or a bare provider name meaning "that provider's default model" — the
  provider-generic spelling of the cross-provider verifier.
- Child policy is **materialized at spawn**: when inheriting, the parent's
  effective values (not the unresolved dict) are written to the child, so an
  org-config change between spawn and execution cannot silently reroute a
  child.
- Runs persist requested routing + provenance at admission and effective
  routing at execution; worker results echo the child's effective model +
  effort.
- Tool description teaches the ladder, the director/workhorse split, and the
  cross-provider verifier pattern (prefixed model id).
- Tests: override honored; inherit materializes parent effective values;
  invalid model/effort rejected; cross-provider spawn resolves the right
  transport.

### Slice 3 — Headless Ask Effort + Skill Tier Wiring (SHIPPED, #488)

- Bridge ask schema (`app/api/schemas/external_agents.py`) gains an optional
  `effort` field; the route plumbs it; `create_headless_ask` builds its
  policy from canonical keys with a **whitelist** — callers may set routing
  fields only; `headless`/`tool_policy`/profile stamps stay forced; default
  remains `medium`; the dead `"tier"` key dies.
- Skill-anchored headless spawns resolve `skills.thinking_tier` when set
  (generalize the AWS-scan pattern; anchor = explicit skill reference on the
  spawn); delete the dead sync skill-row stub. Generic asks have no skill
  anchor and keep the `medium` default.
- Tests: effort passthrough; whitelist blocks non-routing overrides;
  skill-tier fallback ordering (explicit effort > skill tier > medium).

### Slice 4 — Retire The Fast/Deep Mode Surface + Composer Defaults (SHIPPED, #492)

- Deprecation-plan steps 1–4: dead resolver deletion (first test only),
  central deep→fast coercion with logging, composer Mode removal (incl.
  `ThreadStageScreen` mode consumers), audit endpoint migrated to fast +
  `xhigh`.
- Composer Model/Effort pickers gain the "workspace default" sentinel that
  omits metadata keys; stored localStorage picks migrate to the sentinel once
  (one-time normalization), ending the ambient `xhigh` (Finding 2).
- After this PR no surface can create a `deep` run (coercion guarantees it);
  recipes remain in-tree, unreachable, with the coercion log as the watchdog.

### Slice 5 — Remove The Deep Machinery (NOT SHIPPED — issue #482)

- Precondition A: quiet coercion log over an agreed window.
- Precondition B: generalized worker join/continuation shipped (chantier
  continuation promoted to a generic parent-rejoin, or an explicit join tool)
  and the fast prompt's synthesis language updated.
- Then deprecation-plan step 5 in full: recipe/verification deletion, engine
  simplification, frontend run-graph presentation cleanup, enum read
  tolerance for historical rows, nonterminal-deep drain, doc update.

### Slice 6 — Stale Model Code Cleanup + Catalog Contract (SHIPPED, #490)

- Delete the routing marketplace surface + its tests; drop
  `selected_reasoning_effort` (alembic, following the
  `0021_remove_intelligence_tiers` precedent) or leave the column dormant
  (Open Question 5). Delete `modeling.resolve_model`.
- One backend-owned, provider-aware catalog contract serving every picker:
  canonical prefixed id, label, provider, supported effort tiers, auth
  requirement (ChatGPT/Codex vs API key), availability-fallback target, and
  default provenance. Runtime-settings endpoint serves it (replacing the
  unprefixed OpenAI-only list + its non-OpenAI rejection where org policy
  allows); the composer consumes it instead of hardcoded `MODEL_OPTIONS`;
  frontend fallback default aligns with the backend default.
- Prune retired entries from `PROVIDER_MODEL_OPTIONS`/pricing/context tables;
  reconcile drift (`gpt-5.1-codex-mini`, `gpt-5.3-codex`).
- Refresh the Anthropic lineup (default + catalog + key-verify ping model) to
  the current generation, including validated effort values (`max` vs
  `xhigh`) at implementation time.

### Slice 7 — Run Budget Visibility (SHIPPED, #493)

- Aggregate from `agent_api_calls` via `token_usage.py` (the real ledger —
  not the unwritten `metadata->usage` key): worker results include child
  usage; `manage_cycle` usage-summary read; cycle self-review line includes
  run burn; optional `effort` column on `agent_api_calls` for per-tier
  breakdowns; fix or retire the audit endpoint's stale `metadata["usage"]`
  read.

Runtime rollout: the org-default and cycle-pin changes ship as migration `0038`
rather than manual production writes. The one remaining runtime step is
updating doc 1155 / cycle missions with the routing guidance (Illo-owned prose,
versioned at runtime).

## Operating Notes For Whoever Picks This Up

Hard-won specifics that are not obvious from the code.

### Where routing is decided now

`agent_runs.model_policy` is still the single carrier (`{model, thinking}`).
Precedence: explicit per-run declaration (composer pick, cycle override via
revision snapshot, `spawn_worker` parameter, headless-ask effort) → skill tier
where a run has a skill anchor → org `memory_model_config` → code default in
`DEFAULT_PROVIDER_MODELS`. Requested routing with provenance is recorded at
admission; effective post-fallback routing is recorded at execution.

### Traps that have already bitten

- **Cycles route from the revision snapshot, not the live cycle row.** Changing
  `cycles.model_override` alone changes nothing for future runs — the newest
  `cycle_revisions` row is what a run snapshots. Migration `0039` exists purely
  because `0038` missed this.
- **Verify the consumer, not the sender.** Two separate bugs this cycle passed
  their own tests while routing nothing, because the fix touched the value the
  sender holds rather than the one the consumer reads. When changing routing,
  assert on the resulting `AgentRunRequest`/run row, never on the event payload.
- **Alembic heads.** This repo has had multiple two-head incidents, including
  two in this effort (two `0036` heads found in production, and Slices 6 and 7
  independently claiming `0040`). After adding any migration run
  `venv/bin/python -m alembic heads` and expect exactly one. A guard test also
  checks the docstring `Revision ID:` matches the identifier.
- **The Codex backend is not the OpenAI Responses API.** It rejects
  `prompt_cache_retention` and requires `stream=True`. Only `gpt-5.5` and
  `gpt-5.6*` route there (`required_openai_auth_mode`); anything else needs an
  API key, which is a second reason not to pin cheap models per lane.
- **`upgrade.sh` does not recreate `slack-connector`** (it is on the `slack`
  compose profile), so the Slack lane silently runs old code until recreated by
  hand. That is how #469's packaging bug stayed latent for weeks.
- **Frontend changes need `node_modules`.** `npm run check` cannot run without
  it, and a type error shipped to CI once because of that. The main repo
  checkout has one; symlink it into a worktree to verify locally.

### Verifying a routing change on illo-dev

Read-only inspection recipes live in the deploy notes. The quickest end-to-end
receipt is a naturally-due cycle: bump nothing, wait for the next fire, then
check the resulting run's `model_policy` and the model actually used:

```sql
SELECT cr.id, ar.id, ar.model_policy, ar.status
FROM cycle_runs cr JOIN agent_runs ar ON ar.id = cr.run_id
WHERE cr.cycle_id = 8 ORDER BY cr.id DESC LIMIT 1;

SELECT DISTINCT model FROM agent_api_calls WHERE run_id = <that run>;
```

Prefer watching a naturally-due cycle over bumping `next_run_at`, which risks a
catch-up flood.

### Deploy shape

`git pull && ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_SECONDS=300
deploy/scripts/upgrade.sh --build --no-pull`, then recreate `slack-connector`
by hand. If the worker drain times out the script now refuses the swap, removes
the temporary handoff worker and exits non-zero, leaving exactly one worker on
the **old** code — finish with `docker rm -f illospace-worker-1` and
`compose up -d --no-deps worker`. Runs interrupted that way are requeued by the
stale-run reaper (`agent_run_stale_interrupted_requeued`, 300s threshold);
that path was exercised repeatedly and lost no work.

## Out Of Scope

- Any LLM-based dynamic router or per-request model classifier.
- New providers beyond the integrated two; provider plugin architecture.
- Embeddings model routing (`runtime_settings/embedding_registry.py` — separate
  subsystem, untouched).
- Automatic quality escalation / auto-retry tier bumps (deferred until Slice 2
  visibility produces data).
- Per-user entitlements, quotas, or spend enforcement.

## Decisions (Reda, 2026-07-24)

1. **Deep machinery: delete it** (Slice 5), gated on the coercion quiet
   period and the generic join/continuation primitive shipping first.
2. **Cycle 2 keeps `xhigh`.** Slice 1 makes the configured override real;
   coordinator digest/triage is the judgment class the ladder assigns
   `xhigh`. Burn becomes observable via Slice 7 later.
3. **Composer gets the "workspace default" sentinel** (Slice 4): browser runs
   omit model/effort keys and inherit org defaults unless a human explicitly
   picks; one-time migration normalizes stored localStorage picks.
4. **Skill tiers apply to skill-anchored headless runs only** (Slice 3),
   matching the AWS-scan pattern. Interactive runs unaffected.
5. **Marketplace column** (default, override anytime): drop
   `selected_reasoning_effort` via migration in Slice 6, following the
   `0021_remove_intelligence_tiers` precedent.
6. **Budget slice** (default, override anytime): deferred until Slices 1–4
   land; revisit with real routing in place so it measures the after-state.
