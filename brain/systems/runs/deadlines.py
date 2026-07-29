"""Persisted AgentRun deadline enforcement with a bounded graceful close-out."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunRow
from brain.systems.runs.domain import EventVisibility
from brain.systems.runs.events import run_event
from brain.systems.runs.failures import safe_terminal_run_message
from brain.systems.runs.status import OPEN_RUN_STATUSES, RunStatus
from brain.systems.runs.store import AsyncAgentRunStore
from brain.systems.runs.tool_event_read_model import tool_call_summary


DEADLINE_CLOSEOUT_REQUESTED_EVENT = "run.deadline_closeout_requested"
DEADLINE_EXPIRED_EVENT = "run.expired"
_STEERING_SUBMITTED_EVENT = "run.steering_submitted"
_OPEN_STATUS_VALUES = tuple(status.value for status in OPEN_RUN_STATUSES)


@dataclass(frozen=True, slots=True)
class DeadlineSweepResult:
    closeout_requested: int = 0
    expired: int = 0
    expired_run_ids: tuple[int, ...] = ()


def deadline_closeout_grace_seconds() -> int:
    try:
        return max(1, int(os.getenv("AGENT_RUN_DEADLINE_CLOSEOUT_SECONDS", "90")))
    except (TypeError, ValueError):
        return 90


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _elapsed_label(seconds: int | None) -> str:
    if seconds is None:
        return "No state-changing tool call has completed yet."
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"The last state-changing tool call completed {seconds} seconds ago."
    minutes = seconds // 60
    unit = "minute" if minutes == 1 else "minutes"
    return f"The last state-changing tool call completed {minutes} {unit} ago."


async def _request_closeout(
    session: AsyncSession,
    *,
    run_id: int,
    now: datetime,
    grace_seconds: int,
) -> bool:
    closeout_expires_at = now + timedelta(seconds=grace_seconds)
    result = await session.execute(
        update(AgentRunRow)
        .where(
            AgentRunRow.id == int(run_id),
            AgentRunRow.status.in_(_OPEN_STATUS_VALUES),
            AgentRunRow.deadline_at.is_not(None),
            AgentRunRow.deadline_at <= now,
            AgentRunRow.closeout_expires_at.is_(None),
        )
        .values(closeout_expires_at=closeout_expires_at, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False

    row = await session.get(AgentRunRow, int(run_id), populate_existing=True)
    if row is None:
        return False
    activity = await tool_call_summary(session, int(run_id), now=now)
    elapsed = activity.get("seconds_since_last_write_tool_call")
    content = (
        "[SYSTEM DEADLINE] This run has reached its wall-clock deadline. "
        "Stop gathering new information now. Persist the best partial result you have "
        "and emit the required closing output. "
        f"{_elapsed_label(elapsed)} "
        f"The close-out window ends in {grace_seconds} seconds."
    )
    store = AsyncAgentRunStore(session)
    await store.append_event(
        run_event(
            int(run_id),
            DEADLINE_CLOSEOUT_REQUESTED_EVENT,
            {
                "deadline_at": _as_utc(row.deadline_at).isoformat(),
                "closeout_expires_at": closeout_expires_at.isoformat(),
                "grace_seconds": grace_seconds,
                "seconds_since_last_state_change": elapsed,
            },
            root_run_id=row.root_run_id or int(run_id),
            producer="deadline_watchdog",
            visibility=EventVisibility.INTERNAL,
        )
    )
    await store.append_event(
        run_event(
            int(run_id),
            _STEERING_SUBMITTED_EVENT,
            {"content": content, "user_id": None, "system": True},
            root_run_id=row.root_run_id or int(run_id),
            producer="deadline_watchdog",
            visibility=EventVisibility.INTERNAL,
        )
    )
    return True


async def _expire_after_closeout(
    session: AsyncSession,
    *,
    run_id: int,
    now: datetime,
) -> bool:
    row = await session.get(AgentRunRow, int(run_id), populate_existing=True)
    if row is None or str(row.status or "") not in _OPEN_STATUS_VALUES:
        return False
    closeout_expires_at = row.closeout_expires_at
    if closeout_expires_at is None or _as_utc(closeout_expires_at) > now:
        return False
    store = AsyncAgentRunStore(session)
    expired, changed = await store.set_status_with_result(
        int(run_id),
        RunStatus.EXPIRED,
        reason="agent_run_deadline_closeout_elapsed",
        transitioned_at=now,
    )
    if not changed:
        return False
    final_output = str(safe_terminal_run_message(RunStatus.EXPIRED) or "")
    if final_output:
        artifact = await store.append_final_answer_once(
            int(run_id),
            final_output,
            root_run_id=expired.root_run_id or int(run_id),
        )
        safe_output = str(getattr(artifact, "text", None) or final_output)
        if not await store.has_event_type(int(run_id), "run.text_delta"):
            await store.append_event(
                run_event(
                    int(run_id),
                    "run.text_completed",
                    {"text": safe_output},
                    root_run_id=expired.root_run_id or int(run_id),
                    producer="deadline_watchdog",
                )
            )
    await store.append_event(
        run_event(
            int(run_id),
            DEADLINE_EXPIRED_EVENT,
            {
                "deadline_at": _as_utc(row.deadline_at).isoformat()
                if row.deadline_at is not None
                else None,
                "closeout_expires_at": _as_utc(closeout_expires_at).isoformat(),
                "status": RunStatus.EXPIRED.value,
            },
            root_run_id=expired.root_run_id or int(run_id),
            producer="deadline_watchdog",
        )
    )
    from brain.systems.runs.chantier_continuation import (
        queue_chantier_continuation_for_terminal_run,
    )

    await queue_chantier_continuation_for_terminal_run(
        session,
        terminal_run_id=int(run_id),
    )
    return True


async def sweep_agent_run_deadlines(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    grace_seconds: int | None = None,
    limit: int = 25,
) -> DeadlineSweepResult:
    """Request graceful close-out, then expire runs whose grace window elapsed."""

    resolved_now = _as_utc(now or datetime.now(timezone.utc))
    resolved_grace = max(
        1,
        int(grace_seconds if grace_seconds is not None else deadline_closeout_grace_seconds()),
    )
    candidate_ids = list(
        (
            await session.scalars(
                select(AgentRunRow.id)
                .where(
                    AgentRunRow.status.in_(_OPEN_STATUS_VALUES),
                    AgentRunRow.deadline_at.is_not(None),
                    AgentRunRow.deadline_at <= resolved_now,
                )
                .order_by(AgentRunRow.deadline_at.asc(), AgentRunRow.id.asc())
                .limit(max(1, int(limit)))
            )
        ).all()
    )
    requested = 0
    expired = 0
    expired_run_ids: list[int] = []
    for run_id in candidate_ids:
        row = await session.get(AgentRunRow, int(run_id), populate_existing=True)
        if row is None:
            continue
        if row.closeout_expires_at is None:
            requested += int(
                await _request_closeout(
                    session,
                    run_id=int(run_id),
                    now=resolved_now,
                    grace_seconds=resolved_grace,
                )
            )
            continue
        did_expire = await _expire_after_closeout(
                session,
                run_id=int(run_id),
                now=resolved_now,
            )
        expired += int(did_expire)
        if did_expire:
            expired_run_ids.append(int(run_id))
    return DeadlineSweepResult(
        closeout_requested=requested,
        expired=expired,
        expired_run_ids=tuple(expired_run_ids),
    )


__all__ = [
    "DEADLINE_CLOSEOUT_REQUESTED_EVENT",
    "DEADLINE_EXPIRED_EVENT",
    "DeadlineSweepResult",
    "deadline_closeout_grace_seconds",
    "sweep_agent_run_deadlines",
]
