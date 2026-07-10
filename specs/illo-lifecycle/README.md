# Illo lifecycle overhaul — freshness, task-type, and assignment

## Next Agent Prompt

**Status (2026-07-10):** Slice 5 (deploy-state & post-deploy verification,
[slices/05-deploy-state.md](slices/05-deploy-state.md)) is **code-complete on
this branch**: pure core + sweep + verification + `check_fix_deploy_state`
tool + prose ladder (SKILL.md/monitor prompt), Codex-implemented and
Claude-reviewed, fast suite green (3438 passed, 9 pre-existing
`test_llm_worker` env failures), and the ladder **verified live** by two
READ-ONLY illo-dev dry runs (952-pattern): run 1088 pre-promotion → expected
noise / zero refiles / zero pings + promotion recommendation; run 1089
post-promotion → reopen #904 + escalate to the builder (axel-havard). Live
activation gates below. The doc-1155 delta
([slices/05-doc-1155-delta.md](slices/05-doc-1155-delta.md)) is **applied**:
live doc v6 (2026-07-10) carries both this slice's ladder and the parallel
digest-contract edits, verified identical to the bundled SKILL.md.

**Prior status (2026-07-09):** All five slices **implemented and shipped in PR #276**
(2 commits). Pure logic cores are unit-tested (**105 focused tests green**); an
independent adversarial review found ~10 MED/LOW findings, all **fixed and pushed**
(commit `76ed45f`). Full suite **3280 passed** (1 pre-existing env-only failure,
unrelated). Live read-only validation on `illo-dev` confirmed the sync cycle
(id 2, `0 8,13` "Uwear Ticket Coordinator Check-ins") and that "doc 1155" is live
`domain_records` id 1155 — the deterministic rules **preserve** its Reda/Axel/JB
routing rather than override it. What remains is deploy/live wiring a dev checkout
cannot exercise (GitHub App, live DB/env, Slack, scheduler registration) —
enumerated per slice below.

**Key implementation decisions (differ from the original slice text):**
- **Persistence via JSONB, not migrations.** `task_domain` + the assignment
  decision live in `ideas.agent_details` (existing JSONB); freshness rides the
  record `data` / envelope hints. This avoids risky central-table migrations that
  can't be validated without a live DB. Add a column later if querying needs it.
- **Unclaimed pool enacted via a sentinel owner (env-gated).** Rather than loosen
  `ideas.user_id` (`NOT NULL`), owner-less items park on a configured pool user
  (`ILLO_UNCLAIMED_POOL_USER_ID`); teammates claim by reassigning. Unset → the old
  skip (no behavior change). The notify digest counts the pool. Activation: create
  a pool user + set the env var. (A `NOT NULL`-drop migration is the purer
  long-term option if preferred.)
- **New router/cycle are additive and NOT auto-registered**, so they can't affect
  app startup or the scheduler until deliberately wired.

**Live-gated activation checklist (per slice):**
- **Slice 0:** bump the sync cycle cadence; `manage_soul`/`manage_inbound` on live
  Illo. (SOUL author-nudge default is already in code.)
- **Slice 1:** activate GitHub App (PR #265); set `ILLO_GITHUB_WEBHOOK_SECRET` +
  `ILLO_GITHUB_CONNECTION_ID`; `app.include_router(github_webhooks.router)`;
  configure the source policy + domain projection; add a reconciliation cycle.
- **Slice 2b:** surface `task_domain` in the `illo_read`/`domain.inspect` output.
- **Slice 3:** set `ILLO_BUSINESS_OWNER_USER_ID` (+ optional
  `ILLO_PRODUCT_OWNER_USER_ID`, `ILLO_REPO_OWNERS`) to turn on rule routing —
  **must be valid user ids in the org, or those triage events fail** (Idea.user_id
  is a NOT-NULL FK); create an unclaimed-pool user + set
  `ILLO_UNCLAIMED_POOL_USER_ID` to enact the pool; migrate live "1155" prose into
  the rule table.
- **Slice 4:** register a cycle calling `run_notify_cycle(session, org_id=…,
  channel_id=<team channel>, since=<last_run_at>)`; add the urgent-bypass hook at
  triage completion.
- **Slice 5:** set `ILLO_DEPLOY_SWEEP_REPOS` (watched repos) to arm the
  promotion sweep (+ optional `ILLO_DEPLOY_SETTLE_MINUTES` /
  `ILLO_DEPLOY_QUIET_HOURS` overrides); ensure the Slice-1 source
  policy/projection **covers `pull_request` events** — the sweep hook runs on
  the post-projection ingest path, so unprojected merge envelopes never reach
  it; ~~apply the deploy-state ladder delta to live doc 1155~~ **done — doc
  v6 (2026-07-10) carries it**; verification tick rides Slice 4's cycle
  registration; optional Rollbar read-only token (Vault) for API-backed quiet
  checks — **Reda decided 2026-07-10: skip for now**, Slack-inference default
  stands.

**Before ending your pass:** update this section.

### Global TODO — code complete ✅ (live wiring pending)
- [x] Reda confirmed the four _Decisions_ (2026-07-08)
- [x] Slice 0 — SOUL author-nudge default in code (cadence/policy = live steps)
- [x] Slice 1 — pure cores + GitHub router (`github_webhooks.py`); live: App + config + register
- [x] Slice 2a — task_domain module + domain-aware self-assess
- [x] Slice 2b — run.py + triage (`agent_details`) + orchestrate/blocker/evals; live: read surface
- [x] Slice 3 — resolve_owner + rules, wired into triage; live: env owner id + pool nullability
- [x] Slice 4 — notify decision/digest + `run_notify_cycle`; live: register cycle + urgent hook
- [x] Review pass — 6 self-review bugs found + fixed (98 focused tests green)
- [x] Slice 5 — deploy-state & post-deploy verification (spec + code +
  prose, 2026-07-10, from the Rollbar #2206 → #904 re-fire case; ladder
  dry-run-verified on illo-dev, runs 1088/1089); live: env + doc 1155 delta
  + Rollbar-token decision
- [ ] Live activation (per checklist above) — Reda / infra

---

## Goal

After a full day of live triage/coordination with Illo, three lifecycle problems
surfaced. This feature fixes all three, reusing infrastructure that already
exists rather than building parallel paths.

1. **Freshness.** A twice-daily "cycle" polls GitHub into Illo's store, so
   teammates' agents read state up to ~12h stale, with no signal of how stale.
2. **Scope.** Business and product-management tasks are starting to land in
   repos. Illo's identity/triage layer is domain-neutral and won't reject them,
   but every place it _concretely types a task_ is engineering-only, so non-eng
   work gets a nonsensical or empty quality bar and eng-shaped handling.
3. **Assignment.** There is no task-type/label/repo→person mapping anywhere.
   Assignment is soft prose the model interprets, which is why the wrong-owner
   fumble recurs — it's structural, not a one-off bug.

## The core idea

Two separations drive the whole design:

- **Data freshness ≠ communication judgment.** Webhooks keep the domain true
  within seconds (no judgment). The cycle stops being a data pump and becomes a
  more-frequent _notify-loop_: read what changed, decide whether it's worth
  telling the team, Slack it or stay quiet.
- **Typed task-type is the spine for both scope and assignment.** Once
  `task_type` is a real field instead of a guess, non-eng work gets the right
  checklist/handling _and_ high-stakes routes (business/PM → Reda) become
  deterministic code rules instead of interpreted prose. Task-type and
  assignment are one project, not two.

## Context: what already exists (measured, from recon)

- **Event pipeline (reuse this):** `POST /webhooks`
  ([brain/app/api/routers/webhooks.py](../../brain/app/api/routers/webhooks.py))
  → `submit_inbound_envelope()`
  ([brain/systems/inbound/service.py](../../brain/systems/inbound/service.py)) →
  `inbound_events` → `_apply_domain_projection()` (service.py ~570–673) →
  `domain_records`. Already recognizes GitHub delivery headers
  (`x-github-delivery`, webhooks.py:23). This is the _primary designed path_.
- **Domain store (source of truth):**
  [brain/platform/db/models/domain.py](../../brain/platform/db/models/domain.py)
  — `domain_records` (171–215; `data` JSONB, `version`, `updated_at`),
  `domain_events` append-only before/after/patch log (258–305). Writer:
  `AsyncDomainService` ([brain/systems/user_domains/service.py](../../brain/systems/user_domains/service.py):903–919).
- **Change feed (already queryable):** `query_workspace_data(sources=['domain_events'], start_at=…)`
  → reader `_query_domain_events`
  ([brain/systems/runs/tool_catalog/handlers/workspace_data.py](../../brain/systems/runs/tool_catalog/handlers/workspace_data.py):944–998),
  tool def [brain/systems/runs/tool_definitions.py](../../brain/systems/runs/tool_definitions.py):549,570–574.
- **Cycles (repurpose, don't rebuild):**
  [brain/platform/db/models/cycle.py](../../brain/platform/db/models/cycle.py)
  (`schedule_expr`, `next_run_at`, `last_run_at`), scheduler polls every 30s
  ([brain/systems/cycles/scheduler.py](../../brain/systems/cycles/scheduler.py):16–26),
  output goes to a ledger + thread, **not Slack**
  ([brain/systems/cycles/output_targets.py](../../brain/systems/cycles/output_targets.py):35–61).
- **Slack outbound (one owner):** only `post_slack_reply`
  ([brain/systems/runs/tool_catalog/handlers/slack.py](../../brain/systems/runs/tool_catalog/handlers/slack.py):233–311)
  → `client.post_message` → `chat.postMessage`
  ([brain/systems/slack/client.py](../../brain/systems/slack/client.py):88–101).
  Event-driven reply-in-thread only; requires an explicit `channel_id` outside a
  Slack-triggered run (handlers/slack.py:280). **Nothing posts proactively today.**
- **Engineering-only task typing (fix these):**
  [brain/app/hooks/self_assess.py](../../brain/app/hooks/self_assess.py)
  (`_TASK_PATTERNS` 21–32 greedily buckets `add/create/update/build/write` as
  `code`; `_CHECKLISTS` 40–61) and
  [brain/app/cli/run.py](../../brain/app/cli/run.py) (`CLASSIFIERS` 51–68,
  `TEMPLATES` 75–153, TDD-shaped). These are a *work-mode/execution* axis, a
  different axis from domain — make them domain-aware, don't merge.
- **Assignment surfaces (all soft):** connection `owner_user_id` stamped on the
  event/idea/actor (service.py:189,882,917–936); **silent skip if unset**
  (service.py:867,1096); SOUL `## Coordination` prose
  ([brain/systems/personality/soul.py](../../brain/systems/personality/soul.py):57–66);
  per-source-policy `instructions` injected into the triage message
  (service.py:989–992), edited via `manage_inbound`
  (tool_definitions.py:1004–1005,1072–1074). Assignment mechanism = `manage_idea`
  with `user_id` (tool_definitions.py:1172–1174,1259). **No task-type→person map
  exists.** "Doc 1155" is this runtime prose, not a repo file.
- **Migrations:** Alembic (`alembic.ini` at repo root) — the new `task_type` field
  and freshness columns need migrations.

## Single-owner invariants (refactor-clean — must hold at end state)

The plan must leave the codebase looking designed-today, not bolted-on:

1. **One GitHub→triage write path.** Exactly one write path from GitHub into the
   triage source-of-truth: the domain projection via `submit_inbound_envelope`.
   The reconciliation poll (Slice 1) reuses it — it must **not** be a second
   writer. The existing `cortex_emerge` → `ideas` poll
   ([brain/jobs/pipelines/cortex_emerge.py](../../brain/jobs/pipelines/cortex_emerge.py))
   is a _separate concern_ (curiosity/ideas) and must be explicitly scoped out of
   triage, or retired — never left as a shadow triage source.
2. **One domain-axis owner.** A single `task_domain` classifier module
   (`brain/systems/task_domain.py`) owns the domain axis; every subsystem
   classifies through it. The pre-existing *work-mode/execution* axes
   (`self_assess.py`, `run.py`) stay — they're a different axis (HOW, not WHAT) —
   but become **domain-aware**, overriding the engineering bar only on positive
   non-engineering evidence. No second domain vocabulary.
3. **One owner-resolution function.** A single "resolve owner for this item" seam
   keyed on task_type/repo/policy. Deterministic rules are the owner of
   high-stakes routing; connection-authority and prose are ordered fallbacks
   behind it — not parallel deciders.
4. **One freshness contract.** `source_updated_at` / `last_synced_at` /
   `sync_source` defined once on the projected record and read once by the read
   surface (`domain.inspect`). Not scattered.
5. **One outbound-Slack owner.** All proactive posts (notify-loop + urgent
   bypass) go through `post_slack_reply` / `client.post_message`. No parallel
   Slack client.

### Transitional scaffolding (named, with removal conditions)
- Slice 0's **policy-`instruction` route** (business/PM → Reda) is superseded by
  Slice 3's typed rule. **Remove/migrate** the soft instruction the moment Slice
  3 lands.
- Slice 0's **cycle-frequency bump** is superseded by Slice 1 webhooks. **Revert**
  the frequency to a low-freq reconciliation cadence once webhooks deliver.

## Slice graph

```
Slice 0  Stopgaps (no infra dep) ─────────────┐  ship today
Slice 2  Typed task-type (no infra dep) ──┐   │
Slice 1  Webhook freshness (needs App) ──┐│   │
Slice 3  Deterministic assignment ───────┘│   │  (depends on Slice 2)
Slice 4  Proactive Slack notify ──────────┘   │  (depends on Slice 1)
Slice 5  Deploy-state & verification ─────┘   │  (composes with 1 + 4)
                                              └─ Slice 0 routes/bump retired by Slices 3/1
```
Slices 1 and 2 are largely parallel. 3 depends on 2. 4 depends on 1.
Slice 5's sweep rides Slice 1's webhook lane and its verification tick rides
Slice 4's cycle; its pure cores and prose are independent of both.

## Review map (where the human looks per slice)
- **Slice 0:** a business/PM issue routes to Reda; freshness window observably < 1h.
- **Slice 1:** open/close/comment a test issue → domain reflects within seconds;
  kill the webhook → reconcile backfills. `synced Xm ago` shows on read.
- **Slice 2:** "create a launch plan" types as `business` and gets a business
  checklist, not TDD; an eng task still types `engineering`.
- **Slice 3:** business/PM issue → `owner=Reda` deterministically; ambiguous eng
  issue → judgment path; author-nudge norm honored; nothing silently skipped.
- **Slice 4:** a domain change → appropriate Slack digest within one tick;
  urgent → immediate ping.

## Assumptions (live-only — do NOT investigate; correct if wrong)
- **A1** The current twice-daily cycle is a data-sync mission we retire/repurpose.
  _If it does more, adjust Slice 0/4 — no re-slice._
- **A2** Teammates should read the **domain** as source of truth. If they
  currently read the `ideas` store (cortex_emerge — note its silent
  open/7-day/limit-20/single-owner cap), Slice 1 includes pointing the read path
  at the domain.
- **A3** "Doc 1155" is runtime policy-instruction/SOUL prose. Slice 3 reads and
  preserves its intent at build time before migrating deterministic parts to
  typed rules.

## Decisions (confirmed with Reda 2026-07-08)
1. **task_type set:** `engineering | product | business | ops | other`.
   Marketing/GTM folds into `business` for now; split later if it earns its own
   checklist.
2. **task_type derivation:** a repo/policy prior wins when set (a repo can
   declare its default type; a policy can pin it); the model infers only when
   unset. Precedence: **policy/repo > model inference**.
3. **Notify target:** post to the **existing team Slack channel Illo already
   works in** (the monitored channel) — not a new channel, not DMs. Digest each
   cycle tick (default 30 min); **urgent = immediate** in that same channel.
4. **No-owner → an unclaimed pool folks pull from (a "fourth list"), not
   auto-assignment.** Owner resolves in order: **(1) typed rule**
   (business/product → Reda; repo→owner) → **(2) connection authority** →
   **(3) parked ownerless in a visible unclaimed pool** teammates pick up
   organically (claim = self-assign). No auto-push (no backstop, no
   load-balancer); silent-skip removed — nothing is dropped. The Slice 4 digest
   surfaces unclaimed items so pickup happens.

## Known unknowns (safe to discover during a slice)
- Exact GitHub event → domain object-type field mapping (Slice 1 defines it).
- Whether `task_type` lives on the domain record, the Idea, or both (Slice 2
  picks the seam; default: on the triaged Idea + projected onto the record).
- Reconciliation cadence tuning (Slice 1 starts conservative).

## Out of scope
- Migrating `cortex_emerge`/ideas beyond scoping it out of triage.
- PR-review workflow changes beyond encoding the author-nudge norm.
- Multi-org / multi-tenant assignment rules (single-org assumption holds).
