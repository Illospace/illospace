"""Canonical worker-swap policy snapshot and presentation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import StrEnum
import json
import sys
from typing import Any, Iterable, Sequence

from brain.contracts.statuses import OPEN_RUN_STATUS_VALUES


class WorkerSwapDecision(StrEnum):
    """The deployment action implied by one database snapshot."""

    UNKNOWN = "unknown"
    REPLACE = "replace"
    DRAIN = "drain"


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
    "WorkerSwapDecision",
    "WorkerSwapRun",
    "WorkerSwapSnapshot",
    "parse_worker_swap_snapshot",
    "unknown_worker_swap_snapshot",
    "worker_swap_rows_sql",
    "worker_swap_snapshot",
]
