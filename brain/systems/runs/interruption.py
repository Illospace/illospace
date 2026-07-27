"""Application service for durable, non-terminal AgentRun interruption."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.runs.slack_delivery import (
    is_slack_origin,
    post_slack_run_message,
    run_is_headless,
)
from brain.systems.runs.store import AsyncAgentRunStore


UnitOfWork = None


def _unit_of_work_factory():
    global UnitOfWork
    if UnitOfWork is None:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork as _UnitOfWork

        UnitOfWork = _UnitOfWork
    return UnitOfWork


def _utc_datetime(value: datetime | str | None) -> datetime:
    parsed = value
    if isinstance(parsed, str):
        try:
            parsed = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
    if not isinstance(parsed, datetime):
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RunInterruption:
    """Committed interruption outcome used for post-commit notification."""

    run_id: int
    reason: str
    interrupted_at: datetime
    requeued: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", int(self.run_id))
        object.__setattr__(self, "reason", str(self.reason))
        object.__setattr__(self, "interrupted_at", _utc_datetime(self.interrupted_at))
        object.__setattr__(self, "requeued", bool(self.requeued))


def run_interruption_from_run(run: Any) -> RunInterruption:
    metadata = getattr(run, "metadata_", None)
    if not isinstance(metadata, dict):
        metadata = getattr(run, "metadata", None)
    metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
    interruption = metadata.get("interruption")
    interruption = dict(interruption) if isinstance(interruption, dict) else {}
    return RunInterruption(
        run_id=int(run.id),
        reason=str(interruption.get("reason") or "worker_shutdown"),
        interrupted_at=_utc_datetime(interruption.get("interrupted_at")),
        requeued=bool(interruption.get("requeued")),
    )


def interrupted_run_message(interruption: RunInterruption) -> str:
    """Return the public status update for a worker-interrupted run."""

    time_label = interruption.interrupted_at.strftime("%H:%M UTC")
    prefix = (
        f"I was interrupted by a system restart at {time_label} "
        f"(run {int(interruption.run_id)})"
    )
    if interruption.requeued:
        return f"{prefix}; I've re-queued it and will reply here when it finishes."
    return (
        f"{prefix}; I could not re-queue it. Work completed before the interruption "
        "was preserved, but the run did not finish."
    )


def interruption_notice_condition(interruption: RunInterruption) -> str:
    """Return the stable public condition used to collapse restart retries."""

    return (
        "interruption:requeued"
        if interruption.requeued
        else "interruption:not_requeued"
    )


async def notify_run_interruption(interruption: RunInterruption) -> dict[str, Any] | None:
    """Notify the originating surface after the interruption commit succeeds."""

    async with _unit_of_work_factory()() as uow:
        run = await uow.session.get(AgentRunRow, int(interruption.run_id))
        if run is None or not is_slack_origin(run):
            return None
        if run_is_headless(run) and run.parent_run_id is not None:
            return None
        return await post_slack_run_message(
            uow.session,
            run=run,
            text=interrupted_run_message(interruption),
            deferral_condition=interruption_notice_condition(interruption),
        )


async def interrupt_and_requeue_run(
    store: AsyncAgentRunStore,
    run_id: int,
    *,
    reason: str,
    interrupted_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> RunInterruption | None:
    """Persist one interruption inside the caller's transaction."""

    options: dict[str, Any] = {"reason": reason}
    if interrupted_at is not None:
        options["interrupted_at"] = interrupted_at
    if details is not None:
        options["details"] = details
    run, changed = await store.interrupt_and_requeue(int(run_id), **options)
    return run_interruption_from_run(run) if changed else None


async def interrupt_and_requeue_run_ids(
    run_ids: Iterable[int],
    *,
    reason: str,
    interrupted_at: datetime | None = None,
) -> tuple[RunInterruption, ...]:
    """Fence worker-owned attempts, commit their requeue, then notify."""

    occurred_at = _utc_datetime(interrupted_at)
    interruptions: list[RunInterruption] = []
    async with _unit_of_work_factory()() as uow:
        store = AsyncAgentRunStore(uow.session)
        for run_id in sorted({int(value) for value in run_ids}):
            interruption = await interrupt_and_requeue_run(
                store,
                run_id,
                reason=reason,
                interrupted_at=occurred_at,
            )
            if interruption is not None:
                interruptions.append(interruption)

    for interruption in interruptions:
        await notify_run_interruption(interruption)
    return tuple(interruptions)


__all__ = [
    "RunInterruption",
    "interrupt_and_requeue_run",
    "interrupt_and_requeue_run_ids",
    "interruption_notice_condition",
    "interrupted_run_message",
    "notify_run_interruption",
    "run_interruption_from_run",
]
