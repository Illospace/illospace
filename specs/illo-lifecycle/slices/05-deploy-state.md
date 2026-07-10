# Slice 5 — Deploy-state & post-deploy verification

**Composes with Slice 1 (merge events arrive via the GitHub webhook lane) and
Slice 4 (the notify cycle carries the verification tick; posts go through the
one Slack owner).** Both are code-complete awaiting live wiring; this slice's
pure cores, prose, and tests are buildable and verifiable now, and its runtime
pieces are inert behind the same activation gates.

## The problem observed live (2026-07-10, Rollbar #2206 → #904)

Uwear merges fixes to `staging` but promotes to prod roughly weekly (evergreen
staging→main promotion PR, e.g. uwear-backend #859) unless urgent. Prod Rollbar
alerts therefore **re-fire for already-fixed bugs**, and the lifecycle has no
representation of this. Intake works (alert → monitored channel → headless
triage → real issue #904 / tracker record 1238, PR #905 opened as a main
hotfix), but dedup is **binary**: ticket exists → skip. So a re-firing alert
for a staging-fixed bug is either silently skipped (no annotation, no
occurrence awareness) or — if the ticket closed at merge — refiled as a
duplicate (the "done-reappeared" fumble class). Nothing verifies an error
actually **stopped** after the promotion deploy; tracker `status` has nothing
between "merged" and `Done`. Record 1238 shows the data gap: Rollbar #2206 and
fix PR #905 exist only as prose in `body`/`progress_note` — nothing structured
for a dedup ladder to key on.

The case study then advanced to exactly the state this slice models, while the
spec was being written: Axel confirmed #2206 as **deployment drift** — the
real fix is PR #860, merged to *staging* 2026-07-07 (merge `d9980489`,
compare `main...d9980489` = diverged ⇒ not in prod) — closed hotfix #905
unmerged as unnecessary, and closed issue #904 as "known". Under this slice
that ticket is `deploy_state=prod_pending` with `fix_pr=uwear-ai/uwear-backend#860`
and **cannot be `Done`** (not deployed, not verified); today, closed-with-
live-alert is precisely the armed done-reappeared trap: the next #2206
re-fire meets a closed ticket and binary dedup would refile it.

## Contract unlocked

A re-firing alert is never blindly skipped and never refiled. Triage branches
on the fix's **deploy-state**: not-merged → occurrence note; merged-awaiting-
promotion → expected noise, annotated, owner NOT re-pinged; deployed-and-still-
firing → the fix didn't work, ticket **reopened** and the builder escalated.
`Done` for alert-linked tickets means **deployed to prod AND verified quiet in
Rollbar**, not merely merged — with evidence ("verified quiet since deploy at
T"). When a waiting fix keeps accumulating occurrences, Illo recommends early
promotion in the team channel (recommend only — never merges).

## Deploy-state model (one axis, one owner)

`deploy_state` tracks the ticket's **latest fix attempt**, not the ticket
itself; ticket `status` is untouched as an axis. Values (absent = no fix
merged):

- `staging` — fix PR merged to the repo's staging branch; promotion state not
  yet confirmed. Set on the fix-PR merge event (base = staging) or at
  triage-time backfill.
- `prod_pending` — ancestry-confirmed on staging and **not** an ancestor of
  main; awaiting the weekly promotion. (`staging` upgrades to `prod_pending`
  on any sweep/triage touch. The ladder treats both identically — the split is
  bookkeeping precision, not behavior.)
- `deployed` — the fix's merge commit is an ancestor of main (promotion merged
  **or** direct-to-main hotfix like #905 — both paths, one check).
  `deployed_at` stamps the merge time; the verification window runs.
- `verified` — the Rollbar item stayed quiet from `deployed_at` (+ settle)
  through the quiet window; `verified_at` stamped; ticket may close `Done`.

On a post-deploy re-fire past the settle window the ladder **reopens**: status
back to `Todo`, `deploy_state` cleared (the fix is no longer believed), the
failed attempt recorded in `progress_note` prose. A new fix PR restarts the
ladder.

The mechanical "is this fix in prod" check is commit ancestry — GitHub compare
`main...SHA` (status `identical`/`behind` ⇒ ancestor) — NOT "was the promotion
PR merged": a fix merged to staging after the promotion PR's cutoff is not in
that promotion. Per-ticket ancestry at sweep time is correctness, not
optimization. Indeterminate reads (API down, no token) leave state unchanged —
degrade open, never fail closed (the PR #287 convention).

## API seam / changes

1. **Pure core (one owner of the axis)** — new `brain/systems/deploy_state.py`,
   patterned on [change_notifications.py](../../../brain/systems/change_notifications.py)
   (explicit, testable logic — not prose):
   - `DeployState` str-enum: `staging | prod_pending | deployed | verified`.
   - `parse_rollbar_alert(text) -> AlertSignature | None` — from a Rollbar
     Slack attachment/fallback text (live shape:
     `<https://app.rollbar.com/a/uwear/fix/item/Uwear-API/2206|#2206 100th
     error: ClientError: 400 INVALID_ARGUMENT…>`), extract project + item
     number (the signature, e.g. `Uwear-API#2206`), error title, and the
     **occurrence milestone** ("100th error" → 100) that drives rate-spike
     escalation. Note: Rollbar posts have empty `text` — content rides
     attachments; parse whatever text the caller fetched.
   - `classify_refire(*, deploy_state, ticket_status, deployed_at, now,
     settle) -> LadderAction` — the ladder, pure:
     - no merged fix → `NOTE_OCCURRENCE` (append freshness/occurrence note;
       milestone alert also raises `priority`);
     - `staging`/`prod_pending` → `EXPECTED_NOISE` (annotate the ticket,
       optional one-time thread reply "known — fixed by PR #X merged to
       staging, ships with next weekly promotion"; **never refile, never
       re-ping the owner** — and a merely-staged fix wins over a stale `Done`:
       the prematurely closed ticket is quietly normalized, never escalated;
       a milestone on a `prod_pending` fix additionally yields
       `RECOMMEND_PROMOTION`, `staging` having upgraded to `prod_pending` on
       the triage touch);
     - `deployed` within settle → `EXPECTED_NOISE` (deploy draining);
     - `deployed` past settle, or `verified`, or ticket `Done` with no
       merely-staged fix → `REOPEN_ESCALATE` (this is why blind
       dedup-suppression is unacceptable, and it retires the done-reappeared
       refile bug).
   - `classify_merge_event(hints) -> MergeKind` — `promotion` (base main, head
     staging), `hotfix` (base main, other head), `fix_to_staging`, `other`.
2. **Record fields (runtime rows, no migration)** — the domain writer rejects
   unknown `data` keys ([service.py](../../../brain/systems/user_domains/service.py):57–66),
   so the new keys are `DomainFieldDefinition` rows added via the existing
   `add_field_definition` (service.py:364) by an idempotent
   `ensure_deploy_state_fields(...)` keyed on object key `github_ticket` (the
   Domain-1 id is runtime data, not a constant). Flat, all optional:
   `rollbar_item` (text, `Uwear-API#2206`), `alert_last_seen_at` (datetime),
   `alert_occurrences` (number, highest milestone seen), `fix_pr` (text,
   repo-qualified `uwear-ai/uwear-backend#905` — cross-repo fixes exist),
   `fix_merge_sha` (text), `fix_merged_at` (datetime), `deploy_state` (enum),
   `deployed_at`, `verified_at`, `promotion_recommended_at` (datetime).
   History stays in `progress_note` prose; fields hold the latest attempt.
   Called lazily by the sweep/tool (both gated), so activation stays
   deliberate.
3. **Merge hints on the webhook envelope** — extend `github_event_to_envelope`
   ([github_webhook.py](../../../brain/systems/inbound/github_webhook.py):63):
   for `pull_request` closed events also stamp `hints.merged` (the bool it
   drops today), `base_ref`, `head_ref`, `merge_commit_sha`, `merged_at`.
   Additive; existing envelope fields untouched.
4. **Promotion-event sweep (deterministic, no LLM)** — new
   `run_deploy_sweep(session, *, org_id, repo, merge_event)` invoked from the
   inbound path right after projection when an envelope carries
   merged-to-main hints for a watched repo (`ILLO_DEPLOY_SWEEP_REPOS`,
   comma-separated; unset = off, the standing inert-until-wired convention).
   For each Domain-1 record **whose `fix_pr` lives in that repo** (fix-repo
   identity, not the ticket's own `repo` field — an app ticket fixed by a
   backend PR is swept by the backend promotion) with `deploy_state` in
   {`staging`,`prod_pending`}: ancestry-check its `fix_merge_sha` against
   main; flip to `deployed` + `deployed_at` when confirmed. Writes go through
   `AsyncDomainService` (the one domain writer) with `expected_version`; the
   resulting `domain_events` feed the Slice-4 digest for free ("shipped,
   verifying: …"). Also backfills `staging → prod_pending` confirmations.
5. **Verification pass (timer side, close-half only)** — a
   `run_deploy_verification(session, org_id, now)` step on the Slice-4 notify
   tick ([change_notifications_cycle](../../../brain/systems/change_notifications_cycle.py)):
   records with `deploy_state=deployed` and `deployed_at + settle + quiet
   window` elapsed and no `alert_last_seen_at` past settle → flip `verified`,
   close `Done` with evidence ("verified quiet since deploy at T"). The
   **reopen half is event-driven** (a re-fire flows through triage → ladder →
   `REOPEN_ESCALATE` immediately) — the timer only ever closes, so an
   unregistered cycle degrades safe: tickets merely stay `deployed`. Quiet is
   derived from our own record data (every re-fire passes the monitored
   channel → ladder → `alert_last_seen_at`), so **no Rollbar API is
   required** for the default path. Defaults `ILLO_DEPLOY_SETTLE_MINUTES=30`,
   `ILLO_DEPLOY_QUIET_HOURS=24`.
6. **Agent tool: `check_fix_deploy_state`** — read-only sibling of
   `create_github_issue` (same registration pattern:
   [definitions/github.py](../../../brain/systems/runs/tool_catalog/definitions/github.py),
   handler, [registry.py](../../../brain/systems/runs/tool_catalog/registry.py)
   toolsets; risk: read). Input repo + PR number (or SHA); output `{merged,
   base_ref, in_staging, in_main, deploy_state, recommended_action}` —
   computed by the **same pure core** the sweep uses, so prose and code cannot
   drift. The ancestry helper reuses the project-context GitHub client
   (token-aware, degrade-open on `None`).
7. **Prose ladder (the dedup rule's new text)** — rewrite the binary
   "One problem = one issue" rule in
   [uwear-engineering-triage/SKILL.md](../../../brain/systems/skills/builtin_skill_bundles/uwear-engineering-triage/SKILL.md)
   into the deploy-state ladder (match by `rollbar_item`/signature first, then
   fuzzy; branch per `classify_refire`; stamp the structured fields when
   filing/linking), redefine `Done` for alert-linked tickets (deployed AND
   verified quiet), and add the recommend-early-promotion norm (**recommend
   only; never merge the promotion PR**). Mirror the same ladder into the
   monitored-channel prompt
   ([triggers.py](../../../brain/systems/slack/triggers.py):235 —
   `slack_channel_monitor_message`) as one branch line. The live doc (Domain
   37 record 1155, currently v5) gets the same delta as an activation step —
   coordinate with the parallel digest-contract edit to the same doc.
8. **Urgent-promotion recommendation** — when the ladder yields
   `RECOMMEND_PROMOTION` (milestone re-fire on a `prod_pending` fix), the
   triage run posts to the team channel via `post_slack_reply` (invariant 5):
   "fix for #904 (PR #905) is merged and waiting; #2206 hit its Nth occurrence
   today; recommend promoting now." Deduped by `promotion_recommended_at`
   (max one per ticket per day). Matches the weekly-unless-urgent policy;
   merging remains a human action.
9. **Optional enabler (decision for Reda — do not block):** a Rollbar
   **read-only** API token in Vault (the runtime-authored
   `PROD_POSTGRES_READONLY_URL` / skill-46 pattern) would let the
   verification pass query item occurrence counts deterministically instead
   of inferring quiet from Slack recurrence (catches sub-threshold fires that
   never reach the channel). The quiet-check is a seam with the internal
   inference as default impl; the API impl slots in when the token exists.
10. **Alert text at ingest (small)** — surface Slack attachment
    fallback/preview text into the monitor envelope/message
    ([ingress.py](../../../brain/systems/slack/ingress.py) /
    triggers.py): run 1073's "Message text:" was **empty** because Rollbar
    posts attachment-only; the signature should be visible without a fetch
    (the agent still reads the thread for full context).

## What the human can run/see

- Unit probe: `classify_refire(deploy_state=prod_pending, …)` →
  `EXPECTED_NOISE`; same ticket after promotion + settle →
  `REOPEN_ESCALATE`; `parse_rollbar_alert` on the recorded #2206 fallback →
  `Uwear-API#2206`, milestone 100.
- Feed a recorded promotion-merge webhook delivery locally → a
  `prod_pending` fixture record flips to `deployed` with `deployed_at`, and a
  `domain_event` shows the flip.
- Dry-run pattern (run 952 style, READ-ONLY, no posts): synthetic re-fire of
  Rollbar #2206 with the fix framed merged-to-staging → the triage run
  classifies "expected — awaiting promotion", files **zero** new issues,
  pings **zero** owners. Synthetic post-promotion re-fire (live truth: #905
  IS in main) → reports it would reopen #904 and escalate to the builder.
- `check_fix_deploy_state(repo=uwear-ai/uwear-backend, pr=905)` →
  `in_main=true, deploy_state=deployed` (live read).

## Verification

- Unit: ladder truth table (every `deploy_state` × settle/milestone/status
  combination → action); parser fixtures from the real #2206 attachment incl.
  new-item vs Nth-error forms and non-Rollbar text → `None`;
  `classify_merge_event` promotion vs hotfix vs staging-fix; sweep flips only
  ancestry-confirmed records (fix-after-promotion-cutoff stays
  `prod_pending`); verification closes only quiet-through-window records;
  recommendation dedup (second milestone same day → no second post).
- Integration (test-DB, `submit_inbound_envelope` pattern of
  [test_inbound_webhooks.py](../../../tests/test_inbound_webhooks.py)): a
  merged-to-main `pull_request` envelope with `ILLO_DEPLOY_SWEEP_REPOS` set
  triggers exactly one sweep; idempotent on delivery replay; unset env → no
  sweep (inert).
- Ensure-fields: idempotent (second call adds nothing); unknown-key rejection
  gone once fields exist.
- The two dry-run scenarios above on illo-dev, non-posting, after the prose
  ships.

## Must stay green

- Existing webhook envelope mapping and idempotency
  ([test_github_webhook.py](../../../tests/test_github_webhook.py)) — hints
  are additive.
- Ordinary (non-alert-linked) ticket flow: no new required fields, `Done`
  semantics unchanged, triage prompt still fits its current decision shape.
- Slack monitor ack/headless behavior
  ([test_slack_channel_monitor.py](../../../tests/test_slack_channel_monitor.py)).
- One domain writer, one Slack owner, one GitHub→triage write path (README
  invariants) — the sweep writes via `AsyncDomainService` and posts nothing
  itself.

## Feedback that would change this slice

- Grace defaults (settle 30m / quiet 24h) and whether quiet should also
  require a minimum number of prod requests (traffic-aware verification needs
  the Rollbar API enabler).
- Whether `staging` vs `prod_pending` earns its keep or collapses to one
  value.
- Recommendation cadence (once/day/ticket) and whether it should escalate to
  a DM after N ignored recommendations.
- The Rollbar read-token decision (item 9) — with it, verification can also
  run occurrence-count deltas instead of pure quiet/not-quiet.
- Revert detection: ancestry can't see reverts (a reverted fix still passes
  the compare check). Today the prose handles it (clear `deploy_state` when a
  revert is linked); mechanical revert detection would need commit-message or
  linked-PR heuristics.
