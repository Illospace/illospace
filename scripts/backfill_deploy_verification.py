#!/usr/bin/env python3
"""Upgrade legacy deploy verification and retire stored deploy-state fields.

The default mode and explicit ``--dry-run`` are identical. Dry runs may inspect
one organization with ``--org-id`` or every organization with an active deploy
tracker when no organization is supplied. ``--apply`` requires ``--org-id``.

The canonical ``verified`` boolean takes precedence over legacy state when both
are valid. A contradictory legacy ``deploy_state="verified"`` value is still an
anomaly: the entire apply aborts before any write so neither source of evidence
is discarded. Unknown legacy enum values, malformed overlays, and incomplete
verification timestamps also abort apply and remain visible in the report.

Usage:
    venv/bin/python scripts/backfill_deploy_verification.py --dry-run
    venv/bin/python scripts/backfill_deploy_verification.py \
        --org-id <org-uuid> --dry-run
    venv/bin/python scripts/backfill_deploy_verification.py \
        --org-id <org-uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TypedDict

from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from brain.platform.db.models.domain import (
    Domain,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.deploy_record_contract import (
    RETIRED_DEPLOY_FIELDS,
    deploy_ticket_object_keys,
    without_retired_deploy_fields,
)
from brain.systems.deploy_tracker import (
    ensure_deploy_verification_fields,
    list_deploy_ticket_records,
)


PRECEDENCE_RULE = (
    "A valid canonical verified boolean is authoritative. "
    "If legacy deploy_state='verified' disagrees, cleanup aborts until the "
    "conflict is resolved; legacy verified is backfilled only when no canonical "
    "boolean exists and verified_at is present."
)


class BackfillError(TypedDict):
    record_id: int
    field: str
    code: str
    message: str
    value: Any


class BackfillReportRow(TypedDict):
    record_id: int
    legacy_verified: bool
    verified_backfilled: bool
    retired_fields_removed: list[str]
    errors: list[BackfillError]


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    record: DomainRecord
    data: dict[str, Any]
    report_row: BackfillReportRow

    @property
    def errors(self) -> tuple[BackfillError, ...]:
        return tuple(self.report_row["errors"])

    @property
    def changed(self) -> bool:
        return self.data != dict(self.record.data or {})


def _error(
    record: DomainRecord,
    *,
    field: str,
    code: str,
    message: str,
    value: Any,
) -> BackfillError:
    return {
        "record_id": record.id,
        "field": field,
        "code": code,
        "message": message,
        "value": value,
    }


def _valid_timestamp(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _plan_record(
    record: DomainRecord,
    *,
    allowed_deploy_states: Sequence[str],
) -> BackfillPlan:
    old_data = dict(record.data or {})
    errors: list[BackfillError] = []
    deploy_state = old_data.get("deploy_state")
    clean_deploy_state = (
        deploy_state.strip()
        if isinstance(deploy_state, str)
        else deploy_state
    )
    allowed_states = set(allowed_deploy_states)
    if deploy_state is not None and (
        not isinstance(clean_deploy_state, str)
        or clean_deploy_state not in allowed_states
    ):
        errors.append(
            _error(
                record,
                field="deploy_state",
                code="unknown_deploy_state",
                message=(
                    "deploy_state is not a declared value on the legacy enum; "
                    "the row cannot be cleaned safely"
                ),
                value=deploy_state,
            )
        )
    legacy_verified = clean_deploy_state == "verified"

    verified_present = "verified" in old_data
    verified = old_data.get("verified")
    explicit_verification = isinstance(verified, bool)
    if verified_present and verified is not None and not explicit_verification:
        errors.append(
            _error(
                record,
                field="verified",
                code="malformed_verified_overlay",
                message="verified must be a boolean or null",
                value=verified,
            )
        )

    verified_at = old_data.get("verified_at")
    if verified_at is not None and not _valid_timestamp(verified_at):
        errors.append(
            _error(
                record,
                field="verified_at",
                code="malformed_verified_timestamp",
                message="verified_at must be an ISO-8601 timestamp or null",
                value=verified_at,
            )
        )
    if (legacy_verified or verified is True) and verified_at is None:
        errors.append(
            _error(
                record,
                field="verified_at",
                code="verified_without_timestamp",
                message=(
                    "verified evidence requires verified_at; no timestamp can "
                    "be inferred safely"
                ),
                value=verified_at,
            )
        )
    if legacy_verified and verified is False:
        errors.append(
            _error(
                record,
                field="verified",
                code="legacy_canonical_conflict",
                message=(
                    "canonical verified=false takes precedence but conflicts "
                    "with legacy deploy_state='verified'; resolve before cleanup"
                ),
                value=verified,
            )
        )

    for field in RETIRED_DEPLOY_FIELDS - {"deploy_state"}:
        value = old_data.get(field)
        if value is not None and not _valid_timestamp(value):
            errors.append(
                _error(
                    record,
                    field=field,
                    code="malformed_retired_timestamp",
                    message=(
                        f"{field} must be an ISO-8601 timestamp or null before "
                        "its evidence can be retired"
                    ),
                    value=value,
                )
            )

    new_data = without_retired_deploy_fields(old_data)
    verified_backfilled = (
        legacy_verified
        and not explicit_verification
        and not errors
    )
    if verified_backfilled:
        new_data["verified"] = True
    return BackfillPlan(
        record=record,
        data=new_data,
        report_row={
            "record_id": record.id,
            "legacy_verified": legacy_verified,
            "verified_backfilled": verified_backfilled,
            "retired_fields_removed": sorted(
                set(old_data) & RETIRED_DEPLOY_FIELDS
            ),
            "errors": errors,
        },
    )


async def _active_retired_fields(
    session,
    *,
    org_id: str,
) -> list[DomainFieldDefinition]:
    return list(
        (
            await session.scalars(
                select(DomainFieldDefinition)
                .join(
                    DomainObjectType,
                    DomainObjectType.id
                    == DomainFieldDefinition.object_type_id,
                )
                .join(Domain, Domain.id == DomainObjectType.domain_id)
                .where(
                    Domain.org_id == str(org_id),
                    Domain.archived_at.is_(None),
                    DomainObjectType.key.in_(deploy_ticket_object_keys()),
                    DomainObjectType.archived_at.is_(None),
                    DomainFieldDefinition.key.in_(RETIRED_DEPLOY_FIELDS),
                    DomainFieldDefinition.archived_at.is_(None),
                )
                .order_by(DomainFieldDefinition.id)
            )
        ).all()
    )


async def _deploy_tracker_org_ids(session) -> list[str]:
    return [
        str(org_id)
        for org_id in (
            await session.scalars(
                select(Domain.org_id)
                .join(
                    DomainObjectType,
                    DomainObjectType.domain_id == Domain.id,
                )
                .where(
                    Domain.archived_at.is_(None),
                    DomainObjectType.key.in_(deploy_ticket_object_keys()),
                    DomainObjectType.archived_at.is_(None),
                )
                .distinct()
                .order_by(Domain.org_id)
            )
        ).all()
    ]


async def backfill_deploy_verification(
    session,
    *,
    org_id: str,
    apply: bool = False,
) -> dict[str, object]:
    """Plan and optionally apply the legacy verification upgrade atomically."""
    records = await list_deploy_ticket_records(
        session,
        org_id=org_id,
        include_archived=True,
    )
    retired_fields = await _active_retired_fields(session, org_id=org_id)
    deploy_state_options = {
        field.object_type_id: tuple(
            str(option)
            for option in (field.options or [])
        )
        for field in retired_fields
        if field.key == "deploy_state"
    }
    plans = [
        _plan_record(
            record,
            allowed_deploy_states=deploy_state_options.get(
                record.object_type_id,
                (),
            ),
        )
        for record in records
    ]
    errors = [
        error
        for plan in plans
        for error in plan.errors
    ]
    changed_plans = [
        plan
        for plan in plans
        if plan.changed and not plan.errors
    ]
    aborted = apply and bool(errors)

    schema = None
    if apply and not aborted:
        schema = await ensure_deploy_verification_fields(
            session,
            org_id=org_id,
        )
        for plan in changed_plans:
            plan.record.data = plan.data
            plan.record.version += 1
        retired_at = datetime.now(tz=timezone.utc)
        for field in retired_fields:
            field.archived_at = retired_at
        await session.flush()

    return {
        "applied": apply and not aborted,
        "apply_requested": apply,
        "aborted": aborted,
        "precedence": PRECEDENCE_RULE,
        "error_count": len(errors),
        "errors": errors,
        "updated": len(changed_plans) if apply and not aborted else 0,
        "would_update": len(changed_plans),
        "blocked_updates": sum(
            plan.changed and bool(plan.errors)
            for plan in plans
        ),
        "fields_retired": (
            len(retired_fields)
            if apply and not aborted
            else 0
        ),
        "fields_would_retire": len(retired_fields),
        "schema": schema,
        "records": [plan.report_row for plan in plans],
    }


async def _run(
    *,
    org_id: str | None,
    apply: bool,
) -> dict[str, object]:
    async with UnitOfWork() as uow:
        if org_id is not None:
            return await backfill_deploy_verification(
                uow.session,
                org_id=org_id,
                apply=apply,
            )
        reports = [
            {
                "org_id": tracker_org_id,
                **await backfill_deploy_verification(
                    uow.session,
                    org_id=tracker_org_id,
                ),
            }
            for tracker_org_id in await _deploy_tracker_org_ids(uow.session)
        ]
        return {
            "applied": False,
            "apply_requested": False,
            "aborted": False,
            "scope": "all_deploy_tracker_organizations",
            "precedence": PRECEDENCE_RULE,
            "organization_count": len(reports),
            "error_count": sum(
                int(report["error_count"])
                for report in reports
            ),
            "errors": [
                {
                    "org_id": report["org_id"],
                    **error,
                }
                for report in reports
                for error in report["errors"]
            ],
            "would_update": sum(
                int(report["would_update"])
                for report in reports
            ),
            "blocked_updates": sum(
                int(report["blocked_updates"])
                for report in reports
            ),
            "fields_would_retire": sum(
                int(report["fields_would_retire"])
                for report in reports
            ),
            "organizations": reports,
        }


def _parse_args(
    argv: Sequence[str] | None = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org-id",
        default=None,
        help=(
            "Organization UUID that owns the deploy tracker; dry-run inspects "
            "all tracker organizations when omitted"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="Report proposed changes without writing (default)",
    )
    mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="Write proposed schema and record changes for --org-id",
    )
    parser.set_defaults(apply=False)
    args = parser.parse_args(argv)
    if args.apply and not args.org_id:
        parser.error("--apply requires --org-id")
    return args


def main() -> None:
    args = _parse_args()
    try:
        report = asyncio.run(
            _run(org_id=args.org_id, apply=args.apply)
        )
    except Exception as exc:  # noqa: BLE001
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        report = {
            "applied": False,
            "apply_requested": args.apply,
            "aborted": args.apply,
            "precedence": PRECEDENCE_RULE,
            "error_count": 1,
            "errors": [
                {
                    "code": "backfill_unavailable",
                    "exception": type(cause).__name__,
                    "message": str(cause),
                }
            ],
        }
    print(json.dumps(report, sort_keys=True))
    if int(report.get("error_count", 0)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
