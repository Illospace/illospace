# Slice 05 — Triage-moment minting (env-gated)

## Contract unlocked
The first routing moment goes warm: when triage resolves an owner for an
actionable item, Illo mints a packet and replies in the origin Slack thread
with the human brief + launch link, and attaches the handoff reference to
the record. This is the feature's first end-to-end value.

## API seam
`brain/systems/briefing/mint.py` — the single orchestration owner:

```python
async def mint_packet_for_record(session, *, org_id, record_ref, owner,
                                 ask, acceptance_criteria,
                                 readers, budget) -> MintResult
# gather_pieces → assemble_dossier → compose_packet → create_launch_handoff
# → attach reference to the record → return {handoff, human_brief, launch_url}
```

Hook point: triage completion, immediately after
`inbound/assignment.py` resolution is persisted — same place the lifecycle
slices instrument. The Slack reply goes through the existing
`post_slack_reply` path (`work_intake_slack.py` thread_ts plumbing), i.e.
into the origin thread, threaded, not a new channel post.

Gating & policy:
- `ILLO_HANDOFF_PACKETS` env: unset → no behavior change (repo activation
  pattern). Value = task-domain/type allowlist (start: bug-shaped +
  engineering domain).
- Unclaimed-pool items get a packet too (owner label "unclaimed") — the
  brief is what makes claiming cheap.
- Target per owner from `ILLO_MEMBER_AGENT_TARGETS` (slice 04).
- `ask` and `acceptance_criteria` come from the triage run's structured
  output (it already produces a classification + summary; extend that
  contract, don't parse prose).
- Idempotency: `idempotency_key = f"{record_ref}:{revision}"` — re-triage of
  unchanged truth reuses the existing handoff; changed truth supersedes
  (metadata `supersedes: <old id>`; old status → `superseded`).

## Doc-1155 delta (applied at activation, not before)
Add to the triage playbook: when a packet is minted, the reply IS the
packet brief — do not also write a freeform summary; on-demand
"brief me on X" requests route through the same mint path (tool call), never
hand-assembled context. Keep the delta text in this slice file when written;
apply to live doc 1155 following the pointer-section discipline (never grow
the core).

## What the human can run/see
Dry-run mode first: `ILLO_HANDOFF_PACKETS=dry` mints nothing, logs the
would-be brief + payload to the run's evidence ledger. Read-only illo-dev
dry run (952-pattern) over a day of real triage → paste 3 sample briefs
into `assets/dry-run-05.md` for Reda's review. **Non-blocking checkpoint:**
give ~5 min for a reaction, then decide on the evidence and record the
decision here.

## Verification
- Integration test with fakes: triage completion → one handoff row, one
  threaded Slack reply (fake sender), reference attached to record;
  idempotent re-run → no duplicate.
- Failure containment: mint failure never fails the triage run (evidence
  line instead) — triage worked before packets existed and must keep
  working without them.
- Live gates before flipping the env from `dry`: sample briefs approved,
  supersede path exercised once.

## Stays green
Triage suite unchanged with env unset; full fast suite.

## Feedback that would change this slice
Reda wants packets on every triaged item (no allowlist), or wants the
digest (slice 06) to be the first surfaced moment instead of thread replies.
