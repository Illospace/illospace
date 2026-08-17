"""Read models for Cycle receipt-silence observation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select

from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.repositories.base import BaseRepository


@dataclass(frozen=True, slots=True)
class CycleReceiptSnapshot:
    """Persistence-neutral inputs needed by the silence policy."""

    cycle_id: int
    name: str
    executor_binding: str | None
    schedule_expr: str
    timezone: str
    receipt_monitoring_started_at: datetime | None
    created_at: datetime | None
    last_receipt_at: datetime | None


class CycleSilenceRepository(BaseRepository[Cycle]):
    """Read enabled schedules with their latest completed receipt."""

    model = Cycle

    async def list_receipt_snapshots(self) -> tuple[CycleReceiptSnapshot, ...]:
        latest_receipt = (
            select(func.max(CycleRun.completed_at))
            .where(CycleRun.cycle_id == Cycle.id)
            .correlate(Cycle)
            .scalar_subquery()
        )
        rows = (
            await self._session.execute(
                select(
                    Cycle.id,
                    Cycle.name,
                    Cycle.executor_binding,
                    Cycle.schedule_expr,
                    Cycle.timezone,
                    Cycle.receipt_monitoring_started_at,
                    Cycle.created_at,
                    latest_receipt.label("last_receipt_at"),
                )
                .where(
                    Cycle.enabled.is_(True),
                    Cycle.deleted_at.is_(None),
                )
                .order_by(Cycle.id.asc())
            )
        ).all()
        return tuple(
            CycleReceiptSnapshot(
                cycle_id=int(cycle_id),
                name=str(name),
                executor_binding=executor_binding,
                schedule_expr=str(schedule_expr),
                timezone=str(timezone_name),
                receipt_monitoring_started_at=monitoring_started_at,
                created_at=created_at,
                last_receipt_at=last_receipt_at,
            )
            for (
                cycle_id,
                name,
                executor_binding,
                schedule_expr,
                timezone_name,
                monitoring_started_at,
                created_at,
                last_receipt_at,
            ) in rows
        )
