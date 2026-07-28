"""Schema and persistence operations for deploy-tracker records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select

from brain.platform.db.models.domain import (
    Domain,
    DomainFieldDefinition,
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

PRODUCTION_GATE_FIELD = "production_gate"
PRODUCTION_GATE_PENDING = "prod_pending"
PRODUCTION_GATE_FIELD_DEFINITIONS: tuple[dict, ...] = (
    {
        "key": PRODUCTION_GATE_FIELD,
        "name": "Production Gate",
        "field_type": "enum",
        "options": [PRODUCTION_GATE_PENDING],
    },
)


@dataclass(frozen=True, slots=True)
class DeployTrackerTransition:
    """Outcome of one canonical tracker workflow transition."""

    changed: bool
    progress_added: bool = False


def append_progress_note(data: Mapping[str, object], line: str) -> str | None:
    """Append one line using the tracker progress-note convention."""
    if "progress_note" not in data:
        return None
    current = str(data.get("progress_note") or "").rstrip()
    return f"{current}\n{line}".lstrip()


def append_progress_note_once(
    data: Mapping[str, object],
    line: str,
) -> tuple[str | None, bool]:
    """Append a progress line idempotently using the tracker convention."""

    if "progress_note" not in data:
        return None, False
    current = str(data.get("progress_note") or "").rstrip()
    if line in current:
        return current, False
    return f"{current}\n{line}".lstrip(), True


async def _ensure_fields(
    session,
    *,
    org_id: str,
    definitions: tuple[dict, ...],
    domain_id: int | None = None,
    domain_slug: str | None = None,
) -> dict:
    stmt = (
        select(DomainObjectType)
        .join(Domain, Domain.id == DomainObjectType.domain_id)
        .where(
            Domain.org_id == str(org_id),
            Domain.archived_at.is_(None),
            DomainObjectType.key.in_(deploy_ticket_object_keys()),
            DomainObjectType.archived_at.is_(None),
        )
    )
    if domain_id is not None:
        stmt = stmt.where(Domain.id == int(domain_id))
    if domain_slug is not None:
        stmt = stmt.where(Domain.slug == str(domain_slug))
    object_types = (
        await session.scalars(stmt.order_by(DomainObjectType.id))
    ).all()
    service = AsyncDomainService(session)
    added = 0
    changed = 0
    for object_type in object_types:
        existing = {
            field.key: field
            for field in (
                await session.scalars(
                    select(DomainFieldDefinition)
                    .where(
                        DomainFieldDefinition.object_type_id == object_type.id,
                        DomainFieldDefinition.key.in_(
                            definition["key"] for definition in definitions
                        ),
                    )
                    .order_by(DomainFieldDefinition.id)
                )
            ).all()
        }
        for payload in definitions:
            key = str(payload["key"])
            field = existing.get(key)
            if field is None:
                field = await service.add_field_definition(
                    object_type,
                    dict(payload),
                    emit_event=False,
                )
                existing[key] = field
                added += 1
                continue
            desired_options = list(field.options or [])
            for option in payload.get("options") or []:
                option = str(option)
                if option not in desired_options:
                    desired_options.append(option)
            desired_type = str(payload["field_type"])
            desired_required = bool(payload.get("required", False))
            desired_name = str(payload.get("name") or field.name)
            if (
                field.archived_at is not None
                or field.field_type != desired_type
                or field.required != desired_required
                or field.name != desired_name
                or list(field.options or []) != desired_options
            ):
                field.archived_at = None
                field.field_type = desired_type
                field.required = desired_required
                field.name = desired_name
                field.options = desired_options
                changed += 1
    if changed:
        await session.flush()
    return {
        "object_types": len(object_types),
        "fields_added": added,
        "fields_changed": changed,
    }


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
        "fields_changed": (
            verification["fields_changed"]
            + resolution["fields_changed"]
        ),
    }


async def ensure_production_gate_fields(
    session,
    *,
    org_id: str,
    domain_id: int | None = None,
    domain_slug: str | None = None,
) -> dict:
    """Provision or reconcile the distinct production-gate workflow field."""

    verification = await _ensure_fields(
        session,
        org_id=org_id,
        definitions=DEPLOY_VERIFICATION_FIELD_DEFINITIONS,
        domain_id=domain_id,
        domain_slug=domain_slug,
    )
    gate = await _ensure_fields(
        session,
        org_id=org_id,
        definitions=PRODUCTION_GATE_FIELD_DEFINITIONS,
        domain_id=domain_id,
        domain_slug=domain_slug,
    )
    return {
        "object_types": max(
            verification["object_types"],
            gate["object_types"],
        ),
        "fields_added": (
            verification["fields_added"]
            + gate["fields_added"]
        ),
        "fields_changed": (
            verification["fields_changed"]
            + gate["fields_changed"]
        ),
    }


async def list_deploy_ticket_records(
    session,
    *,
    org_id: str,
    include_archived: bool = False,
    domain_id: int | None = None,
    domain_slug: str | None = None,
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
    if domain_id is not None:
        stmt = stmt.where(Domain.id == int(domain_id))
    if domain_slug is not None:
        stmt = stmt.where(Domain.slug == str(domain_slug))
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


async def mark_prod_pending(
    session,
    record: DomainRecord,
    *,
    fix_pr: str,
    fix_merge_sha: str,
    progress_lines: Sequence[str],
    reason: str,
) -> DeployTrackerTransition:
    """Move a closed tracker issue back behind the production gate."""

    data = dict(record.data or {})
    progress_added = False
    note: str | None = None
    note_data: Mapping[str, object] = data
    for line in progress_lines:
        note, added = append_progress_note_once(note_data, line)
        progress_added = progress_added or added
        if note is not None:
            note_data = {**data, "progress_note": note}
    patch: dict[str, object] = {
        "status": "In Review",
        PRODUCTION_GATE_FIELD: PRODUCTION_GATE_PENDING,
        "fix_pr": fix_pr,
        "fix_merge_sha": fix_merge_sha,
    }
    if note is not None:
        patch["progress_note"] = note
    if all(data.get(key) == value for key, value in patch.items()):
        return DeployTrackerTransition(
            changed=False,
            progress_added=progress_added,
        )
    await update_deploy_ticket_record(
        session,
        record,
        patch,
        reason=reason,
    )
    return DeployTrackerTransition(
        changed=True,
        progress_added=progress_added,
    )


async def mark_deployed(
    session,
    record: DomainRecord,
    *,
    fix_pr: str,
    fix_merge_sha: str,
    reason: str,
) -> DeployTrackerTransition:
    """Complete a closed tracker issue whose fix is contained by production."""

    data = dict(record.data or {})
    patch: dict[str, object] = {
        "status": "Done",
        "fix_pr": fix_pr,
        "fix_merge_sha": fix_merge_sha,
    }
    if PRODUCTION_GATE_FIELD in data:
        patch[PRODUCTION_GATE_FIELD] = None
    if all(data.get(key) == value for key, value in patch.items()):
        return DeployTrackerTransition(changed=False)
    await update_deploy_ticket_record(
        session,
        record,
        patch,
        reason=reason,
    )
    return DeployTrackerTransition(changed=True)
