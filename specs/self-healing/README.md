# Self-healing: freeze → automatic restart, and auto-update from origin/main

## Why

The scheduler daemon has frozen four+ times (#632), twice for ~10 hours. Detection is
complete and layered (SchedulerOverdueMonitor on the API loop, escalation latch in
`scheduler_alert_latches`, GitHub-Actions deadman), but every path terminates in a Slack
message. Meanwhile the `updater` sidecar sits idle holding `/var/run/docker.sock`, able to
restart any service from a single file write. Separately, `illo update --mode compose`
(fast-forward to origin/main + rebuild + migrate + drain-aware restart) works but nothing
invokes it automatically, so merged fixes sit undeployed for days (#632's own fix, #658).

This spec closes both gaps with wiring, not new machinery.

## Slice 1 — freeze triggers a restart (in-repo)

Detector: `brain/app/scheduler/overdue_monitor.py` (`SchedulerOverdueMonitor`, runs on the
API event loop, survives daemon freezes). It already maintains a durable latch row with
`freeze_started_at` via `brain/app/scheduler/overdue_alert_state.py`.

Actuator: the updater sidecar's runtime-services queue
(`deploy/scripts/self-update-daemon.sh`, handler `process_runtime_services_request`,
2s poll). The API container mounts the same `illo_private` volume and has
`ILLO_RUNTIME_SERVICES_REQUEST_FILE` in its env. The Python writer used by
`tool_manage_runtime_services` lives in `brain/systems/runtime_settings/runtime_services.py`
— reuse its request-file writer for a system-originated request (no `User`; bypass the
workspace-identity check for this internal caller, do NOT weaken the MCP tool's auth).

Behavior:
- When a freeze has been latched for ≥ `SCHEDULER_SELF_HEAL_AFTER_MINUTES` (default 10),
  the monitor enqueues a restart of `["scheduler"]`, `requested_by` marking it as
  self-heal (e.g. `scheduler-self-heal`), unless a request or `.running` file already exists.
- Attempt cap: at most `SCHEDULER_SELF_HEAL_MAX_ATTEMPTS` (default 2) per freeze episode,
  claimed atomically through `scheduler_alert_latches` (same claim pattern as the
  escalation ladder, key e.g. `scheduler_self_heal`). When the cap is exhausted and the
  freeze persists, the next escalation message must say self-heal failed and a human is
  needed.
- Slack `#alerts` one-liner on each heal attempt ("restarting scheduler automatically,
  freeze Xm, attempt N/2") and on recovery-after-heal. Reuse `_deliver_alert` plumbing.
- Recovery (latch released) resets the episode so a future freeze gets fresh attempts.

Known subtlety (from repo audit): `scheduler_liveness_checkpoints` stays fresh while a tick
awaits a hung call (the heartbeat task keeps running), so the monitor's overdue-jobs signal
is the correct trigger — do not switch the trigger to the liveness checkpoint.

## Slice 2 — auto-update when origin/main moves (updater daemon)

In `deploy/scripts/self-update-daemon.sh`: every `ILLO_AUTO_UPDATE_POLL_SECONDS`
(default 300; counted against the 2s main loop, not a second loop), when
`ILLO_AUTO_UPDATE_ENABLED` (default `1`) and no self-update request/running file exists:
`git -C /repo fetch origin main` and compare `origin/main` to the deployed `main` HEAD.
If remote is ahead, write the normal self-update request file (`requested_by:
"auto-update"`, include old/new SHAs in the request detail). Everything downstream —
`illo update --mode compose`, ff-only sync, migrate, worker drain decision, updater
self-replacement via `schedule_updater_refresh_after_self_update` — is existing behavior.

- Fetch failures: log to the self-update log, write nothing, try again next interval
  (offline git remote must never wedge the request queues — keep the fetch time-bounded,
  e.g. `timeout 60`).
- Failed updates already land in `status.json` with an exit code; do not add a new alert
  channel in this slice.
- Compose env: add both env vars to the `updater` service with defaults.

## Slice 3 — host-level backstop (covers the supervisor dying)

Slice 1 lives in the API container. If the API is down/frozen too, nothing acts. Host-side:

1. Healthchecks for `scheduler` (and `worker`) in `deploy/compose/docker-compose.yml`:
   a small CLI probe (e.g. `python -m brain.app.cli.scheduler healthcheck`) that exits
   nonzero when the scheduler is frozen. Signal: enabled jobs overdue beyond ~10 min lag
   (reuse `async_scheduler_overdue_candidates` / `lag_seconds_at`), because the liveness
   checkpoint lies during await-hangs (see slice 1 note). Generous interval/retries so a
   busy-but-alive daemon is never marked unhealthy. Worker check analogous if a cheap
   truthful signal exists; otherwise scheduler only.
2. `deploy/scripts/install-watchdog-unit.sh` (mirror the style/scope handling of
   `install-boot-unit.sh`): installs `illospace-watchdog.service` (Type=oneshot) +
   `illospace-watchdog.timer` (every 5 min). The service runs
   `deploy/scripts/watchdog-check.sh`, which:
   - runs `deploy/scripts/inert-stack-check.sh`; on exit 3 (inert) or 4 (down) runs
     `docker compose ... up -d` (NO `--force-recreate` — same safety rationale as the
     boot unit's header comment: must never tear down a deploy in flight);
   - restarts any container currently `health=unhealthy` (`docker compose restart <svc>`);
   - skips all action when an update is in flight (self-update `.running` file present);
   - logs to `/var/log/illospace-watchdog.log` or journal.
3. README note in `deploy/compose/README.md`: one command to install boot unit + watchdog.

Out of scope: the root cause of #632 (why the tick loop stops), meetbot deploy path (#658),
and the whole-host-offline case (tailnet drop, #512's worst case) — only the GH-Actions
deadman can see that, and it already alerts.

## Acceptance

- Unit tests for the self-heal claim/cap logic (same style as overdue-monitor tests) and
  for the auto-update gating in the daemon script if a harness exists for it.
- A frozen scheduler (simulated: latch row with old `freeze_started_at`, overdue jobs)
  leads the monitor to write exactly one runtime-services request; a second evaluation
  within the same episode does not double-write; cap enforced across replicas (atomic
  claim).
- Auto-update: with remote ahead → request file written once; while `.running` exists →
  nothing; fetch failing → loop continues.
- `illo update`/upgrade.sh flows untouched and green; `deploy_compose config` valid.
- Watchdog script is a no-op on a healthy stack (exit 0, no docker actions).

## Next Agent Prompt

Slices 1 and 2 are complete in the worktree. The overdue monitor uses a durable,
bounded self-heal claim before the existing runtime-services writer, and the
updater's existing two-second loop now performs a five-minute, 60-second-bounded
origin/main poll. It queues the normal update request only for a fast-forward,
and shares the Python writer's start lock. The focused auto-update and deploy
tests pass (`89 passed`).

Next: implement Slice 3. Add an overdue-lag scheduler CLI healthcheck and a
scheduler-only Compose healthcheck. Add the host watchdog check and systemd
timer installer without any teardown or `--force-recreate` path. Do not add a
worker healthcheck unless a cheap truthful signal is found; the current worker
lifecycle phase is not one.
