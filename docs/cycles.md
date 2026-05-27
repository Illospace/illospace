# Cycles

Cycles are database-backed recurring jobs owned by the Illo scheduler.

## Runtime Model

- Recurring prompts and background work live in the database as `cycles` and
  `cycle_runs`.
- One-time reminders use the same table with a schedule expression of
  `at:<ISO datetime>`. The scheduler runs them once, clears `next_run_at`, and
  disables the cycle after it claims the run.
- Built-in nightly work is registered through scheduler catalog jobs.
- The scheduler daemon is started from `ops/illo-scheduler.service` in
  self-hosted production.
- Illo does not require OS crontab entries for recurring product behavior.

## Operational Notes

- Run one scheduler daemon per deployment.
- Keep scheduler configuration in exported environment variables or an external
  environment file such as `~/.config/illo-brain/production.env`.
- Do not commit generated run logs, private journals, or operator notes.

## 2026-05-27 Production Review

Observed on the Tailscale production server:

- 5 active cycle definitions.
- 18 historical cycle runs.
- Run states: 7 completed, 5 skipped, 4 queued, 2 running.
- Stuck active rows included old queued runs, one missed daily Illo review, and
  running rows whose underlying AgentRun rows had already reached terminal
  states.
- The worker process saturated Postgres with long-lived `idle in transaction`
  sessions after asyncpg connections crossed event loops between the legacy
  cycle scheduler and agent-runner supervisor.

Immediate reliability checklist:

- [x] Restore production DB access by restarting the unhealthy worker.
- [x] Confirm exact cycle and cycle-run state from Postgres.
- [x] Make worker+cycle-scheduler mode use loop-safe DB pooling.
- [x] Recover recent stale queued CycleRun rows instead of leaving them queued.
- [x] Settle stale running CycleRun rows when their AgentRun is already
  terminal.
- [x] Avoid firing ancient missed runs by marking them skipped once they are
  outside the catch-up window.
- [x] Prevent `async_execute_cycle_run` from re-executing non-queued rows.
- [x] Add focused regression tests for pooling and stale-run recovery.

Product redesign checklist:

- [ ] Treat a Cycle as an autonomous recurring mission, not a scheduled message
  inside a thread.
- [ ] Decouple execution state from thread state; a busy output/context thread
  should not make the Cycle skip.
- [ ] Make cadence, context envelope, output contract, run ledger, and learning
  loop first-class primitives.
- [ ] Keep autonomy broad by default; use system-level safety rules rather than
  a Cycle-specific permission layer.
- [ ] Add visible recovery/observability for stale queued, stale running,
  skipped, and missed-window runs.
