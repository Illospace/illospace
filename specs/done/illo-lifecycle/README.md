# Illo lifecycle overhaul — freshness, task-type, assignment, deploy state

*Closed 2026-08-19. Slices 00–04 shipped 2026-07-09 (PR #276), slice 05
followed 2026-07-10 (PR #293), and everything was live-wired through July;
slice 05's stored deploy-state machine was later **replaced wholesale** by
read-time derivation (#473, commit `af8344a9`, 2026-07-28). This record covers what survives, why it has the shape it has,
and what the plan predicted wrong.*

## What it is

Four owner modules that make Illo's triage lifecycle deterministic where
determinism is cheap, and honest where it is not:

- **Webhook ingress** (the planned "freshness contract" on top of it never
  landed — see below) — `brain/systems/inbound/github_webhook.py` (pure
  envelope core: `verify_signature`, `github_event_to_envelope`) behind
  `POST /webhooks/github` (`brain/app/api/routers/github_webhooks.py`,
  mounted in `main.py`; self-gates with 503 until
  `ILLO_GITHUB_WEBHOOK_SECRET` + `ILLO_GITHUB_CONNECTION_ID` are set). One
  GitHub→triage write path: the router submits the same
  `submit_inbound_envelope` every other lane uses.
- **Task domain** — `brain/systems/task_domain.py` (`TaskDomain`,
  `classify_task_domain`); consumed by `self_assess.py` checklists,
  `cli/run.py`, and triage. One domain-axis owner; persisted as JSONB in
  `ideas.agent_details`, no migration.
- **Deterministic assignment** — `brain/systems/inbound/assignment.py`
  (`resolve_owner`, `OwnerDecision` with basis provenance) wired into
  `_queue_illo_triage` (`inbound/service.py`), parking unresolvable items on
  the unclaimed-pool sentinel.
- **Deploy state, derived on read** — `brain/systems/deploy_state.py`
  (`derive_deploy_state(s)`, `unmerged|staging|deployed`),
  `deploy_state_github.py` (`observe_ancestry`: GitHub compare
  `identical|behind` ⇒ contained), `deploy_record_contract.py`
  (the stored deploy facts are exactly fix_pr, fix_merge_sha, verified,
  verified_at; `RETIRED_DEPLOY_FIELDS` hidden at every read boundary). Two consumers: the
  always-on `check_fix_deploy_state` tool and the staging-only closure sweep
  (`staging_only_closure.py`, scheduled via `tracker_maintenance.py`).

The prose half of the lifecycle (Reda/Axel/JB routing, builder-first
ownership, the deploy-state read ladder, digest contract) lives in the
`uwear-engineering-triage` skill —
`brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/SKILL.md`
— applied to the live doc by `brain/app/cli/activate_uwear_engineering_triage.py`.

## The reason

- **Rules decide only what is safely mechanical.** The engineering
  three-way routing stays *prose*, deliberately: `default_rules()` registers
  BUSINESS/PRODUCT domain routes plus an env-driven repo→owner table
  (`ILLO_REPO_OWNERS`, empty by default in compose) — no engineering
  *keyword* route exists in code, and
  `tests/test_assignment.py::test_engineering_is_not_auto_routed` pins the
  keyword side (it does not cover repo rules; those route whatever an
  operator explicitly configures). A keyword heuristic must never yank an
  engineering ticket between Reda, Axel and JB; judgment stays in the runtime
  playbook. The text heuristic
  drives ownership only on the GitHub lane (`repo` present) so a stray word
  in a Slack message cannot re-route an item.
- **Deploy state is derivable, so it is derived.** The original slice 05
  stored a state machine updated by webhook sweeps. It failed live: a real
  staging→main promotion (uwear-backend#1289) produced zero transitions, and
  14 of 20 active records could never transition (no `fix_merge_sha`). A
  stored deploy state is a cache invalidated by webhooks; every failure is
  that cache going stale. `#473` deleted the machine (`deploy_state_sweep.py`,
  the webhook trigger, the `ILLO_DEPLOY_SWEEP_REPOS` env gate, the stored
  enum) and replaced it with ancestry at read time. There is **no gate on the
  read path** (`test_check_fix_deploy_state_is_always_on`); regression guards
  assert merged-PR webhooks have **no** deploy side effects
  (`tests/test_inbound_webhooks.py`).
- **Never guess healthy.** An indeterminate GitHub read renders exactly
  `"unknown"`, per-ref failures are isolated, and identical commits count as
  contained so a no-diff promotion resolves. Records persist only
  non-derivable facts; promotion of staging→main is a human action, never
  Illo's.

## Invariants (enforced, with the enforcement point)

1. Engineering is never keyword-auto-routed (`assignment.py` + test above;
   env-configured repo rules are the one sanctioned exception); explicit
   GitHub assignee always wins; builder-first ownership; business/product/
   website = Reda exclusively; never assign a PR reviewer (SKILL.md +
   `soul.py` Coordination block).
2. Deploy evidence fields are the only stored deploy facts
   (`deploy_record_contract.py`); the four retired fields are stripped at
   every read boundary (`user_domains/service.py`,
   `workspace_data.py`).
3. One GitHub **webhook**→triage write path through `submit_inbound_envelope`
   (`tests/test_inbound_webhooks.py::test_webhook_and_mcp_submit_share_inbound_event_path`)
   — with the `cortex_emerge` exception noted below.
4. Unroutable items park on the unclaimed pool, never silently dropped —
   **caveat:** with `ILLO_UNCLAIMED_POOL_USER_ID` unset the pre-existing
   silent skip is preserved by design, and it is unset in the live compose
   env, so the silent skip is still the live behavior today.

## What the plan predicted wrong (recorded so nobody re-derives it)

- **Slice 05 was rebuilt** (above) — the two `illo-dev` validation runs
  (1088/1089) validated a ladder whose implementation no longer exists; they
  are not re-runnable evidence for today's code.
- **The notify cycle never registered.** `run_notify_cycle`
  (`change_notifications_cycle.py`) has zero production callers; the digest
  responsibility moved into the coordinator cycle's prose contract, and the
  sweeps it wrapped run via `tracker_maintenance.py` instead. The pure core
  (`change_notifications.py`) is live-dead. Ticketed at close as
  [#843](https://github.com/Illospace/illospace/issues/843).
- **The freshness contract (invariant 4 of the plan) was never built** — no
  `last_synced_at`, and no freshness-stamp `sync_source` on inbound events or
  ideas (the PR tracker's own `sync_source` data key is unrelated
  provenance); `source_updated_at` survives only as an envelope hint. No
  GitHub delta-poll backstop exists either (`inbound/reconciliation.py` is a
  different thing — it reconciles run receipts, not GitHub deltas).
- **The "not auto-registered" claim inverted** — the webhook router *is*
  mounted unconditionally and self-gates with 503 instead.
- **`cortex_emerge` can still ingest GitHub issues into ideas on demand**
  (`POST /cortex/emerge`; unscheduled, double-gated on
  `ILLO_GITHUB_ISSUE_OWNER` plus a working `gh` CLI) — a second write path,
  so the "no shadow triage source" invariant was never fully resolved.
- **Doc-1155 mechanics superseded.** The hand-applied anchored-edit runbook
  (the old `05-doc-1155-delta` slice) was replaced by the slug-resolved
  activation CLI (`docs/329-live-delta.md`); the live doc moved v6 → v7 → v8,
  and cycle 2's mission is now the chantier-primary digest contract, not the
  original sync mission. Pinned record ids from that era are dead (migration
  `0028_deactivate_pinned_chantier_digest`).
- **Later layers built on slice 05** that this spec never anticipated: the
  staging-only closure production gate (#554), the promotion-PR job
  (`brain/jobs/pipelines/staging_promotion_pr.py`, the #475 idea), and
  promotion-readiness surfacing in cycles.

## Tests

`tests/test_github_webhook.py`, `test_notify_routing.py`,
`test_task_domain.py`, `test_self_assess.py`, `test_assignment.py`,
`test_change_notifications.py`, `test_deploy_state.py`,
`test_deploy_state_batch.py`, `test_check_fix_deploy_state_tool.py`,
`test_staging_only_closure.py`, `test_alert_resolution.py`, plus the
webhook no-side-effect guards in `test_inbound_webhooks.py`.
