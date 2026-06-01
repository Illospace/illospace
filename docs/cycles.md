# Cycles

Cycles are database-backed recurring jobs owned by the Illo scheduler.

## Runtime Model

- Recurring missions and background work live in the database as `cycles` and
  `cycle_runs`.
- The durable memory source for a Cycle is its database ledger, not the display
  thread. Threads are output/context targets that can orient future Cycle memory.
- Mission/configuration changes are recorded as immutable `cycle_revisions`.
- User or agent guidance is recorded in `cycle_guidance` and snapshotted onto
  each run.
- Output destinations are recorded in `cycle_output_targets`. Runs snapshot the
  active targets so they can repair, replace, or adapt output surfaces without
  losing the mission.
- Each terminal run writes a self-review entry to `cycle_run_evaluations` and a
  short `cycle_runs.self_review_summary`.
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
