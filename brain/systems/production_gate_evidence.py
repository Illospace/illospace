"""Persistence reads for production-gate evidence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import select

from brain.platform.db.models.provider_alert import ProviderAlertOccurrence
from brain.systems.production_gate_policy import ProductionEvidence


class ProductionEvidenceReader(Protocol):
    async def list_recent(
        self,
        session: Any,
        *,
        org_id: str,
        since: datetime,
        until: datetime,
    ) -> Sequence[ProductionEvidence]: ...


class StoredAlertEvidenceReader:
    """Read normalized ``#alerts`` Rollbar occurrences already persisted."""

    async def list_recent(
        self,
        session: Any,
        *,
        org_id: str,
        since: datetime,
        until: datetime,
    ) -> Sequence[ProductionEvidence]:
        rows = (
            await session.scalars(
                select(ProviderAlertOccurrence)
                .where(
                    ProviderAlertOccurrence.org_id == str(org_id),
                    ProviderAlertOccurrence.occurred_at >= since,
                    ProviderAlertOccurrence.occurred_at <= until,
                )
                .order_by(
                    ProviderAlertOccurrence.occurred_at.asc(),
                    ProviderAlertOccurrence.id.asc(),
                )
            )
        ).all()
        return tuple(
            ProductionEvidence(
                source="#alerts",
                reference=row.external_id,
                signature=row.signature_title,
                occurred_at=_utc(row.occurred_at),
            )
            for row in rows
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
