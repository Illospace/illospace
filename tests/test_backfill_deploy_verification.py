"""Legacy deploy-verification backfill tests."""

from __future__ import annotations

import re
from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

import scripts.backfill_deploy_verification as backfill
from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)
from brain.systems.deploy_record_contract import RETIRED_DEPLOY_FIELDS
from brain.systems.user_domains.service import AsyncDomainService


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
REPO = "uwear-ai/uwear-backend"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_deploy_verification_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._deploy_verification_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            Domain.__table__,
            DomainObjectType.__table__,
            DomainFieldDefinition.__table__,
            DomainRelationType.__table__,
            DomainRecord.__table__,
            DomainRelation.__table__,
            DomainEvent.__table__,
        ]
    )


async def _legacy_record(session) -> DomainRecord:
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Legacy deploy tracker",
        objects=[
            {
                "key": "github_ticket",
                "name": "GitHub Ticket",
                "title_field": "title",
                "fields": [
                    {
                        "key": "title",
                        "field_type": "text",
                        "required": True,
                    },
                    {"key": "repo", "field_type": "text"},
                    {"key": "fix_pr", "field_type": "text"},
                    {"key": "fix_merge_sha", "field_type": "text"},
                    {
                        "key": "deploy_state",
                        "field_type": "enum",
                        "options": [
                            "staging",
                            "deployed",
                            "verified",
                        ],
                    },
                    {"key": "deployed_at", "field_type": "datetime"},
                    {"key": "fix_merged_at", "field_type": "datetime"},
                    {
                        "key": "promotion_recommended_at",
                        "field_type": "datetime",
                    },
                    {"key": "verified_at", "field_type": "datetime"},
                ],
            }
        ],
    )
    return await service.create_record(
        ORG_ID,
        domain.id,
        "github_ticket",
        title="Legacy verified row",
        data={
            "title": "Legacy verified row",
            "repo": REPO,
            "fix_pr": f"{REPO}#1264",
            "fix_merge_sha": "a" * 40,
            "deploy_state": "verified",
            "deployed_at": "2026-07-26T12:00:00+00:00",
            "fix_merged_at": "2026-07-26T10:00:00+00:00",
            "promotion_recommended_at": "2026-07-26T11:00:00+00:00",
            "verified_at": "2026-07-27T12:00:00+00:00",
        },
    )


async def test_backfill_dry_run_does_not_upgrade_schema_or_record(session):
    record = await _legacy_record(session)
    before = deepcopy(record.data)
    version = record.version

    report = await backfill.backfill_deploy_verification(
        session,
        org_id=ORG_ID,
    )

    await session.refresh(record)
    fields = (await session.scalars(select(DomainFieldDefinition))).all()
    assert report["applied"] is False
    assert report["would_update"] == 1
    assert report["fields_would_retire"] == 4
    assert record.data == before
    assert record.version == version
    assert "verified" not in {field.key for field in fields}
    assert all(field.archived_at is None for field in fields)


async def test_backfill_upgrades_legacy_row_and_is_rerunnable(session):
    record = await _legacy_record(session)

    first = await backfill.backfill_deploy_verification(
        session,
        org_id=ORG_ID,
        apply=True,
    )
    await session.refresh(record)

    assert first["updated"] == 1
    assert first["fields_retired"] == 4
    assert record.data["verified"] is True
    assert record.data["verified_at"] == "2026-07-27T12:00:00+00:00"
    assert record.data["fix_pr"] == f"{REPO}#1264"
    assert record.data["fix_merge_sha"] == "a" * 40
    assert not (set(record.data) & RETIRED_DEPLOY_FIELDS)

    service = AsyncDomainService(session)
    serialized = await service.serialize_record(record)
    assert serialized["data"]["verified"] is True
    assert not (set(serialized["data"]) & RETIRED_DEPLOY_FIELDS)

    fields = (await session.scalars(select(DomainFieldDefinition))).all()
    active_keys = {
        field.key
        for field in fields
        if field.archived_at is None
    }
    retired_keys = {
        field.key
        for field in fields
        if field.archived_at is not None
    }
    assert {
        "fix_pr",
        "fix_merge_sha",
        "verified",
        "verified_at",
    } <= active_keys
    assert retired_keys == RETIRED_DEPLOY_FIELDS

    after_first = deepcopy(record.data)
    version = record.version
    second = await backfill.backfill_deploy_verification(
        session,
        org_id=ORG_ID,
        apply=True,
    )
    await session.refresh(record)

    assert second["updated"] == 0
    assert second["would_update"] == 0
    assert second["fields_retired"] == 0
    assert record.data == after_first
    assert record.version == version
