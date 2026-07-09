# Slice 0 — Stopgaps (ship today)

**No infra dependency. No/low code. Uses runtime surfaces that already exist.**
These buy freshness and correct routing _now_ while Slices 1/3 land, and are
explicitly retired by them.

## Contract unlocked
- Staleness window drops from ~12h to < 1h immediately.
- Business/PM issues route to Reda instead of being guessed.
- The "nudge the author, never assign a reviewer" norm is encoded in the system
  for the first time.

## API seam / changes
1. **Cycle frequency bump** — change the GitHub-sync cycle's `schedule_expr`
   (twice-daily → every 30–60 min). This is a `cycles` row edit (runtime data),
   not code. Owner: the cycle; see [cycle.py](../../../brain/platform/db/models/cycle.py):45.
   _Transitional: reverts to a low-freq reconciliation cadence once Slice 1
   webhooks deliver._
2. **Policy `instruction` route** — via `manage_inbound`
   (update_policy) set the business/PM source policy's `instructions` to route
   those repos' items to Reda and to not guess owners for others. Injected into
   triage at [service.py](../../../brain/systems/inbound/service.py):989–992.
   _Transitional: removed/migrated when Slice 3's typed rule lands._
3. **SOUL norm** — via `manage_soul` (action=replace) add to the `## Coordination`
   block of [soul.py](../../../brain/systems/personality/soul.py):57–66:
   "When coordinating on a GitHub issue or PR, nudge its author to act; never
   assign a reviewer or a coordination owner." (Encoded nowhere today.)

## What the human can run/see
- Post a test issue in a business/PM repo → observe it triaged and owned by Reda.
- Confirm the cycle's `next_run_at` reflects the new cadence.
- `manage_soul` action=read shows the new Coordination line.

## Verification
- A business/PM-repo item resolves `owner = Reda` (not skipped, not guessed).
- A non-business item still routes by existing behavior (no regression).
- Freshness: max observed domain lag < the new cycle interval.

## Must stay green
- Existing triage for other sources unchanged.
- No code paths touched (this slice is runtime config only) — if any code edit
  sneaks in, it belongs in a later slice.

## Feedback that would change this slice
- If Reda wants business/PM as per-owner DMs vs a shared thread now.
- If the cycle turns out to do more than GitHub sync (A1) — then the frequency
  bump needs a narrower target.
