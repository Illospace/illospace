"""Schema and persistence operations for deploy-tracker records."""

from __future__ import annotations

from typing import Mapping

from sqlalchemy import select

from brain.platform.db.models.domain import (
    Domain,
    DomainObjectType,
    DomainRecord,
)
from brain.systems.deploy_record_contract import deploy_ticket_object_keys
from brain.systems.user_domains.service import AsyncDomainService


DEPLOY_VERIFICATION_FIELD_DEFINITIONS: tuple[dict, ...] = (
    {"key": "fix_pr", "name": "Fix PR", "field_type": "text"},
    {"key": "fix_merge_sha", "name": "Fix Merge SHA", "field_type": "text"},
    {"key": "verified", "name": "Verified", "field_type": "boolean"},
    {"key": "verified_at", "name": "Verified At", "field_type": "datetime"},
)

ALERT_RESOLUTION_FIELD_DEFINITIONS: tuple[dict, ...] = (
    {"key": "rollbar_item", "name": "Rollbar Item", "field_type": "text"},
    {"key": "alert_last_seen_at", "name": "Alert Last Seen At", "field_type": "datetime"},
    {"key": "alert_occurrences", "name": "Alert Occurrences", "field_type": "number"},
    {
        "key": "alert_slack_channel",
        "name": "Alert Slack Channel",
        "field_type": "text",
    },
    {
        "key": "alert_slack_thread_ts",
        "name": "Alert Slack Thread Timestamp",
        "field_type": "text",
    },
    {
        "key": "alert_slack_bot_user_id",
        "name": "Alert Slack Bot User Id",
        "field_type": "text",
    },
    {
        "key": "resolution_confirmed_ts",
        "name": "Resolution Confirming Slack Timestamp",
        "field_type": "text",
    },
    {
        "key": "resolution_reproduced_ts",
        "name": "Resolution Reproduced Slack Timestamp",
        "field_type": "text",
    },
)


def append_progress_note(data: Mapping[str, object], line: str) -> str | None:
    """Append one line using the tracker progress-note convention."""
    if "progress_note" not in data:
        return None
    current = str(data.get("progress_note") or "").rstrip()
    return f"{current}\n{line}".lstrip()


async def _ensure_fields(
    session,
    *,
    org_id: str,
    definitions: tuple[dict, ...],
) -> dict:
    object_types = (
        await session.scalars(
            select(DomainObjectType)
            .join(Domain, Domain.id == DomainObjectType.domain_id)
            .where(
                Domain.org_id == str(org_id),
                Domain.archived_at.is_(None),
                DomainObjectType.key.in_(deploy_ticket_object_keys()),
                DomainObjectType.archived_at.is_(None),
            )
            .order_by(DomainObjectType.id)
        )
    ).all()
    service = AsyncDomainService(session)
    added = 0
    for object_type in object_types:
        existing = {
            field.key
            for field in await service.list_fields(object_type.id)
        }
        for payload in definitions:
            if payload["key"] in existing:
                continue
            await service.add_field_definition(object_type, dict(payload), emit_event=False)
            existing.add(str(payload["key"]))
            added += 1
    return {"object_types": len(object_types), "fields_added": added}


async def ensure_deploy_verification_fields(session, *, org_id: str) -> dict:
    """Provision the complete fix-identity and human-verification schema."""
    return await _ensure_fields(
        session,
        org_id=org_id,
        definitions=DEPLOY_VERIFICATION_FIELD_DEFINITIONS,
    )


async def ensure_alert_resolution_fields(session, *, org_id: str) -> dict:
    """Provision the complete tracker schema used by alert harvesting."""
    verification = await ensure_deploy_verification_fields(
        session,
        org_id=org_id,
    )
    resolution = await _ensure_fields(
        session,
        org_id=org_id,
        definitions=ALERT_RESOLUTION_FIELD_DEFINITIONS,
    )
    return {
        "object_types": max(
            verification["object_types"],
            resolution["object_types"],
        ),
        "fields_added": (
            verification["fields_added"]
            + resolution["fields_added"]
        ),
    }


async def list_deploy_ticket_records(
    session,
    *,
    org_id: str,
    include_archived: bool = False,
) -> list[DomainRecord]:
    """Select tracker records for read-time enrichment or maintenance."""
    stmt = (
        select(DomainRecord)
        .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
        .join(Domain, Domain.id == DomainRecord.domain_id)
        .where(
            DomainRecord.org_id == str(org_id),
            Domain.archived_at.is_(None),
            DomainObjectType.key.in_(deploy_ticket_object_keys()),
            DomainObjectType.archived_at.is_(None),
        )
    )
    if not include_archived:
        stmt = stmt.where(DomainRecord.archived_at.is_(None))
    return list((await session.scalars(stmt.order_by(DomainRecord.id))).all())


async def update_deploy_ticket_record(
    session,
    record: DomainRecord,
    patch: dict,
    *,
    reason: str,
) -> None:
    """Apply an optimistic system-authored patch to a deploy ticket."""
    async with session.begin_nested():
        await AsyncDomainService(session).update_record(
            str(record.org_id),
            record.domain_id,
            record.id,
            data_patch=patch,
            expected_version=record.version,
            actor_kind="system",
            reason=reason,
        )
