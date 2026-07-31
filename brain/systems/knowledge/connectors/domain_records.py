"""Incremental structural distillation for Domain records."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_CONNECTOR_BATCH_SIZE
from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.systems.knowledge.connectors.base import (
    KnowledgeDraft,
    KnowledgeEnumeration,
)
from brain.systems.user_domains.service import AsyncDomainService


def _cursor_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class DomainRecordsConnector:
    source_key = "domain_records"

    def __init__(self, *, max_items: int = KNOWLEDGE_CONNECTOR_BATCH_SIZE):
        self.max_items = max(1, int(max_items))

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> KnowledgeEnumeration:
        marker = _cursor_datetime(cursor.get("updated_at"))
        marker_id = max(0, int(cursor.get("id") or 0))
        stmt = (
            select(DomainRecord, Domain, DomainObjectType)
            .join(Domain, Domain.id == DomainRecord.domain_id)
            .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
            .order_by(DomainRecord.updated_at.asc(), DomainRecord.id.asc())
            .limit(self.max_items)
        )
        if marker is not None:
            stmt = stmt.where(
                or_(
                    DomainRecord.updated_at > marker,
                    and_(
                        DomainRecord.updated_at == marker,
                        DomainRecord.id > marker_id,
                    ),
                )
            )

        service = AsyncDomainService(session)
        drafts: list[KnowledgeDraft] = []
        rows = list((await session.execute(stmt)).all())
        for record, domain, object_type in rows:
            compact = await service.serialize_record_compact(record)
            summary = json.dumps(
                compact,
                default=str,
                ensure_ascii=False,
                sort_keys=True,
            )
            raw_text = str(record.search_text or "").strip()
            if not raw_text:
                raw_text = json.dumps(
                    record.data if isinstance(record.data, dict) else {},
                    default=str,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            drafts.append(
                KnowledgeDraft(
                    source=self.source_key,
                    kind="doc_page" if object_type.key == "doc_page" else "record",
                    source_ref=f"domain_record:{record.id}",
                    title=record.title,
                    summary=summary,
                    entities=[domain.slug, object_type.key],
                    raw_text=raw_text,
                    extra={
                        "org_id": str(record.org_id),
                        "domain_id": record.domain_id,
                        "domain_slug": domain.slug,
                        "object_type_id": record.object_type_id,
                        "object_type": object_type.key,
                        "version": record.version,
                    },
                    source_created_at=record.created_at,
                    source_updated_at=record.updated_at,
                    archived_at=record.archived_at,
                )
            )

        if not rows:
            return KnowledgeEnumeration(drafts=drafts, cursor=dict(cursor))
        last_record = rows[-1][0]
        return KnowledgeEnumeration(
            drafts=drafts,
            cursor={
                "updated_at": _utc_iso(last_record.updated_at),
                "id": last_record.id,
            },
        )


__all__ = ["DomainRecordsConnector"]
