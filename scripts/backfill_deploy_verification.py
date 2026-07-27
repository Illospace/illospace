#!/usr/bin/env python3
"""Upgrade legacy deploy verification and retire stored deploy-state fields.

The default mode is a dry run. Pass ``--apply`` to provision the canonical
fix/verification schema, convert ``deploy_state="verified"`` rows that do not
already carry an explicit boolean judgment, remove retired keys from record
JSON, and archive their field definitions.

Usage:
    venv/bin/python scripts/backfill_deploy_verification.py --org-id <org-uuid>
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
from typing import Any, TypedDict

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


class BackfillReportRow(TypedDict):
    record_id: int
    legacy_verified: bool
    verified_backfilled: bool
    retired_fields_removed: list[str]


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    record: DomainRecord
    data: dict[str, Any]
    report_row: BackfillReportRow

    @property
    def changed(self) -> bool:
        return self.data != dict(self.record.data or {})


def _plan_record(record: DomainRecord) -> BackfillPlan:
    old_data = dict(record.data or {})
    legacy_verified = (
        str(old_data.get("deploy_state") or "").strip().casefold()
        == "verified"
    )
    explicit_verification = isinstance(old_data.get("verified"), bool)
    new_data = without_retired_deploy_fields(old_data)
    verified_backfilled = legacy_verified and not explicit_verification
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


async def backfill_deploy_verification(
    session,
    *,
    org_id: str,
    apply: bool = False,
) -> dict[str, object]:
    """Plan and optionally apply the legacy verification upgrade."""
    records = await list_deploy_ticket_records(
        session,
        org_id=org_id,
        include_archived=True,
    )
    plans = [_plan_record(record) for record in records]
    retired_fields = await _active_retired_fields(session, org_id=org_id)
    changed_plans = [plan for plan in plans if plan.changed]

    schema = None
    if apply:
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
        "applied": apply,
        "updated": len(changed_plans) if apply else 0,
        "would_update": len(changed_plans),
        "fields_retired": len(retired_fields) if apply else 0,
        "fields_would_retire": len(retired_fields),
        "schema": schema,
        "records": [plan.report_row for plan in plans],
    }


async def _run(*, org_id: str, apply: bool) -> dict[str, object]:
    async with UnitOfWork() as uow:
        return await backfill_deploy_verification(
            uow.session,
            org_id=org_id,
            apply=apply,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--org-id",
        required=True,
        help="Organization UUID that owns the deploy tracker",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write proposed schema and record changes (default: dry run)",
    )
    args = parser.parse_args()
    report = asyncio.run(_run(org_id=args.org_id, apply=args.apply))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
