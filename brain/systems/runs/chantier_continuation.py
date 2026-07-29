"""Event-driven continuation for worker fan-outs.

The chantier continuation remains the primary, backwards-compatible scope.
Opted-in non-chantier fan-outs fall back to their parent run's own thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunRow,
)
from brain.systems.runs.domain import ArtifactType
from brain.systems.runs.evidence_health import (
    WorkerEvidenceReceipt,
    record_terminal_worker_evidence,
    worker_evidence_receipt_for_run,
)
from brain.systems.runs.events import run_event
from brain.systems.runs.status import TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.work_intake import (
    AGENT_RUN_CONTINUATION_TARGET,
    WorkIntakeEvent,
    admit_work,
)


CONTINUATION_QUEUED_EVENT = "run.chantier_continuation_queued"
CONTINUATION_SOURCE = "chantier_continuation"
GENERIC_CONTINUATION_QUEUED_EVENT = "run.worker_continuation_queued"
GENERIC_CONTINUATION_SOURCE = "worker_continuation"
_MAX_CHILD_OUTPUT_CHARS = 6_000
_MAX_OUTPUTS_CHARS = 24_000


@dataclass(frozen=True, slots=True)
class ChantierScope:
    record_id: int
    domain_id: int | None
    source_run: AgentRunRow


async def queue_chantier_continuation_for_terminal_run(
    session: AsyncSession,
    *,
    terminal_run_id: int,
) -> int | None:
    """Queue one continuation after a worker fan-out reaches its barrier.

    The fan-out parent row is the serialization boundary. The continuation's
    source idempotency key is also derived from that parent, so a retry or two
    workers reaching terminal state together cannot admit duplicate runs.
    Chantier-scoped fan-outs retain their original behavior; other fan-outs
    must explicitly opt in through worker metadata.
    """

    terminal_run = await session.get(AgentRunRow, int(terminal_run_id))
    if terminal_run is None:
        return None

    anchor_id = await _fanout_anchor_id(session, terminal_run)
    if anchor_id is None:
        return None
    anchor = await _lock_run(session, anchor_id)
    if anchor is None:
        return None

    workers = await _spawned_workers(session, anchor.id)
    if not workers:
        return None
    anchor_terminal = (
        coerce_run_status(anchor.status) in TERMINAL_RUN_STATUSES
    )
    all_workers_terminal = not any(
        coerce_run_status(worker.status) not in TERMINAL_RUN_STATUSES
        for worker in workers
    )
    await _record_fanout_evidence_health(
        session,
        anchor=anchor,
        workers=workers,
        anchor_terminal=anchor_terminal,
        all_workers_terminal=all_workers_terminal,
    )
    if not anchor_terminal or not all_workers_terminal:
        return None

    store = AsyncAgentRunStore(session)
    if await store.has_event_type(anchor.id, CONTINUATION_QUEUED_EVENT):
        return await _existing_continuation_run_id(session, anchor.id)

    scope = await _resolve_chantier_scope(session, anchor)
    if scope is None:
        return await _queue_generic_continuation(
            session,
            store=store,
            anchor=anchor,
            workers=workers,
        )

    child_results = await _child_results(session, workers)
    evidence_receipt = worker_evidence_receipt_for_run(anchor)
    evidence_health = evidence_receipt.to_payload()
    idempotency_key = f"chantier:continuation:{anchor.id}"
    target = _continuation_target(scope.source_run, scope=scope, fanout_run_id=anchor.id)
    admission = await admit_work(
        session,
        WorkIntakeEvent(
            source=CONTINUATION_SOURCE,
            event_type="chantier.child_runs_completed",
            org_id=str(anchor.org_id or ""),
            actor={
                "id": scope.source_run.user_id or anchor.user_id,
                "org_id": anchor.org_id,
                "principal_type": "agent_runtime",
                "name": "chantier continuation hook",
            },
            target=target,
            payload={
                "message": _continuation_message(
                    scope=scope,
                    fanout_run_id=anchor.id,
                    child_results=child_results,
                    evidence_receipt=evidence_receipt,
                ),
                "workspace_ref": dict(scope.source_run.workspace_ref or {}),
                "metadata": _continuation_metadata(
                    scope.source_run,
                    scope=scope,
                    fanout_run_id=anchor.id,
                    worker_ids=[int(worker.id) for worker in workers],
                    evidence_health=evidence_health,
                ),
            },
            policy={
                "producer": "run_completion_hook",
                "idempotency_key": idempotency_key,
                "run_event": "chantier_continuation",
            },
        ),
    )
    if not admission.ok or admission.run_id is None:
        raise RuntimeError(
            "Chantier continuation admission failed for fan-out "
            f"{anchor.id}: {admission.skipped_reason or 'missing run id'}"
        )

    await store.append_event(
        run_event(
            anchor.id,
            CONTINUATION_QUEUED_EVENT,
            {
                "continuation_run_id": int(admission.run_id),
                "chantier_record_id": scope.record_id,
                "chantier_domain_id": scope.domain_id,
                "anchor_thread_id": scope.source_run.thread_id,
                "worker_run_ids": [int(worker.id) for worker in workers],
            },
            root_run_id=anchor.root_run_id or anchor.id,
            producer="run_completion_hook",
        )
    )
    return int(admission.run_id)


async def _queue_generic_continuation(
    session: AsyncSession,
    *,
    store: AsyncAgentRunStore,
    anchor: AgentRunRow,
    workers: list[AgentRunRow],
) -> int | None:
    if not _wants_generic_continuation(workers):
        return None
    if await store.has_event_type(anchor.id, GENERIC_CONTINUATION_QUEUED_EVENT):
        return await _existing_generic_continuation_run_id(session, anchor.id)

    child_results = await _child_results(session, workers)
    evidence_receipt = worker_evidence_receipt_for_run(anchor)
    evidence_health = evidence_receipt.to_payload()
    idempotency_key = f"worker:continuation:{anchor.id}"
    admission = await admit_work(
        session,
        WorkIntakeEvent(
            source=GENERIC_CONTINUATION_SOURCE,
            event_type="worker.child_runs_completed",
            org_id=str(anchor.org_id or ""),
            actor={
                "id": anchor.user_id,
                "org_id": anchor.org_id,
                "principal_type": "agent_runtime",
                "name": "worker continuation hook",
            },
            target=_generic_continuation_target(anchor),
            payload={
                "message": _generic_continuation_message(
                    anchor=anchor,
                    child_results=child_results,
                    evidence_receipt=evidence_receipt,
                ),
                "workspace_ref": dict(anchor.workspace_ref or {}),
                "model_policy": dict(anchor.model_policy or {}),
                "metadata": _generic_continuation_metadata(
                    anchor,
                    worker_ids=[int(worker.id) for worker in workers],
                    evidence_health=evidence_health,
                ),
            },
            policy={
                "producer": "run_completion_hook",
                "idempotency_key": idempotency_key,
                "run_event": "worker_continuation",
            },
        ),
    )
    if not admission.ok or admission.run_id is None:
        raise RuntimeError(
            "Worker continuation admission failed for fan-out "
            f"{anchor.id}: {admission.skipped_reason or 'missing run id'}"
        )

    await store.append_event(
        run_event(
            anchor.id,
            GENERIC_CONTINUATION_QUEUED_EVENT,
            {
                "continuation_run_id": int(admission.run_id),
                "anchor_thread_id": anchor.thread_id,
                "worker_run_ids": [int(worker.id) for worker in workers],
            },
            root_run_id=anchor.root_run_id or anchor.id,
            producer="run_completion_hook",
        )
    )
    return int(admission.run_id)


async def _fanout_anchor_id(
    session: AsyncSession,
    terminal_run: AgentRunRow,
) -> int | None:
    metadata = terminal_run.metadata_ if isinstance(terminal_run.metadata_, dict) else {}
    if _is_spawned_worker(metadata) and terminal_run.parent_run_id is not None:
        return int(terminal_run.parent_run_id)
    spawned = await _spawned_workers(session, terminal_run.id)
    return int(terminal_run.id) if spawned else None


async def _lock_run(session: AsyncSession, run_id: int) -> AgentRunRow | None:
    stmt = (
        select(AgentRunRow)
        .where(AgentRunRow.id == int(run_id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one_or_none()


async def _spawned_workers(
    session: AsyncSession,
    parent_run_id: int,
) -> list[AgentRunRow]:
    rows = (
        await session.scalars(
            select(AgentRunRow)
            .where(AgentRunRow.parent_run_id == int(parent_run_id))
            .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
        )
    ).all()
    return [
        row
        for row in rows
        if _is_spawned_worker(row.metadata_ if isinstance(row.metadata_, dict) else {})
    ]


def _is_spawned_worker(metadata: Mapping[str, Any]) -> bool:
    return bool(
        metadata.get("spawned_by_tool")
        or str(metadata.get("origin") or "") == "spawn_worker"
    )


def _wants_generic_continuation(workers: list[AgentRunRow]) -> bool:
    return any(
        (worker.metadata_ if isinstance(worker.metadata_, dict) else {}).get(
            "join_parent"
        )
        is True
        for worker in workers
    )


async def _record_fanout_evidence_health(
    session: AsyncSession,
    *,
    anchor: AgentRunRow,
    workers: list[AgentRunRow],
    anchor_terminal: bool,
    all_workers_terminal: bool,
) -> None:
    await record_terminal_worker_evidence(
        session,
        parent_run_id=int(anchor.id),
        workers=workers,
        fanout_complete=anchor_terminal and all_workers_terminal,
    )


async def _resolve_chantier_scope(
    session: AsyncSession,
    anchor: AgentRunRow,
) -> ChantierScope | None:
    current: AgentRunRow | None = anchor
    seen: set[int] = set()
    while current is not None and int(current.id) not in seen:
        seen.add(int(current.id))
        scope = _scope_from_run(current)
        if scope is not None:
            return scope
        current = (
            await session.get(AgentRunRow, int(current.parent_run_id))
            if current.parent_run_id is not None
            else None
        )

    # Independent Slack/Cortex follow-up runs have no parent link. Preserve
    # the durable thread binding established by an earlier chantier run.
    thread_runs = (
        await session.scalars(
            select(AgentRunRow)
            .where(
                AgentRunRow.org_id == anchor.org_id,
                AgentRunRow.thread_id == anchor.thread_id,
                AgentRunRow.id <= anchor.id,
            )
            .order_by(AgentRunRow.id.desc())
        )
    ).all()
    for run in thread_runs:
        scope = _scope_from_run(run)
        if scope is not None:
            return scope
    return None


def _scope_from_run(run: AgentRunRow) -> ChantierScope | None:
    metadata = run.metadata_ if isinstance(run.metadata_, dict) else {}
    for key in (
        "chantier_continuation",
        "chantier_declare_guarantee",
        "chantier_declare",
    ):
        value = metadata.get(key)
        if not isinstance(value, Mapping):
            continue
        record_id = _positive_int(value.get("record_id") or value.get("chantier_record_id"))
        if record_id is None:
            record_id = _record_ref_id(value.get("record_ref"))
        if record_id is None:
            continue
        domain_id = _positive_int(value.get("domain_id") or value.get("chantier_domain_id"))
        return ChantierScope(record_id=record_id, domain_id=domain_id, source_run=run)
    return None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _record_ref_id(value: Any) -> int | None:
    prefix = "domain_record:"
    text = str(value or "").strip()
    return _positive_int(text[len(prefix) :]) if text.startswith(prefix) else None


def _continuation_target(
    source_run: AgentRunRow,
    *,
    scope: ChantierScope,
    fanout_run_id: int,
) -> dict[str, Any]:
    target = dict(source_run.target_ref or {})
    target["chantier"] = {
        "record_id": scope.record_id,
        "domain_id": scope.domain_id,
        "anchor_thread_id": source_run.thread_id,
        "completed_fanout_run_id": int(fanout_run_id),
    }
    return target


def _continuation_metadata(
    source_run: AgentRunRow,
    *,
    scope: ChantierScope,
    fanout_run_id: int,
    worker_ids: list[int],
    evidence_health: dict[str, Any],
) -> dict[str, Any]:
    source_metadata = source_run.metadata_ if isinstance(source_run.metadata_, dict) else {}
    metadata: dict[str, Any] = {
        "execution_profile": source_run.profile,
        "evidence_health": dict(evidence_health),
        "chantier_continuation": {
            "record_id": scope.record_id,
            "domain_id": scope.domain_id,
            "anchor_run_id": int(source_run.id),
            "anchor_thread_id": source_run.thread_id,
            "completed_fanout_run_id": int(fanout_run_id),
            "worker_run_ids": worker_ids,
        },
    }
    for key in (
        "slack_trigger",
        "slack_thread_id",
        "discussion_trigger",
        "originating_surface",
        "triggering_surface",
        "source_surface",
        "required_response_tool",
        "final_answer_target_surface",
    ):
        value = source_metadata.get(key)
        if value not in (None, "", {}, []):
            metadata[key] = value
    for key, value in dict(source_run.model_policy or {}).items():
        metadata.setdefault(str(key), value)
    return metadata


def _generic_continuation_target(anchor: AgentRunRow) -> dict[str, Any]:
    return {
        "kind": AGENT_RUN_CONTINUATION_TARGET,
        "thread_id": anchor.thread_id,
        "target_ref": dict(anchor.target_ref or {}),
    }


def _generic_continuation_metadata(
    anchor: AgentRunRow,
    *,
    worker_ids: list[int],
    evidence_health: dict[str, Any],
) -> dict[str, Any]:
    source_metadata = anchor.metadata_ if isinstance(anchor.metadata_, dict) else {}
    metadata: dict[str, Any] = {
        "execution_profile": anchor.profile,
        "evidence_health": dict(evidence_health),
        "worker_continuation": {
            "anchor_run_id": int(anchor.id),
            "anchor_thread_id": anchor.thread_id,
            "completed_fanout_run_id": int(anchor.id),
            "worker_run_ids": worker_ids,
        },
    }
    for key in (
        "slack_trigger",
        "slack_thread_id",
        "discussion_trigger",
        "originating_surface",
        "triggering_surface",
        "source_surface",
        "required_response_tool",
        "final_answer_target_surface",
    ):
        value = source_metadata.get(key)
        if value not in (None, "", {}, []):
            metadata[key] = value
    for key, value in dict(anchor.model_policy or {}).items():
        metadata.setdefault(str(key), value)
    return metadata


async def _child_results(
    session: AsyncSession,
    workers: list[AgentRunRow],
) -> list[dict[str, Any]]:
    outputs_by_run = await _latest_worker_outputs(
        session, [int(worker.id) for worker in workers]
    )
    results: list[dict[str, Any]] = []
    remaining_chars = _MAX_OUTPUTS_CHARS
    for worker in workers:
        output = outputs_by_run.get(int(worker.id), "")
        output = output[: min(_MAX_CHILD_OUTPUT_CHARS, remaining_chars)]
        remaining_chars = max(0, remaining_chars - len(output))
        metadata = worker.metadata_ if isinstance(worker.metadata_, dict) else {}
        results.append(
            {
                "run_id": int(worker.id),
                "status": str(worker.status),
                "role": metadata.get("worker_role") or "worker",
                "output": output,
            }
        )
    return results


async def _latest_worker_outputs(
    session: AsyncSession,
    run_ids: list[int],
) -> dict[int, str]:
    """Fetch every worker's durable output in one ordered query.

    Rows arrive newest-first, so the first artifact seen for a run is its latest;
    a FINAL_ANSWER always wins over a bare WORKER_RESULT for the same worker,
    matching the prior per-worker selection without the N+1 fan-out.
    """

    if not run_ids:
        return {}
    rows = (
        await session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id.in_(run_ids),
                AgentRunArtifactRow.artifact_type.in_(
                    (ArtifactType.FINAL_ANSWER.value, ArtifactType.WORKER_RESULT.value)
                ),
            )
            .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
        )
    ).all()
    selected: dict[int, AgentRunArtifactRow] = {}
    for row in rows:
        run_id = int(row.run_id)
        current = selected.get(run_id)
        if current is None or (
            current.artifact_type != ArtifactType.FINAL_ANSWER.value
            and row.artifact_type == ArtifactType.FINAL_ANSWER.value
        ):
            selected[run_id] = row
    return {run_id: str(row.text or "") for run_id, row in selected.items()}


def _continuation_message(
    *,
    scope: ChantierScope,
    fanout_run_id: int,
    child_results: list[dict[str, Any]],
    evidence_receipt: WorkerEvidenceReceipt,
) -> str:
    lines = [
        "Automated chantier continuation: the worker fan-out has reached its terminal barrier.",
        "No human follow-up triggered this run.",
        "",
        f"Chantier Domain record: {scope.record_id}",
        f"Anchor thread: {scope.source_run.thread_id}",
        f"Completed fan-out run: {fanout_run_id}",
        "",
        *_evidence_health_message_lines(evidence_receipt),
        "Consume every durable worker result below. Fold the evidence into the chantier record "
        "and continue the loop: decide the next step, ask any genuinely blocking questions on "
        "the anchor surface, and delegate distinct follow-up work when warranted. Do not wait "
        "for a human nudge merely because the fan-out parent already ended.",
    ]
    for result in child_results:
        lines.extend(
            [
                "",
                f"## Worker run {result['run_id']} ({result['role']}, {result['status']})",
                str(result["output"] or "[No textual artifact was recorded.]"),
            ]
        )
    return "\n".join(lines)


def _generic_continuation_message(
    *,
    anchor: AgentRunRow,
    child_results: list[dict[str, Any]],
    evidence_receipt: WorkerEvidenceReceipt,
) -> str:
    lines = [
        "Automated worker continuation: the worker fan-out has reached its terminal barrier.",
        "No human follow-up triggered this run.",
        "",
        f"Anchor thread: {anchor.thread_id}",
        f"Completed fan-out run: {anchor.id}",
        "",
        *_evidence_health_message_lines(evidence_receipt),
        "Consume every durable worker result below. Synthesize their evidence, continue the "
        "parent thread's work, and decide the next step. Ask genuinely blocking questions on "
        "the parent surface when needed. Do not wait for a human nudge merely because the "
        "fan-out parent already ended.",
    ]
    for result in child_results:
        lines.extend(
            [
                "",
                f"## Worker run {result['run_id']} ({result['role']}, {result['status']})",
                str(result["output"] or "[No textual artifact was recorded.]"),
            ]
        )
    return "\n".join(lines)


def _evidence_health_message_lines(
    evidence_receipt: WorkerEvidenceReceipt,
) -> list[str]:
    if not evidence_receipt.degraded:
        return ["Evidence health: ok; every spawned worker shard completed.", ""]
    named = ", ".join(evidence_receipt.missing_shards) or "unknown worker shard"
    return [
        f"Evidence health: degraded; missing worker shard(s): {named}.",
        "Do not report a normal sweep or infer absence from those missing shards.",
        "",
    ]


async def _existing_continuation_run_id(
    session: AsyncSession,
    fanout_run_id: int,
) -> int | None:
    key = f"chantier:continuation:{fanout_run_id}"
    return await session.scalar(
        select(AgentRunRow.id)
        .where(AgentRunRow.source_idempotency_key == key)
        .order_by(AgentRunRow.id.asc())
        .limit(1)
    )


async def _existing_generic_continuation_run_id(
    session: AsyncSession,
    fanout_run_id: int,
) -> int | None:
    key = f"worker:continuation:{fanout_run_id}"
    return await session.scalar(
        select(AgentRunRow.id)
        .where(AgentRunRow.source_idempotency_key == key)
        .order_by(AgentRunRow.id.asc())
        .limit(1)
    )


queue_worker_continuation_for_terminal_run = (
    queue_chantier_continuation_for_terminal_run
)


__all__ = [
    "CONTINUATION_QUEUED_EVENT",
    "CONTINUATION_SOURCE",
    "GENERIC_CONTINUATION_QUEUED_EVENT",
    "GENERIC_CONTINUATION_SOURCE",
    "queue_chantier_continuation_for_terminal_run",
    "queue_worker_continuation_for_terminal_run",
]
