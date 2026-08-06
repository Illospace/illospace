"""Read-only coordinator-digest projection for open asks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select

from brain.contracts.statuses import ACTIVE_OPEN_ASK_STATUS_VALUES, OpenAskStatus
from brain.kernel.common.time import assume_utc
from brain.platform.db.models.open_ask import ObligationKind, OpenAsk


OPEN_ASK_STRAGGLER_AFTER = timedelta(hours=1)


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
        return assume_utc(row.routed_at)
    return assume_utc(row.opened_at)


async def list_open_ask_stragglers(
    session: Any,
    *,
    org_id: str,
    now: datetime | None = None,
    older_than: timedelta = OPEN_ASK_STRAGGLER_AFTER,
) -> list[dict[str, Any]]:
    current = assume_utc(now)
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
    "list_open_ask_stragglers",
]
