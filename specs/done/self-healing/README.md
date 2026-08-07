# Self-healing scheduler and automatic Compose updates

## Overview

Illospace now has three recovery layers for a single-server
Compose deployment:

- the API process detects scheduler freezes and requests a bounded scheduler
  restart through the updater sidecar;
- the updater sidecar polls `origin/main` and sends new commits through the
  normal update path;
- a host systemd timer reconciles an inert stack and restarts a Compose service
  when one of its regular containers is unhealthy.

These layers close two operational gaps. A scheduler freeze previously ended
in an alert even though the deployment already had a restart actuator. Merged
fixes also remained undeployed until an operator requested an update. The host
layer remains independent because in-container recovery cannot act when the
API and its supervisor are both unavailable.

## Why it has this shape

### Overdue work is the freeze signal

The scheduler heartbeat is emitted by an independent asyncio task. It stays
fresh while a scheduler tick waits forever in a hung call. A heartbeat-based
probe would therefore report the known failure mode as healthy. Enabled,
unpaused scheduler jobs with stale `next_run_at` values and no active lease are
the durable evidence used by both the API monitor and the container probe.

The source of that evidence is
`brain.app.scheduler.read_models.async_scheduler_overdue_candidates`. The API
policy starts at `brain.app.scheduler.overdue_monitor.SchedulerOverdueMonitor`;
the Docker probe starts at `brain.app.cli.scheduler.cmd_healthcheck`.

### Recovery is durable, bounded, and episode-scoped

Multiple API replicas can see the same freeze. A process-local counter would
allow each replica to restart the scheduler independently, and a counter reset
by process restart would never enforce a real cap. The self-heal claim therefore
lives beside the alert latch in `scheduler_alert_latches`. Atomic claims bind
the attempt count to the overdue episode. Confirmed recovery releases the alert
latch but retains the attempt row, so it cannot erase a concurrent claim. A
claim for a later episode atomically rebinds that row and resets its count.

The durable policy is in
`brain.app.scheduler.overdue_alert_state.claim_scheduler_self_heal` and
`brain.platform.db.models.scheduler.SchedulerAlertLatch`. Migration
`0056_scheduler_self_heal_attempts` adds the attempt counter. An explicit busy
result does not consume an attempt because no restart was requested.
Queue-writer exceptions consume the claimed attempt, as does a request that was
accepted and then failed, so a broken actuator cannot retry forever.

### Internal recovery reuses the operator actuator

The runtime-services file queue was already the boundary between the API
container and the Docker-owning updater sidecar. Self-heal uses that same
writer and the same allowed service catalog. It does not grant the MCP tool a
new authorization path and does not give the API direct Docker access.

The internal entry point is
`brain.systems.runtime_settings.runtime_services.async_try_restart_runtime_services`.
`brain.systems.runtime_settings.sidecar_queue.SidecarQueue` owns queue paths
and atomic JSON writes. `_async_queue_runtime_services_restart` in
`brain/systems/runtime_settings/runtime_services.py` owns the start-lock and
check-lock-recheck sequence. The updater consumes the request in
`deploy/scripts/self-update-daemon.sh:process_runtime_services_request`.

### Automatic updates preserve the proven deployment path

Polling only decides whether to enqueue an update. It does not replace the
update implementation. A bounded fetch compares local `main` with
`origin/main`; only a fast-forward remote advance creates a request. Equal,
divergent, and local-ahead histories do nothing. This keeps automatic updates
from rewriting local history. An offline remote can delay the serial sidecar
loop for at most the 60-second fetch timeout, but it cannot wedge the queues.

`deploy/scripts/self-update-daemon.sh:poll_auto_update_if_due` and
`maybe_queue_auto_update` own this decision. Accepted requests still run
`./illo update --mode compose`, including the existing migration,
drain-aware worker swap, and updater replacement behavior.

### The host watchdog never acts like a deploy

The watchdog is a repair loop, not a rollout mechanism. It may run `docker
compose up -d` when the stack is inert or down, and it may restart a service
that Docker already labels unhealthy. It must never use forced recreation or
tear the stack down. It checks the updater's private-volume `.running` marker
inside the updater container before inspection and again immediately before
each mutation. Standard output and errors go to the systemd journal.

The policy is in `deploy/scripts/watchdog-check.sh`. Unit generation and the
five-minute timer are in `deploy/scripts/install-watchdog-unit.sh`. The latter
matches the system/user scope and lingering behavior of
`deploy/scripts/install-boot-unit.sh`.

## Invariants

- Scheduler freeze decisions key off overdue jobs, never the liveness
  checkpoint.
- A freeze episode has one durable attempt budget shared by all API replicas.
- Immediately before a restart request, the monitor confirms that overdue work
  still exists and that the durable alert latch still identifies the episode
  that claimed the attempt.
- Recovery closes the alert episode; a later distinct episode atomically
  resets the retained self-heal attempt budget.
- System-originated restarts use the existing runtime-services allowlist and
  file queue; MCP authorization remains unchanged.
- Auto-update fetches are time-bounded and enqueue only fast-forward advances
  of `origin/main`.
- The existing `illo update` and `deploy/scripts/upgrade.sh` flows remain the
  only update execution path.
- The watchdog rechecks update state before every Compose mutation.
- Host reconciliation uses `up -d` without forced recreation and has no
  teardown action.
- A Docker healthcheck is present only when it has a cheap, truthful signal.

## Deliberate departures and rejected alternatives

No worker healthcheck was added. The worker lifecycle phase can remain
`claiming` while work is hung, so using it would turn another presence signal
into a false liveness guarantee. The watchdog still detects a missing or
drained worker through `deploy/scripts/inert-stack-check.sh`.

The watchdog does not read the updater marker from a host path. The marker is
inside the named `illo_private` volume, so the check reads it with `docker exec`
in the regular updater container. If the updater container is absent, the
marker cannot be inspected; treating that state as idle is necessary for the
same watchdog to recover a fully down stack. During a normal update, the
updater remains present until it clears the marker.

The scheduler healthcheck does not use the monitor's alert latch. A container
probe must be read-only and useful even when the API monitor is unavailable.
It applies `SCHEDULER_SELF_HEAL_AFTER_MINUTES` directly to job lag; the API
monitor applies the same configured duration after its overdue episode begins.

## Code and test map

- Detection and claims:
  `brain/app/scheduler/overdue_monitor.py`,
  `brain/app/scheduler/overdue_alert_state.py`,
  `brain/app/scheduler/read_models.py`, and
  `tests/test_scheduler_overdue_monitor.py`.
- Runtime restart queue:
  `brain/systems/runtime_settings/runtime_services.py`,
  `brain/systems/runtime_settings/sidecar_queue.py`, and
  `tests/test_runtime_settings.py`.
- Automatic update poll:
  `deploy/scripts/self-update-daemon.sh` and
  `tests/test_self_update_daemon.py`.
- Container probe and Compose wiring:
  `brain/app/cli/scheduler.py`,
  `deploy/compose/docker-compose.yml`, and
  `tests/test_scheduler_cli.py`.
- Host backstop:
  `deploy/scripts/watchdog-check.sh`,
  `deploy/scripts/install-watchdog-unit.sh`,
  `tests/test_watchdog.py`, and
  `tests/test_safe_deploy.py`.

## Boundaries

This recovery system does not diagnose the scheduler tick-loop root cause. It
also cannot recover a powered-off or network-isolated host; the external
deadman remains responsible for detecting that state. Meetbot-specific update
and deployment mechanics remain outside this work; an unhealthy Meetbot in the
same active Compose project is still eligible for the generic watchdog restart.

There was no visual baseline or inspiration asset for this operational change.
