"""Import-safe worker lifecycle publication and cover observation contract."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
from typing import Sequence


# This deliberately is not configurable. The worker and the host-side deploy
# scripts must agree even though upgrade.sh and runtime-services.sh do not source
# the Compose environment.
WORKER_LIFECYCLE_PATH = Path("/tmp/illo-worker-lifecycle-phase")
UNKNOWN_WORKER_LIFECYCLE_PHASE = "unknown"


class WorkerLifecyclePhase(StrEnum):
    """Externally observable AgentRun worker lifecycle."""

    STARTING = "starting"
    CLAIMING = "claiming"
    DRAINING = "draining"
    STOPPED = "stopped"


class WorkerContainerState(StrEnum):
    """Container liveness as observed by the Docker host."""

    RUNNING = "running"
    DEFINITIVELY_NOT_RUNNING = "definitively_not_running"
    UNKNOWN = "unknown"


class WorkerCoverObservation(StrEnum):
    """Whether one container can safely cover destruction of another worker."""

    CLAIMING = "claiming"
    PENDING = "pending"
    DEFINITIVELY_NOT_CLAIMING = "definitively_not_claiming"


@dataclass(frozen=True, slots=True)
class WorkerLifecycleRecord:
    """A lifecycle phase owned by exactly one operating-system process."""

    phase: WorkerLifecyclePhase
    pid: int
    process_start_identity: str

    def as_json(self) -> str:
        return json.dumps(
            {
                "phase": self.phase.value,
                "pid": self.pid,
                "process_start_identity": self.process_start_identity,
            },
            separators=(",", ":"),
        )


def parse_worker_lifecycle_phase(
    raw: str | WorkerLifecyclePhase | None,
) -> WorkerLifecyclePhase | None:
    """Normalize a published phase; missing and invalid observations are unknown."""

    if isinstance(raw, WorkerLifecyclePhase):
        return raw
    value = str(raw or "").strip()
    if not value or value == UNKNOWN_WORKER_LIFECYCLE_PHASE:
        return None
    try:
        return WorkerLifecyclePhase(value)
    except ValueError:
        return None


def _process_start_identity(pid: int) -> str | None:
    """Return an identity that changes when an operating-system PID is reused."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        stat = ""
    if stat:
        try:
            # Linux procfs field 22 is the process start time in clock ticks.
            start_ticks = stat.rsplit(")", 1)[1].split()[19]
        except (IndexError, ValueError):
            return None
        return f"linux-start-ticks:{start_ticks}"

    # Workers run in Linux containers, where procfs supplies the authoritative
    # process-generation identity. Without it, no phase is safe to publish.
    return None


def _parse_worker_lifecycle_record(raw: str) -> WorkerLifecycleRecord | None:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    phase = parse_worker_lifecycle_phase(payload.get("phase"))
    pid = payload.get("pid")
    process_start_identity = payload.get("process_start_identity")
    if (
        phase is None
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(process_start_identity, str)
        or not process_start_identity
    ):
        return None
    return WorkerLifecycleRecord(
        phase=phase,
        pid=pid,
        process_start_identity=process_start_identity,
    )


def publish_worker_lifecycle_phase(phase: WorkerLifecyclePhase | str) -> None:
    """Atomically publish a phase owned by the current worker process generation."""

    try:
        normalized = parse_worker_lifecycle_phase(phase)
        if normalized is None:
            raise ValueError(f"cannot publish unknown worker lifecycle phase: {phase!r}")
        pid = os.getpid()
        process_start_identity = _process_start_identity(pid)
        if process_start_identity is None:
            raise RuntimeError(f"cannot identify worker process generation for pid {pid}")
        record = WorkerLifecycleRecord(
            phase=normalized,
            pid=pid,
            process_start_identity=process_start_identity,
        )
        temporary_path = WORKER_LIFECYCLE_PATH.with_name(
            f".{WORKER_LIFECYCLE_PATH.name}.{pid}.tmp"
        )
        temporary_path.write_text(f"{record.as_json()}\n", encoding="utf-8")
        os.replace(temporary_path, WORKER_LIFECYCLE_PATH)
    except Exception:
        # A failed transition must not leave the preceding `claiming` record
        # valid while the runner has already begun to drain.
        try:
            WORKER_LIFECYCLE_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_worker_lifecycle_phase() -> WorkerLifecyclePhase | None:
    """Read a phase only while the process generation that published it exists."""

    try:
        raw = WORKER_LIFECYCLE_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    record = _parse_worker_lifecycle_record(raw)
    if record is None:
        return None
    if _process_start_identity(record.pid) != record.process_start_identity:
        return None
    return record.phase


def observe_worker_cover(
    container_state: WorkerContainerState | str,
    phase: WorkerLifecyclePhase | str | None,
) -> WorkerCoverObservation:
    """Combine liveness and generation-validated phase into one safety answer."""

    try:
        normalized_container_state = WorkerContainerState(container_state)
    except ValueError:
        normalized_container_state = WorkerContainerState.UNKNOWN

    if normalized_container_state is WorkerContainerState.DEFINITIVELY_NOT_RUNNING:
        return WorkerCoverObservation.DEFINITIVELY_NOT_CLAIMING
    if normalized_container_state is WorkerContainerState.UNKNOWN:
        return WorkerCoverObservation.PENDING

    normalized_phase = parse_worker_lifecycle_phase(phase)
    if normalized_phase is WorkerLifecyclePhase.CLAIMING:
        return WorkerCoverObservation.CLAIMING
    if normalized_phase in {
        WorkerLifecyclePhase.DRAINING,
        WorkerLifecyclePhase.STOPPED,
    }:
        return WorkerCoverObservation.DEFINITIVELY_NOT_CLAIMING
    return WorkerCoverObservation.PENDING


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("read")
    observe_parser = subparsers.add_parser("observe")
    observe_parser.add_argument("container_state")
    observe_parser.add_argument("phase")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "read":
        phase = read_worker_lifecycle_phase()
        print(phase.value if phase is not None else UNKNOWN_WORKER_LIFECYCLE_PHASE)
        return 0
    print(observe_worker_cover(args.container_state, args.phase).value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "UNKNOWN_WORKER_LIFECYCLE_PHASE",
    "WORKER_LIFECYCLE_PATH",
    "WorkerContainerState",
    "WorkerCoverObservation",
    "WorkerLifecyclePhase",
    "WorkerLifecycleRecord",
    "observe_worker_cover",
    "parse_worker_lifecycle_phase",
    "publish_worker_lifecycle_phase",
    "read_worker_lifecycle_phase",
]
