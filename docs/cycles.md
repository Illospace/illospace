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
