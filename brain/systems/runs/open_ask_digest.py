"""Expiry policy and coordinator-digest projection for open asks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select

from brain.contracts.statuses import (
    ACTIVE_OPEN_ASK_STATUS_VALUES,
    AGENT_RUN_DB_STATUS_VALUES,
    TERMINAL_RUN_STATUS_VALUES,
    OpenAskStatus,
    project_run_status_value,
)
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.open_ask import ObligationKind, OpenAsk
from brain.systems.runs.open_ask_settlement import _supersede_pending_notices


OPEN_ASK_STRAGGLER_AFTER = timedelta(hours=1)
# Three days is well below the observed 146-176h failures while allowing recovery.
RUN_DEFERRAL_EXPIRY_AFTER = timedelta(hours=72)
_TERMINAL_ORIGIN_RUN_STATUS_VALUES = tuple(
    status
    for status in AGENT_RUN_DB_STATUS_VALUES
    if project_run_status_value(status) in TERMINAL_RUN_STATUS_VALUES
)


def _aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _age_label(age: timedelta) -> str:
    total_minutes = max(0, int(age.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _open_ask_age_started_at(row: OpenAsk) -> datetime:
    if row.status == OpenAskStatus.ROUTED.value and row.routed_at is not None:
        return _aware_utc(row.routed_at)
    return _aware_utc(row.opened_at)


async def expire_stale_run_deferrals(
    session: Any,
    *,
    org_id: str,
    now: datetime | None = None,
    expiry_after: timedelta = RUN_DEFERRAL_EXPIRY_AFTER,
) -> int:
    """Expire old run promises only after their originating run is terminal."""

    current = _aware_utc(now)
    cutoff = current - expiry_after
    results = list(
        (
            await session.execute(
                select(OpenAsk, AgentRunRow.status)
                .join(AgentRunRow, AgentRunRow.id == OpenAsk.origin_run_id)
                .where(
                    OpenAsk.org_id == str(org_id),
                    OpenAsk.obligation_kind == ObligationKind.RUN_DEFERRAL,
                    OpenAsk.status == OpenAskStatus.OPEN.value,
                    OpenAsk.opened_at < cutoff,
                    AgentRunRow.status.in_(_TERMINAL_ORIGIN_RUN_STATUS_VALUES),
                )
                .order_by(OpenAsk.id.asc())
                .with_for_update(of=OpenAsk)
            )
        ).all()
    )
    expiry_hours = int(expiry_after.total_seconds() // 3600)
    for row, run_status in results:
        row.status = OpenAskStatus.EXPIRED.value
        row.expired_at = current
        row.status_reason = (
            f"Origin run {int(row.origin_run_id)} is terminal ({str(run_status)}); "
            f"run deferral expired after {expiry_hours}h."
        )
    rows = [row for row, _run_status in results]
    await _supersede_pending_notices(session, [int(row.id) for row in rows])
    if rows:
        await session.flush()
    return len(rows)


async def list_open_ask_stragglers(
    session: Any,
    *,
    org_id: str,
    now: datetime | None = None,
    older_than: timedelta = OPEN_ASK_STRAGGLER_AFTER,
) -> list[dict[str, Any]]:
    current = _aware_utc(now)
    cutoff = current - older_than
    rows = list(
        (
            await session.scalars(
                select(OpenAsk)
                .where(
                    OpenAsk.org_id == str(org_id),
                    OpenAsk.status.in_(ACTIVE_OPEN_ASK_STATUS_VALUES),
                    or_(
                        and_(
                            OpenAsk.status == OpenAskStatus.OPEN.value,
                            OpenAsk.opened_at < cutoff,
                        ),
                        and_(
                            OpenAsk.status == OpenAskStatus.ROUTED.value,
                            func.coalesce(OpenAsk.routed_at, OpenAsk.opened_at)
                            < cutoff,
                        ),
                    ),
                )
                .order_by(
                    OpenAsk.obligation_kind.asc(),
                    OpenAsk.opened_at.asc(),
                    OpenAsk.id.asc(),
                )
            )
        ).all()
    )
    return [
        {
            "id": row.id,
            "status": row.status,
            "obligation_kind": row.obligation_kind,
            "owner_label": row.owner_label,
            "routed_to_name": row.routed_to_name,
            "routed_to_slack_id": row.routed_to_slack_id,
            "requester_name": (
                row.owner_label
                if row.obligation_kind == ObligationKind.HUMAN_ASK
                else None
            ),
            "requester_slack_id": row.requester_slack_id,
            "ask_text": row.ask_text,
            "origin_ref": row.origin_ref,
            "age": _age_label(current - _open_ask_age_started_at(row)),
            "age_seconds": max(
                0,
                int((current - _open_ask_age_started_at(row)).total_seconds()),
            ),
            "thread_permalink": row.thread_permalink,
        }
        for row in rows
    ]


__all__ = [
    "OPEN_ASK_STRAGGLER_AFTER",
    "RUN_DEFERRAL_EXPIRY_AFTER",
    "expire_stale_run_deferrals",
    "list_open_ask_stragglers",
]
