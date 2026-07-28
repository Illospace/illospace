"""Worker fan-out evidence receipts and their atomic persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.cycle import CycleRun
from brain.systems.runs.events import run_event
from brain.systems.runs.failures import safe_terminal_run_message
from brain.systems.runs.status import (
    TERMINAL_RUN_STATUSES,
    RunStatus,
    coerce_run_status,
)
from brain.systems.runs.store import AsyncAgentRunStore


_FAILURE_PAYLOAD_KEYS = frozenset(
    {
        "kind",
        "tool",
        "child_run_id",
        "worker_run_id",
        "worker_role",
        "shard",
        "repo",
        "stage",
        "status",
        "error",
        "configuration_error",
        "provider",
        "credential",
        "failure_category",
    }
)
_RECEIPT_PAYLOAD_KEYS = frozenset(
    {
        "status",
        "completeness",
        "failures",
        "failure_count",
        "missing_shards",
        "worker_shards",
    }
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _run_id(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _unique_text(values: Sequence[Any]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            normalized
            for value in values
            if (normalized := _text(value)) is not None
        )
    )


def worker_evidence_shard(
    worker: Any,
    *,
    resource: Mapping[str, Any] | None = None,
) -> str:
    """Return the canonical shard label for a worker or one of its resources."""

    metadata = _mapping(getattr(worker, "metadata_", None))
    assignment = _mapping(metadata.get("worker_assignment"))
    assignment_metadata = _mapping(assignment.get("metadata"))
    for source in (
        _mapping(resource),
        metadata,
        assignment_metadata,
        assignment,
    ):
        for key in (
            "worker_shard",
            "shard",
            "repo",
            "name",
            "source",
            "resource_id",
        ):
            if value := _text(source.get(key)):
                return value

    allowed_resources = assignment.get("allowed_resources")
    if isinstance(allowed_resources, list) and len(allowed_resources) == 1:
        if value := _text(allowed_resources[0]):
            return value

    worker_id = _run_id(getattr(worker, "id", None))
    return (
        _text(assignment.get("id"))
        or _text(metadata.get("parent_node_id"))
        or _text(metadata.get("worker_role"))
        or f"worker-{worker_id or 'unknown'}"
    )


@dataclass(frozen=True, slots=True)
class WorkerEvidenceFailure:
    """One canonical missing-worker-evidence marker."""

    shard: str
    stage: str
    error: str
    worker_run_id: int | None = None
    worker_role: str = "worker"
    status: str | None = None
    configuration_error: str | None = None
    provider: str | None = None
    credential: str | None = None
    failure_category: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.worker_run_id,
            self.shard,
            self.stage,
            self.configuration_error,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> WorkerEvidenceFailure:
        worker_run_id = _run_id(
            payload.get("worker_run_id") or payload.get("child_run_id")
        )
        return cls(
            worker_run_id=worker_run_id,
            worker_role=_text(payload.get("worker_role")) or "worker",
            shard=(
                _text(payload.get("shard") or payload.get("repo"))
                or f"worker-{worker_run_id or 'unknown'}"
            ),
            stage=_text(payload.get("stage")) or "worker_execution",
            status=_text(payload.get("status")),
            configuration_error=_text(payload.get("configuration_error")),
            provider=_text(payload.get("provider")),
            credential=_text(payload.get("credential")),
            error=_text(payload.get("error")) or "Worker evidence is unavailable.",
            failure_category=_text(payload.get("failure_category")),
            details={
                str(key): value
                for key, value in payload.items()
                if key not in _FAILURE_PAYLOAD_KEYS
            },
        )

    @classmethod
    def for_admission(
        cls,
        *,
        worker_role: str,
        shard: str,
        configuration_error: str | None,
        provider: str | None,
        credential: str | None,
        error: str,
    ) -> WorkerEvidenceFailure:
        return cls(
            worker_role=worker_role,
            shard=shard,
            stage="worker_admission",
            status="auth_blocked",
            configuration_error=configuration_error,
            provider=provider,
            credential=credential,
            error=error,
        )

    @classmethod
    def from_terminal_worker(cls, worker: Any) -> WorkerEvidenceFailure | None:
        status = coerce_run_status(getattr(worker, "status", None))
        if status == RunStatus.COMPLETED or status not in TERMINAL_RUN_STATUSES:
            return None

        metadata = _mapping(getattr(worker, "metadata_", None))
        failure_metadata = _mapping(metadata.get("failure"))
        category = _text(failure_metadata.get("category"))
        return cls(
            worker_run_id=int(worker.id),
            worker_role=_text(metadata.get("worker_role")) or "worker",
            shard=worker_evidence_shard(worker),
            stage="worker_execution",
            status=status.value,
            error=safe_terminal_run_message(status, category),
            failure_category=category,
        )

    def to_payload(self) -> dict[str, Any]:
        payload = {
            **dict(self.details),
            "kind": "worker_tool_failure",
            "tool": "spawn_worker",
        }
        if self.worker_run_id is not None:
            payload["child_run_id"] = self.worker_run_id
            payload["worker_run_id"] = self.worker_run_id
        payload.update(
            {
                "worker_role": self.worker_role,
                "shard": self.shard,
                "stage": self.stage,
            }
        )
        for key, value in (
            ("status", self.status),
            ("configuration_error", self.configuration_error),
            ("provider", self.provider),
            ("credential", self.credential),
        ):
            if value is not None:
                payload[key] = value
        payload["error"] = self.error
        if self.failure_category is not None:
            payload["failure_category"] = self.failure_category
        return payload


@dataclass(frozen=True, slots=True)
class WorkerEvidenceReceipt:
    """The typed evidence-health receipt stored on a parent fan-out."""

    status: str | None = None
    completeness: str | None = None
    failures: tuple[WorkerEvidenceFailure, ...] = ()
    worker_shards: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> WorkerEvidenceReceipt:
        source = _mapping(payload)
        failures = tuple(
            WorkerEvidenceFailure.from_payload(item)
            for item in source.get("failures") or []
            if isinstance(item, Mapping)
        )
        return cls(
            status=_text(source.get("status")),
            completeness=_text(source.get("completeness")),
            failures=failures,
            worker_shards=_unique_text(source.get("worker_shards") or []),
            details={
                str(key): value
                for key, value in source.items()
                if key not in _RECEIPT_PAYLOAD_KEYS
            },
        )

    @property
    def missing_shards(self) -> tuple[str, ...]:
        return _unique_text([failure.shard for failure in self.failures])[:20]

    @property
    def degraded(self) -> bool:
        return self.status == "degraded" or bool(self.failures)

    def with_failures(
        self,
        failures: Sequence[WorkerEvidenceFailure],
    ) -> tuple[WorkerEvidenceReceipt, tuple[WorkerEvidenceFailure, ...]]:
        current = list(self.failures)
        identities = {failure.identity for failure in current}
        added: list[WorkerEvidenceFailure] = []
        for failure in failures:
            if failure.identity in identities:
                continue
            identities.add(failure.identity)
            current.append(failure)
            added.append(failure)
        return (
            WorkerEvidenceReceipt(
                status="degraded",
                completeness="unavailable",
                failures=tuple(current[:20]),
                worker_shards=self.worker_shards,
                details=self.details,
            ),
            tuple(added),
        )

    def completed(self, worker_shards: Sequence[str]) -> WorkerEvidenceReceipt:
        if self.degraded or self.status == "pending":
            return self
        return WorkerEvidenceReceipt(
            status="ok",
            completeness="complete",
            failures=self.failures,
            worker_shards=_unique_text(worker_shards)[:20],
            details=self.details,
        )

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.details)
        if self.status is not None:
            payload["status"] = self.status
        if self.completeness is not None:
            payload["completeness"] = self.completeness
        if self.failures:
            payload["failures"] = [
                failure.to_payload() for failure in self.failures
            ]
            payload["failure_count"] = len(self.failures)
            payload["missing_shards"] = list(self.missing_shards)
        if self.worker_shards:
            payload["worker_shards"] = list(self.worker_shards)
        return payload


def worker_evidence_receipt_for_run(run: Any) -> WorkerEvidenceReceipt:
    metadata = _mapping(getattr(run, "metadata_", None))
    return WorkerEvidenceReceipt.from_payload(metadata.get("evidence_health"))


def materialization_failures_for_worker(
    worker: Any,
    result: Any,
) -> tuple[WorkerEvidenceFailure, ...]:
    """Convert one worker materialization result to canonical evidence failures."""

    metadata = _mapping(getattr(worker, "metadata_", None))
    if getattr(worker, "parent_run_id", None) is None or not (
        metadata.get("spawned_by_tool") is True
        or metadata.get("origin") == "spawn_worker"
    ):
        return ()

    errors = [
        str(item)
        for item in [
            *(getattr(result, "errors", []) or []),
            *(getattr(result, "warnings", []) or []),
        ]
        if _text(item) is not None
    ]
    failed_resources = [
        dict(item)
        for item in [
            *(getattr(result, "failed_resources", []) or []),
            *(getattr(result, "degraded_resources", []) or []),
        ]
        if isinstance(item, Mapping)
    ]
    if not failed_resources:
        target_ref = _mapping(getattr(worker, "target_ref", None))
        snapshot = _mapping(target_ref.get("project_context_snapshot"))
        resources = snapshot.get("resources")
        failed_resources = [
            dict(item)
            for item in resources or []
            if isinstance(item, Mapping)
        ][:1]
    if not failed_resources:
        failed_resources = [{}]

    failures: list[WorkerEvidenceFailure] = []
    for index, resource in enumerate(failed_resources):
        fallback_error = (
            errors[index]
            if index < len(errors)
            else errors[0]
            if errors
            else "Project Context materialization failed."
        )
        failures.append(
            WorkerEvidenceFailure(
                worker_run_id=int(worker.id),
                worker_role=_text(metadata.get("worker_role")) or "worker",
                shard=worker_evidence_shard(worker, resource=resource),
                stage="project_context_materialization",
                error=_text(resource.get("error")) or fallback_error,
            )
        )
    return tuple(failures)


async def _lock_parent_run(session: Any, parent_run_id: int) -> AgentRunRow | None:
    return await session.get(
        AgentRunRow,
        int(parent_run_id),
        with_for_update=True,
    )


async def _lock_cycle_run(
    session: Any,
    parent_metadata: Mapping[str, Any],
) -> CycleRun | None:
    cycle_run_id = (
        parent_metadata.get("cycle_run_id")
        if parent_metadata.get("source") == "cycle"
        else None
    )
    try:
        normalized_id = int(cycle_run_id)
    except (TypeError, ValueError):
        return None
    return await session.get(
        CycleRun,
        normalized_id,
        with_for_update=True,
    )


async def _persist_parent_evidence_receipt(
    session: Any,
    *,
    parent_run_id: int,
    failures: Sequence[WorkerEvidenceFailure] = (),
    completed_worker_shards: Sequence[str] | None = None,
) -> tuple[WorkerEvidenceReceipt | None, tuple[WorkerEvidenceFailure, ...]]:
    """Lock parent then Cycle, mutate both receipts, and append events."""

    if not failures and completed_worker_shards is None:
        return None, ()

    parent = await _lock_parent_run(session, parent_run_id)
    if parent is None:
        return None, ()

    parent_metadata = dict(_mapping(parent.metadata_))
    receipt = WorkerEvidenceReceipt.from_payload(
        parent_metadata.get("evidence_health")
    )
    added: tuple[WorkerEvidenceFailure, ...] = ()
    if failures:
        receipt, added = receipt.with_failures(failures)
    if completed_worker_shards is not None:
        receipt = receipt.completed(completed_worker_shards)
    parent_metadata["evidence_health"] = receipt.to_payload()
    parent.metadata_ = parent_metadata

    cycle_run = await _lock_cycle_run(session, parent_metadata)
    if cycle_run is not None and failures:
        context_snapshot = dict(_mapping(cycle_run.context_snapshot))
        cycle_receipt = WorkerEvidenceReceipt.from_payload(
            context_snapshot.get("evidence_health")
        )
        cycle_receipt, _ = cycle_receipt.with_failures(failures)
        context_snapshot["evidence_health"] = cycle_receipt.to_payload()
        cycle_run.context_snapshot = context_snapshot

    store = AsyncAgentRunStore(session)
    for failure in added:
        await store.append_event(
            run_event(
                int(parent.id),
                "run.worker_failed",
                failure.to_payload(),
                root_run_id=parent.root_run_id or parent.id,
                producer="spawn_worker",
            )
        )
    return receipt, added


async def record_parent_evidence_failures(
    session: Any,
    *,
    parent_run_id: int,
    failures: Sequence[WorkerEvidenceFailure],
) -> tuple[WorkerEvidenceFailure, ...]:
    """Persist typed failures behind the owner's parent/Cycle lock boundary."""

    _, added = await _persist_parent_evidence_receipt(
        session,
        parent_run_id=parent_run_id,
        failures=failures,
    )
    return added


async def record_terminal_worker_evidence(
    session: Any,
    *,
    parent_run_id: int,
    workers: Sequence[Any],
    fanout_complete: bool,
) -> WorkerEvidenceReceipt | None:
    """Convert terminal workers and complete the parent receipt in one owner."""

    failures = tuple(
        failure
        for worker in workers
        if (failure := WorkerEvidenceFailure.from_terminal_worker(worker))
        is not None
    )
    worker_shards = (
        tuple(worker_evidence_shard(worker) for worker in workers)
        if fanout_complete
        else None
    )
    receipt, _ = await _persist_parent_evidence_receipt(
        session,
        parent_run_id=parent_run_id,
        failures=failures,
        completed_worker_shards=worker_shards,
    )
    return receipt


__all__ = [
    "WorkerEvidenceFailure",
    "WorkerEvidenceReceipt",
    "materialization_failures_for_worker",
    "record_parent_evidence_failures",
    "record_terminal_worker_evidence",
    "worker_evidence_receipt_for_run",
    "worker_evidence_shard",
]
