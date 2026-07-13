# Slice 05 — Triage-moment minting

## Contract unlocked
The first routing moment goes warm: when an inbound item finishes triage
with a resolved owner, Illo mints a packet, replies in the origin Slack
thread with the human brief + launch link, and stamps the packet onto the
idea. This is the feature's first end-to-end value. **No runtime gate**
(Reda, 2026-07-13): once this slice merges and deploys, every actionable
triaged item gets a packet — verification happens pre-merge (below).

## API seam
`brain/systems/briefing/mint.py` — the single orchestration owner:

```python
async def mint_packet_for_job(session, *, org_id, idea, record_ref,
                              owner_user_id, ask, acceptance_criteria,
                              readers, budget) -> MintResult
# gather_pieces → assemble_dossier → compose_packet → create_launch_handoff
# → stamp idea → return {handoff, created: bool, human_brief, launch_url}
```

**Hook point (pinned — do not hook queue time):** the run-completion
reconcile path — `brain/systems/runs/store.py` terminal transition →
`reconcile_inbound_triage_run` (`brain/systems/inbound/reconciliation.py`).
Assignment is persisted much earlier, at queue time
(`inbound/service.py` `_queue_illo_triage`), so at reconcile time read the
owner from `idea.agent_details["assignment"]`, and discover the job's
record via the run's attribution refs (`inbound/attribution.py`
`record_id` → domain record). Items that produced no record use the idea id
as `job_ref` (slice 01 already allows this).

**Ask source (v1, deterministic):** there is NO structured triage-run
output contract today (completion yields prose `final_answer` +
attribution) — do not invent one in this slice and do not parse prose. The
`ask` is a fixed template over what already exists: `task_domain` (queue
heuristic) + the normalized inbound summary. `acceptance_criteria` v1 =
empty unless the template can state one mechanically ("reply lands in
origin thread"). A structured triage-output artifact is a separate,
explicitly-named later task if template asks prove too thin — that
machinery neighbors the issue-249 answer-overwrite territory and deserves
its own tests.

**Mint semantics:**
- `created` flag: `create_launch_handoff` currently returns the existing
  row on an idempotency hit with NO signal (`launch_handoffs.py:171-180`).
  Extend the service to return/expose `(row, created)` — **a reused row
  posts NOTHING to Slack** (re-triage of unchanged truth must be silent).
- Drift guard: on a reused row, compare stored
  instructions/target/owner-metadata against the fresh compose; on drift
  (shouldn't happen if slice-02 hashing is right — this is the belt to its
  suspenders), supersede instead of reusing.
- Supersede = existing vocabulary only: old row → status `archived` +
  `metadata_["superseded_by"]`; new row carries
  `metadata_["supersedes"]`. The status CHECK constraint allows only
  `open/launched/claimed/expired/archived` — no new status, no migration.
- Race safety: mint runs inside `session.begin_nested()` (precedent:
  `change_notifications_cycle.py`); catch IntegrityError from the
  `(org_id, idempotency_key)` unique on flush → re-select by key and treat
  as reused. Serialize supersede per job (row-lock the idea) so a
  triage-mint racing a notify-refresh (slice 06) cannot double-supersede.
- Stamp — Illo-owned state ONLY: `idea.agent_details["packet"] =
  {handoff_id, revision, owner_user_id, minted_at}`. Never inside
  projection-owned record `data` (re-projection rewrites it and would
  silently orphan slice 06's lookup). Mint also persists `owner_user_id`
  into handoff `metadata_` (the model has no assignee column; slice 07's
  per-member split reads this).
- Slack reply — backend path: `slack/client.py`
  `post_message(channel, thread_ts=…)` using the idea's stored origin
  provenance. The `post_slack_reply` TOOL is unusable here (it resolves its
  target from in-run trigger context); it stays the path for the in-run
  "brief me" flow only.
- Target per owner from `ILLO_MEMBER_AGENT_TARGETS` (slice 04, uuid-keyed).
- Unclaimed-pool items get a packet too (owner label "unclaimed") — the
  brief is what makes claiming cheap.

**No gating (superseded 2026-07-13 — see README invariant):** packets fire
for every actionable triaged item, all task domains, as soon as this
merges+deploys. There is no allowlist and no dry/live mode env. What
replaces the old dry mode is a **pre-merge probe**: a read-only CLI
(`python -m brain.systems.briefing --probe-triage --since …`, dev checkout
+ read env against illo-dev) that walks recent real triaged ideas through
gather→assemble→compose WITHOUT creating handoffs or posting, and prints
the briefs. Paste 3+ samples into `assets/pre-merge-probe-05.md`; Reda
eyeballs them on the PR before merge. The only env var is
`ILLO_MEMBER_AGENT_TARGETS` (config-with-default: unset → codex).

## Doc-1155 delta (applied at merge+deploy)
Add to the triage playbook: when a packet is minted, the reply IS the
packet brief — do not also write a freeform summary; on-demand
"brief me on X" requests route through the same mint path (tool call),
never hand-assembled context. Keep the delta text in this slice file when
written; apply to live doc 1155 following the pointer-section discipline
(never grow the core).

## What the human can run/see
The pre-merge probe: recent real triaged items rendered as briefs with
zero side effects → paste 3+ samples into `assets/pre-merge-probe-05.md`
on the PR for Reda's review.
**Non-blocking checkpoint:** give ~5 min for a reaction, then decide on the
evidence and record the decision here.

## Verification
- Integration test with fakes: reconcile completion → one handoff row, one
  threaded Slack reply (fake sender), stamp on idea; idempotent re-run →
  `created=False`, NO second Slack post, no duplicate row.
- Race test: two concurrent mints for one job → one row, loser re-selects,
  session survives (nested transaction, not poisoned).
- Supersede test: changed ask → old row archived + `superseded_by`, new row
  posted once.
- Failure containment: mint failure never fails the triage run (evidence
  line instead) — triage worked before packets existed and must keep
  working without them.
- Record-less item test: idea-id job_ref path mints fine.
- Pre-merge: probe samples approved on the PR; supersede path exercised
  once against illo-dev read data.

## Stays green
Full fast suite; triage tests updated to expect the mint hook (it is now
unconditional — containment tests prove triage survives mint failure).

## Feedback that would change this slice
Reda wants packets on every triaged item (no allowlist), wants the digest
(slice 06) to be the first surfaced moment instead of thread replies, or
template asks prove too thin → schedule the structured-triage-output task.
