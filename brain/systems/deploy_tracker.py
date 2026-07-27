"""Schema and persistence operations for deploy-tracker records."""

from __future__ import annotations

from typing import Mapping

from sqlalchemy import and_, func, or_, select

from brain.platform.db.models.domain import (
    Domain,
    DomainObjectType,
    DomainRecord,
)
from brain.systems.deploy_state import DeployState
from brain.systems.deploy_state_config import deploy_ticket_object_keys
from brain.systems.user_domains.service import AsyncDomainService


DEPLOY_STATE_FIELD_DEFINITIONS: tuple[dict, ...] = (
    {"key": "rollbar_item", "name": "Rollbar Item", "field_type": "text"},
    {"key": "alert_last_seen_at", "name": "Alert Last Seen At", "field_type": "datetime"},
    {"key": "alert_occurrences", "name": "Alert Occurrences", "field_type": "number"},
    {"key": "fix_pr", "name": "Fix PR", "field_type": "text"},
    {"key": "fix_merge_sha", "name": "Fix Merge SHA", "field_type": "text"},
    {"key": "fix_merged_at", "name": "Fix Merged At", "field_type": "datetime"},
    {
        "key": "deploy_state",
        "name": "Deploy State",
        "field_type": "enum",
        "options": [state.value for state in DeployState],
    },
    {"key": "deployed_at", "name": "Deployed At", "field_type": "datetime"},
    {"key": "verified_at", "name": "Verified At", "field_type": "datetime"},
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
    {
        "key": "promotion_recommended_at",
        "name": "Promotion Recommended At",
        "field_type": "datetime",
    },
)


def append_progress_note(data: Mapping[str, object], line: str) -> str | None:
    """Append one line using the tracker progress-note convention."""
    if "progress_note" not in data:
        return None
    current = str(data.get("progress_note") or "").rstrip()
    return f"{current}\n{line}".lstrip()


async def ensure_deploy_state_fields(session, *, org_id: str) -> dict:
    """Idempotently add optional deploy fields to every active ticket type."""
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
        for payload in DEPLOY_STATE_FIELD_DEFINITIONS:
            if payload["key"] in existing:
                continue
            await service.add_field_definition(object_type, dict(payload), emit_event=False)
            existing.add(str(payload["key"]))
            added += 1
    return {"object_types": len(object_types), "fields_added": added}


def _json_text(session, key: str):
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        return func.json_extract(DomainRecord.data, f"$.{key}")
    return DomainRecord.data[key].as_string()


async def list_deploy_ticket_records(
    session,
    *,
    org_id: str,
    states: set[str] | None = None,
    fix_pr_prefix: str | None = None,
    fix_pr: str | None = None,
) -> list[DomainRecord]:
    """Select ticket records by deploy-state and/or fix-PR identity.

    Selection keys on where the FIX lives (``fix_pr`` is repo-qualified), not
    on the ticket's own ``repo`` field — an app ticket fixed by a backend PR
    must be swept by the backend promotion, not the app one.
    """
    stmt = (
        select(DomainRecord)
        .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
        .join(Domain, Domain.id == DomainRecord.domain_id)
        .where(
            DomainRecord.org_id == str(org_id),
            DomainRecord.archived_at.is_(None),
            Domain.archived_at.is_(None),
            DomainObjectType.key.in_(deploy_ticket_object_keys()),
            DomainObjectType.archived_at.is_(None),
        )
    )
    conditions = []
    if states:
        state_arm = _json_text(session, "deploy_state").in_(states)
        if fix_pr_prefix:
            state_arm = and_(
                state_arm,
                _json_text(session, "fix_pr").like(f"{fix_pr_prefix}%"),
            )
        conditions.append(state_arm)
    if fix_pr:
        conditions.append(_json_text(session, "fix_pr") == fix_pr)
    if conditions:
        stmt = stmt.where(or_(*conditions))
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
