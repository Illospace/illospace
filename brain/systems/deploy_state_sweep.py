"""Deterministic persistence wiring for the deploy-state lifecycle.

The pure axis and ladder live in :mod:`brain.systems.deploy_state`.  This module
owns best-effort reads of GitHub-ticket records plus optimistic writes through
``AsyncDomainService``.  It never posts to Slack and never lets GitHub or record
conflicts fail webhook ingestion / notification ticks.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Awaitable, Mapping, Protocol, Sequence

from sqlalchemy import func, or_, select

from brain.platform.db.models.domain import (
    Domain,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
)
from brain.systems.deploy_state import (
    DeployState,
    MergeKind,
    as_utc_datetime,
    classify_merge_event,
    derive_deploy_state,
)
from brain.systems.deploy_state_config import (
    deploy_feature_enabled,
    deploy_quiet_window,
    deploy_settle_window,
)
from brain.systems.deploy_state_github import is_ancestor_of
from brain.systems.user_domains.service import AsyncDomainService


logger = logging.getLogger("illo.deploy_state")

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
        "key": "promotion_recommended_at",
        "name": "Promotion Recommended At",
        "field_type": "datetime",
    },
)


class QuietCheck(Protocol):
    """Replaceable quiet-check seam; a future Rollbar implementation fits here."""

    def __call__(
        self,
        *,
        data: Mapping[str, object],
        deployed_at: datetime,
        settle_until: datetime,
        now: datetime,
    ) -> bool | None | Awaitable[bool | None]: ...


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime | str):
        return None
    return as_utc_datetime(value)


def _iso(value: datetime | str | object) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed else None


def _append_progress_note(data: Mapping[str, object], line: str) -> str | None:
    if "progress_note" not in data:
        return None
    current = str(data.get("progress_note") or "").rstrip()
    return f"{current}\n{line}".lstrip()


async def ensure_deploy_state_fields(session, *, org_id: str) -> dict:
    """Idempotently add optional deploy fields to every active github_ticket type."""
    object_types = (
        await session.scalars(
            select(DomainObjectType)
            .join(Domain, Domain.id == DomainObjectType.domain_id)
            .where(
                Domain.org_id == str(org_id),
                Domain.archived_at.is_(None),
                DomainObjectType.key == "github_ticket",
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


async def _ticket_records(
    session,
    *,
    org_id: str,
    repo: str | None = None,
    states: set[str] | None = None,
    fix_pr: str | None = None,
) -> list[DomainRecord]:
    stmt = (
        select(DomainRecord)
        .join(DomainObjectType, DomainObjectType.id == DomainRecord.object_type_id)
        .join(Domain, Domain.id == DomainRecord.domain_id)
        .where(
            DomainRecord.org_id == str(org_id),
            DomainRecord.archived_at.is_(None),
            Domain.archived_at.is_(None),
            DomainObjectType.key == "github_ticket",
            DomainObjectType.archived_at.is_(None),
        )
    )
    if repo is not None:
        stmt = stmt.where(_json_text(session, "repo") == repo)
    conditions = []
    if states:
        conditions.append(_json_text(session, "deploy_state").in_(states))
    if fix_pr:
        conditions.append(_json_text(session, "fix_pr") == fix_pr)
    if conditions:
        stmt = stmt.where(or_(*conditions))
    return list((await session.scalars(stmt.order_by(DomainRecord.id))).all())


async def _update_record(
    session,
    record: DomainRecord,
    patch: dict,
    *,
    reason: str,
) -> None:
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


async def _check_ancestry(
    repo: str,
    sha: str,
    branch: str,
    tokens: Sequence[str | None],
) -> bool | None:
    """Try ordered read identities until ancestry is determinate."""
    for token in tokens:
        if token is None:
            result = await is_ancestor_of(repo, sha, branch)
        else:
            result = await is_ancestor_of(repo, sha, branch, token=token)
        if result is not None:
            return result
    return None


def _new_summary(kind: MergeKind | str) -> dict:
    return {
        "merge_kind": str(kind),
        "examined": 0,
        "updated": 0,
        "deployed": 0,
        "prod_pending": 0,
        "staging": 0,
        "indeterminate": 0,
        "skipped": 0,
        "errors": [],
    }


async def _sweep_main_merge(
    session,
    *,
    org_id: str,
    repo: str,
    event: Mapping,
    ancestry_tokens: Sequence[str | None],
    summary: dict,
) -> None:
    kind = classify_merge_event(event)
    fix_pr = f"{repo}#{event.get('number')}" if kind is MergeKind.HOTFIX and event.get("number") else None
    records = await _ticket_records(
        session,
        org_id=org_id,
        repo=repo,
        states={DeployState.STAGING.value, DeployState.PROD_PENDING.value},
        fix_pr=fix_pr,
    )
    for record in records:
        summary["examined"] += 1
        data = record.data or {}
        sha = str(data.get("fix_merge_sha") or "").strip()
        if fix_pr and data.get("fix_pr") == fix_pr and event.get("merge_commit_sha"):
            sha = str(event["merge_commit_sha"])
        if not sha:
            summary["skipped"] += 1
            continue
        in_main = await _check_ancestry(repo, sha, "main", ancestry_tokens)
        if in_main is None:
            summary["indeterminate"] += 1
            continue
        patch: dict = {}
        if in_main:
            deployed_at = _iso(event.get("merged_at"))
            if not deployed_at:
                summary["skipped"] += 1
                continue
            patch.update({"deploy_state": DeployState.DEPLOYED.value, "deployed_at": deployed_at})
            if fix_pr and data.get("fix_pr") == fix_pr:
                patch.update({"fix_merge_sha": sha, "fix_merged_at": deployed_at})
            note = _append_progress_note(data, f"deployed to main at {deployed_at}")
            if note is not None:
                patch["progress_note"] = note
            bucket = "deployed"
        elif data.get("deploy_state") == DeployState.STAGING.value:
            in_staging = await _check_ancestry(repo, sha, "staging", ancestry_tokens)
            if in_staging is None:
                summary["indeterminate"] += 1
                continue
            if not in_staging:
                summary["skipped"] += 1
                continue
            patch["deploy_state"] = DeployState.PROD_PENDING.value
            note = _append_progress_note(data, "fix confirmed on staging; awaiting promotion")
            if note is not None:
                patch["progress_note"] = note
            bucket = "prod_pending"
        else:
            summary["skipped"] += 1
            continue
        try:
            await _update_record(session, record, patch, reason=f"deploy_sweep:{kind.value}")
        except Exception as exc:
            logger.warning("deploy sweep skipped record %s: %s", record.id, exc)
            summary["errors"].append(record.id)
            continue
        summary["updated"] += 1
        summary[bucket] += 1


async def _sweep_staging_merge(
    session,
    *,
    org_id: str,
    repo: str,
    event: Mapping,
    ancestry_tokens: Sequence[str | None],
    summary: dict,
) -> None:
    number = event.get("number")
    sha = str(event.get("merge_commit_sha") or "").strip()
    merged_at = _iso(event.get("merged_at"))
    if not number or not sha or not merged_at:
        summary["skipped"] += 1
        return
    fix_pr = f"{repo}#{number}"
    records = await _ticket_records(session, org_id=org_id, fix_pr=fix_pr)
    for record in records:
        summary["examined"] += 1
        data = record.data or {}
        in_main = await _check_ancestry(repo, sha, "main", ancestry_tokens)
        state = derive_deploy_state(
            merged=True,
            base_ref="staging",
            in_staging=True,
            in_main=in_main,
        )
        if state is None:
            state = DeployState.STAGING
        patch = {
            "fix_merge_sha": sha,
            "fix_merged_at": merged_at,
            "deploy_state": state.value,
        }
        if state is DeployState.DEPLOYED:
            patch["deployed_at"] = merged_at
        note = _append_progress_note(data, f"fix {fix_pr} merged to staging at {merged_at}")
        if note is not None:
            patch["progress_note"] = note
        try:
            await _update_record(session, record, patch, reason="deploy_sweep:fix_to_staging")
        except Exception as exc:
            logger.warning("deploy sweep skipped record %s: %s", record.id, exc)
            summary["errors"].append(record.id)
            continue
        summary["updated"] += 1
        summary[state.value] += 1
        if in_main is None:
            summary["indeterminate"] += 1


async def run_deploy_sweep(
    session,
    *,
    org_id: str,
    repo: str,
    merge_event: Mapping,
    ancestry_tokens: Sequence[str | None] | None = None,
) -> dict:
    """Apply one merged-PR event, returning telemetry and never raising."""
    kind = classify_merge_event(merge_event)
    summary = _new_summary(kind)
    if not deploy_feature_enabled(repo):
        summary["disabled"] = True
        return summary
    try:
        tokens = tuple(ancestry_tokens or (None,))
        summary["fields"] = await ensure_deploy_state_fields(session, org_id=org_id)
        if kind in {MergeKind.PROMOTION, MergeKind.HOTFIX}:
            await _sweep_main_merge(
                session,
                org_id=org_id,
                repo=repo,
                event=merge_event,
                ancestry_tokens=tokens,
                summary=summary,
            )
        elif kind is MergeKind.FIX_TO_STAGING:
            await _sweep_staging_merge(
                session,
                org_id=org_id,
                repo=repo,
                event=merge_event,
                ancestry_tokens=tokens,
                summary=summary,
            )
    except Exception as exc:
        logger.exception("deploy sweep failed safely for %s", repo)
        summary["errors"].append(str(exc))
    return summary


def infer_quiet_from_record(
    *,
    data: Mapping[str, object],
    deployed_at: datetime,
    settle_until: datetime,
    now: datetime,
) -> bool | None:
    """Default quiet check: infer from the last alert occurrence we ingested."""
    raw_last_seen = data.get("alert_last_seen_at")
    if raw_last_seen in {None, ""}:
        return True
    last_seen = _parse_datetime(raw_last_seen)
    if last_seen is None:
        return None
    return last_seen <= settle_until


async def _check_quiet(check: QuietCheck, **kwargs) -> bool | None:
    result = check(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def run_deploy_verification(
    session,
    *,
    org_id: str,
    now: datetime,
    quiet_check: QuietCheck = infer_quiet_from_record,
) -> dict:
    """Close only deployed tickets that stayed quiet through the full window."""
    summary = {
        "examined": 0,
        "eligible": 0,
        "verified": 0,
        "not_quiet": 0,
        "indeterminate": 0,
        "skipped": 0,
        "errors": [],
    }
    if not deploy_feature_enabled():
        summary["disabled"] = True
        return summary
    try:
        summary["fields"] = await ensure_deploy_state_fields(session, org_id=org_id)
        records = await _ticket_records(
            session,
            org_id=org_id,
            states={DeployState.DEPLOYED.value},
        )
    except Exception as exc:
        logger.exception("deploy verification setup failed safely")
        summary["errors"].append(str(exc))
        return summary

    current = _parse_datetime(now)
    if current is None:
        summary["errors"].append("invalid now")
        return summary
    settle = deploy_settle_window()
    quiet_window = deploy_quiet_window()
    for record in records:
        summary["examined"] += 1
        data = record.data or {}
        deployed_at = _parse_datetime(data.get("deployed_at"))
        if deployed_at is None:
            summary["skipped"] += 1
            continue
        settle_until = deployed_at + settle
        if current < settle_until + quiet_window:
            summary["skipped"] += 1
            continue
        summary["eligible"] += 1
        try:
            quiet = await _check_quiet(
                quiet_check,
                data=data,
                deployed_at=deployed_at,
                settle_until=settle_until,
                now=current,
            )
        except Exception as exc:
            logger.warning("quiet check failed for record %s: %s", record.id, exc)
            summary["indeterminate"] += 1
            continue
        if quiet is None:
            summary["indeterminate"] += 1
            continue
        if quiet is False:
            summary["not_quiet"] += 1
            continue
        deployed_iso = deployed_at.isoformat()
        patch = {
            "deploy_state": DeployState.VERIFIED.value,
            "verified_at": current.isoformat(),
            "status": "Done",
        }
        note = _append_progress_note(
            data,
            f"verified quiet since deploy at {deployed_iso}",
        )
        if note is not None:
            patch["progress_note"] = note
        try:
            await _update_record(session, record, patch, reason="deploy_verification")
        except Exception as exc:
            logger.warning("deploy verification skipped record %s: %s", record.id, exc)
            summary["errors"].append(record.id)
            continue
        summary["verified"] += 1
    return summary
