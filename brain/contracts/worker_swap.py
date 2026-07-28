"""Canonical import-safe contracts for worker replacement and lifecycle."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence

from brain.contracts.statuses import OPEN_RUN_STATUS_VALUES, WorkerLifecyclePhase


# This deliberately is not configurable. The worker and the host-side deploy
# scripts must agree even though upgrade.sh and runtime-services.sh do not source
# the Compose environment.
WORKER_LIFECYCLE_PHASE_PATH = Path("/tmp/illo-worker-lifecycle-phase")
UNKNOWN_WORKER_LIFECYCLE_PHASE = "unknown"


class WorkerSwapDecision(StrEnum):
    """The deployment action implied by one database snapshot."""

    UNKNOWN = "unknown"
    REPLACE = "replace"
    DRAIN = "drain"


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


def publish_worker_lifecycle_phase(phase: WorkerLifecyclePhase | str) -> None:
    """Atomically publish one canonical phase for host-side ``docker exec``."""

    normalized = parse_worker_lifecycle_phase(phase)
    if normalized is None:
        raise ValueError(f"cannot publish unknown worker lifecycle phase: {phase!r}")
    temporary_path = WORKER_LIFECYCLE_PHASE_PATH.with_name(
        f".{WORKER_LIFECYCLE_PHASE_PATH.name}.tmp"
    )
    temporary_path.write_text(f"{normalized.value}\n", encoding="utf-8")
    os.replace(temporary_path, WORKER_LIFECYCLE_PHASE_PATH)


def read_worker_lifecycle_phase() -> WorkerLifecyclePhase | None:
    """Read the process-independent phase file without importing application code."""

    try:
        raw = WORKER_LIFECYCLE_PHASE_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_worker_lifecycle_phase(raw)


def worker_lifecycle_may_proceed(
    phase: WorkerLifecyclePhase | str | None,
) -> bool:
    """Permit observation/waiting, but no destructive action.

    A starting or temporarily unknown worker may still become a claimer. A
    draining or stopped worker cannot.
    """

    normalized = parse_worker_lifecycle_phase(phase)
    return normalized in {None, WorkerLifecyclePhase.STARTING, WorkerLifecyclePhase.CLAIMING}


def worker_lifecycle_is_claiming(
    phase: WorkerLifecyclePhase | str | None,
) -> bool:
    """Return whether this observation can cover destruction of another worker."""

    return parse_worker_lifecycle_phase(phase) is WorkerLifecyclePhase.CLAIMING


@dataclass(frozen=True, slots=True)
class WorkerSwapRun:
    id: int
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", int(self.id))
        object.__setattr__(self, "status", str(self.status))
        if self.status not in OPEN_RUN_STATUS_VALUES:
            raise ValueError(f"{self.status!r} is not a worker-swap blocking status")

    def as_dict(self) -> dict[str, int | str]:
        return {"id": self.id, "status": self.status}


@dataclass(frozen=True, slots=True)
class WorkerSwapSnapshot:
    """Structured result consumed by every worker replacement path."""

    known: bool
    runs: tuple[WorkerSwapRun, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.runs, key=lambda run: run.id))
        if not self.known and ordered:
            raise ValueError("an unknown worker-swap snapshot cannot contain runs")
        if len({run.id for run in ordered}) != len(ordered):
            raise ValueError("worker-swap snapshot contains duplicate run ids")
        object.__setattr__(self, "runs", ordered)

    @property
    def decision(self) -> WorkerSwapDecision:
        if not self.known:
            return WorkerSwapDecision.UNKNOWN
        if self.runs:
            return WorkerSwapDecision.DRAIN
        return WorkerSwapDecision.REPLACE

    @property
    def count(self) -> int | None:
        return len(self.runs) if self.known else None

    @property
    def run_ids(self) -> tuple[int, ...]:
        return tuple(run.id for run in self.runs)

    @property
    def run_ids_csv(self) -> str:
        return ",".join(str(run_id) for run_id in self.run_ids)

    @property
    def details(self) -> str:
        if not self.known:
            return "unknown"
        return ",".join(f"{run.id}:{run.status}" for run in self.runs)

    @property
    def report(self) -> str:
        return (
            f"Worker pre-swap check: {self.count} interactive run(s) in flight "
            f"(run ids: {self.run_ids_csv}; id/status: {self.details})"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "decision": self.decision.value,
            "count": self.count,
            "run_ids": list(self.run_ids),
            "runs": [run.as_dict() for run in self.runs],
            "policy_statuses": list(OPEN_RUN_STATUS_VALUES),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), separators=(",", ":"))


def worker_swap_snapshot(rows: Iterable[Sequence[Any] | dict[str, Any]]) -> WorkerSwapSnapshot:
    normalized: list[WorkerSwapRun] = []
    for row in rows:
        if isinstance(row, dict):
            run_id, status = row.get("id"), row.get("status")
        else:
            run_id, status = row
        normalized.append(WorkerSwapRun(id=int(run_id), status=str(status)))
    return WorkerSwapSnapshot(known=True, runs=tuple(normalized))


def unknown_worker_swap_snapshot() -> WorkerSwapSnapshot:
    return WorkerSwapSnapshot(known=False)


def parse_worker_swap_snapshot(raw: str) -> WorkerSwapSnapshot:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("worker-swap snapshot must be a JSON object")
    policy_statuses = payload.get("policy_statuses")
    if policy_statuses != list(OPEN_RUN_STATUS_VALUES):
        raise ValueError("worker-swap snapshot was not derived from the canonical policy")
    known = payload.get("known")
    if not isinstance(known, bool):
        raise ValueError("worker-swap snapshot known flag must be boolean")
    rows = payload.get("runs")
    if not isinstance(rows, list):
        raise ValueError("worker-swap snapshot runs must be a list")
    snapshot = (
        worker_swap_snapshot(rows)
        if known
        else unknown_worker_swap_snapshot()
    )
    if payload.get("decision") != snapshot.decision.value:
        raise ValueError("worker-swap snapshot decision does not match its runs")
    if payload.get("count") != snapshot.count:
        raise ValueError("worker-swap snapshot count does not match its runs")
    if payload.get("run_ids") != list(snapshot.run_ids):
        raise ValueError("worker-swap snapshot run ids do not match its runs")
    return snapshot


def worker_swap_rows_sql() -> str:
    """Return the Compose adapter query derived from the canonical policy."""

    statuses = ", ".join("'" + status.replace("'", "''") + "'" for status in OPEN_RUN_STATUS_VALUES)
    return (
        "SELECT COALESCE("
        "json_agg(json_build_object('id', id, 'status', status) ORDER BY id), "
        "'[]'::json"
        ") FROM agent_runs "
        f"WHERE status IN ({statuses});"
    )


def _field_value(snapshot: WorkerSwapSnapshot, field: str) -> str:
    if field == "decision":
        return snapshot.decision.value
    if field == "count":
        return str(snapshot.count) if snapshot.count is not None else "unknown"
    if field == "ids":
        return snapshot.run_ids_csv
    if field == "details":
        return snapshot.details
    if field == "known":
        return "true" if snapshot.known else "false"
    if field == "report":
        return snapshot.report
    raise ValueError(f"unknown worker-swap snapshot field: {field}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("unknown")
    subparsers.add_parser("sql")
    subparsers.add_parser("from-rows")
    subparsers.add_parser("validate")
    subparsers.add_parser("lifecycle-read")
    lifecycle_validate_parser = subparsers.add_parser("lifecycle-validate")
    lifecycle_validate_parser.add_argument("phase")
    lifecycle_may_proceed_parser = subparsers.add_parser("lifecycle-may-proceed")
    lifecycle_may_proceed_parser.add_argument("phase")
    lifecycle_is_claiming_parser = subparsers.add_parser("lifecycle-is-claiming")
    lifecycle_is_claiming_parser.add_argument("phase")
    field_parser = subparsers.add_parser("field")
    field_parser.add_argument(
        "name",
        choices=("decision", "count", "ids", "details", "known", "report"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "unknown":
        print(unknown_worker_swap_snapshot().as_json())
        return 0
    if args.command == "sql":
        print(worker_swap_rows_sql())
        return 0
    if args.command == "lifecycle-read":
        phase = read_worker_lifecycle_phase()
        print(phase.value if phase is not None else UNKNOWN_WORKER_LIFECYCLE_PHASE)
        return 0
    if args.command == "lifecycle-validate":
        phase = str(args.phase).strip()
        if phase == UNKNOWN_WORKER_LIFECYCLE_PHASE:
            return 0
        normalized = parse_worker_lifecycle_phase(phase)
        return 0 if normalized is not None and normalized.value == phase else 1
    if args.command == "lifecycle-may-proceed":
        return 0 if worker_lifecycle_may_proceed(args.phase) else 1
    if args.command == "lifecycle-is-claiming":
        return 0 if worker_lifecycle_is_claiming(args.phase) else 1

    raw = sys.stdin.read()
    if args.command == "from-rows":
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise ValueError("worker-swap query result must be a JSON list")
        print(worker_swap_snapshot(rows).as_json())
        return 0

    snapshot = parse_worker_swap_snapshot(raw)
    if args.command == "validate":
        return 0
    print(_field_value(snapshot, args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "UNKNOWN_WORKER_LIFECYCLE_PHASE",
    "WORKER_LIFECYCLE_PHASE_PATH",
    "WorkerLifecyclePhase",
    "WorkerSwapDecision",
    "WorkerSwapRun",
    "WorkerSwapSnapshot",
    "parse_worker_lifecycle_phase",
    "parse_worker_swap_snapshot",
    "publish_worker_lifecycle_phase",
    "read_worker_lifecycle_phase",
    "unknown_worker_swap_snapshot",
    "worker_lifecycle_is_claiming",
    "worker_lifecycle_may_proceed",
    "worker_swap_rows_sql",
    "worker_swap_snapshot",
]
