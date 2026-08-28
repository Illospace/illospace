"""Incremental structural distillation for Domain records."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_CONNECTOR_BATCH_SIZE
from brain.platform.db.models.domain import Domain, DomainObjectType, DomainRecord
from brain.systems.deploy_record_contract import (
    deploy_ticket_object_keys,
    record_data_for_serialization,
)
from brain.systems.knowledge.connectors.base import (
    KnowledgeDraft,
    KnowledgeEnumeration,
    KnowledgeScope,
    UpdatedAtCursor,
)
from brain.systems.user_domains.service import AsyncDomainService

class DomainRecordsConnector:
    source_key = "domain_records"

    def __init__(self, *, max_items: int = KNOWLEDGE_CONNECTOR_BATCH_SIZE):
        self.max_items = max(1, int(max_items))

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> KnowledgeEnumeration:
        watermark = UpdatedAtCursor.from_mapping(cursor)
        stmt = (
            select(DomainRecord, Domain, DomainObjectType)
            .join(Domain, Domain.id == DomainRecord.domain_id)
            .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
            .order_by(DomainRecord.updated_at.asc(), DomainRecord.id.asc())
            .limit(self.max_items)
        )
        changed_after = watermark.changed_after(
            DomainRecord.updated_at,
            DomainRecord.id,
        )
        if changed_after is not None:
            stmt = stmt.where(changed_after)

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
            record_data = (
                record.data if isinstance(record.data, dict) else {}
            )
            serialized_data = record_data_for_serialization(
                object_type.key,
                record_data,
            )
            raw_text = str(record.search_text or "").strip()
            if object_type.key in deploy_ticket_object_keys() or not raw_text:
                # search_text is cached and can retain retired searchable fields
                # after the record data is cleaned. Rebuild tracker text from the
                # same safe data used by external serializers.
                raw_text = json.dumps(
                    serialized_data,
                    default=str,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            drafts.append(
                KnowledgeDraft(
                    source=self.source_key,
                    kind="doc_page" if object_type.key == "doc_page" else "record",
                    source_ref=f"domain_record:{record.id}",
                    scope=KnowledgeScope.ORGANIZATION,
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
            cursor=watermark.advanced_to(last_record.updated_at, last_record.id),
        )


__all__ = ["DomainRecordsConnector"]
