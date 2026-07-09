# Slice 4 — Proactive Slack notify-loop

**Depends on Slice 1 (fresh domain + `domain_events` fed by webhooks).** This is
the cycle's new job: judgment on a timer.

## Contract unlocked
The cycle stops being a data pump. On each (more-frequent) tick it reads what
changed since last run, decides whether it's worth telling the team, and posts a
Slack digest — or stays quiet. Genuinely urgent events bypass the cycle and ping
immediately.

## API seam / changes
1. **Change feed read** — the notify-loop reads
   `query_workspace_data(sources=['domain_events'], start_at=<last_run>)`
   (reader [workspace_data.py](../../../brain/systems/runs/tool_catalog/handlers/workspace_data.py):944–998).
   Watermark = the cycle's `last_run_at` ([cycle.py](../../../brain/platform/db/models/cycle.py):69).
2. **Decision step** — per changed record decide: noteworthy? who cares (route by
   owner/`task_type`)? batch vs immediate vs no-op. Keep this as explicit, small,
   testable logic — not a vague prose instruction (that's the failure mode this
   whole feature is fixing). Also surface the **unclaimed pool** (Slice 3): name
   items waiting for an owner so folks pick them up organically, and optionally
   escalate anything unclaimed past a threshold.
3. **Post (one owner)** — emit via `post_slack_reply` with an explicit
   `channel_id` (allowed outside a Slack-triggered run,
   [handlers/slack.py](../../../brain/systems/runs/tool_catalog/handlers/slack.py):280).
   No parallel Slack client (README invariant 5). Target = the **existing team
   channel Illo already works in** (the monitored channel) — not a new channel,
   not DMs.
4. **Urgent bypass** — add a triage/webhook-time hook: on an urgent
   classification, post immediately rather than waiting for the tick. This
   triage→Slack hook does not exist today (reconciliation.py doesn't post) —
   it's net-new, and must also go through `post_slack_reply`.
5. **Cadence** — the cycle's `schedule_expr` set to the digest cadence (default
   30 min, redline). This is the "revert" target for Slice 0's frequency bump and
   Slice 1's reconcile cadence — keep the _reconcile_ poll and the _notify_ loop
   as distinct cycles with distinct jobs.

## What the human can run/see
- Make a domain change → within one tick, a digest lands in the channel naming
  what changed and who owns it.
- Trigger an urgent-labeled item → immediate ping, before the next tick.
- A quiet interval → the loop posts nothing (no-op is a valid outcome).

## Verification
- Given recorded `domain_events` since a watermark, the decision step produces
  the expected notify/no-op set (fixtures).
- Digest posts to the configured channel; watermark advances so events aren't
  re-notified next tick.
- Urgent path posts immediately and is not double-posted by the subsequent
  digest.

## Must stay green
- Existing Slack reply-in-thread behavior (event-driven) unchanged.
- The reconcile poll (Slice 1) and this notify loop remain separate — neither
  absorbs the other's job.

## Feedback that would change this slice
- Digest cadence and the exact "urgent" definition (README decision 3).
- Whether no-op-heavy intervals should still post a heartbeat (default: no).
