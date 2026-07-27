"""Deploy fix-reference normalization and backfill tests."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

import scripts.backfill_deploy_fix_refs as backfill
from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)
from brain.systems.deploy_fix_refs import (
    github_repo_from_issue_text,
    normalize_fix_pr_reference,
)
from brain.systems.deploy_tracker import ensure_deploy_state_fields
from brain.systems.user_domains.service import AsyncDomainService


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
REPO = "uwear-ai/uwear-backend"
NOW = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)
ALLOWED_PATCH_FIELDS = {"fix_pr", "fix_merge_sha", "progress_note"}


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_deploy_state_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._deploy_state_patch = True
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


async def _domain(session):
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Engineering",
        objects=[
            {
                "key": "github_ticket",
                "name": "GitHub Ticket",
                "title_field": "title",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text"},
                    {
                        "key": "status",
                        "field_type": "enum",
                        "options": ["Todo", "In Progress", "In Review", "Done"],
                    },
                    {"key": "progress_note", "field_type": "long_text"},
                ],
            }
        ],
    )


async def _record(session, domain, *, title, **data):
    return await AsyncDomainService(session).create_record(
        ORG_ID,
        domain.id,
        "github_ticket",
        title=title,
        data={
            "title": title,
            "repo": REPO,
            "status": "Todo",
            "progress_note": "investigating",
            **data,
        },
    )


async def _seed_backfill_records(session) -> dict[str, DomainRecord]:
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    records = {
        "canonical": await _record(
            session,
            domain,
            title="Canonical",
            deploy_state="staging",
            fix_pr=f"{REPO}#1264",
        ),
        "full_url": await _record(
            session,
            domain,
            title="Full URL",
            deploy_state="prod_pending",
            fix_pr=f"https://github.com/{REPO}/pull/1237",
        ),
        "internal_key": await _record(
            session,
            domain,
            title="Internal key",
            deploy_state="staging",
            fix_pr=f"github:{REPO}:pr:1178",
        ),
        "invalid_merge_sha": await _record(
            session,
            domain,
            title="Invalid merge SHA",
            deploy_state="staging",
            fix_pr=f"{REPO}#555",
        ),
        "bare_number": await _record(
            session,
            domain,
            title="github:uwear-ai/uwearaiapp:issue:389",
            deploy_state="prod_pending",
            fix_pr="591",
        ),
        "no_reference": await _record(
            session,
            domain,
            title="No reference",
            deploy_state="prod_pending",
            fix_pr="",
        ),
        "deployed": await _record(
            session,
            domain,
            title="Already deployed",
            deploy_state="deployed",
            fix_pr=f"https://github.com/{REPO}/pull/999",
        ),
        "archived": await _record(
            session,
            domain,
            title="Archived",
            deploy_state="staging",
            fix_pr=f"https://github.com/{REPO}/pull/777",
        ),
    }
    records["archived"].archived_at = NOW
    await session.flush()
    return records


def _github_lookup() -> AsyncMock:
    merge_shas = {
        (REPO, 1264): "a" * 40,
        (REPO, 1178): "c" * 40,
        ("uwear-ai/uwearaiapp", 591): "d" * 40,
    }

    async def github_response(repo, number):
        merge_sha = merge_shas.get((repo, number))
        if number == 555:
            return {
                "pull_request": {
                    "merged_at": NOW.isoformat(),
                    "merge_commit_sha": "not-a-merge-sha",
                }
            }
        return {
            "pull_request": {
                "merged_at": NOW.isoformat() if merge_sha else None,
                # Open PRs can expose a temporary merge SHA; never persist it.
                "merge_commit_sha": merge_sha or ("b" * 40),
            }
        }

    return AsyncMock(side_effect=github_response)


@pytest.mark.parametrize(
    ("value", "title", "expected"),
    [
        (f"{REPO}#1264", "Canonical", f"{REPO}#1264"),
        (f"https://github.com/{REPO}/pull/1237", "URL", f"{REPO}#1237"),
        (f"github:{REPO}:pr:1178", "Internal key", f"{REPO}#1178"),
        ("PR 1142", f"github:{REPO}:issue:474", f"{REPO}#1142"),
        (
            "591",
            "github:uwear-ai/uwearaiapp:issue:389",
            "uwear-ai/uwearaiapp#591",
        ),
    ],
)
async def test_normalize_fix_pr_reference_formats(value, title, expected):
    default_repo = github_repo_from_issue_text(title)
    assert (
        normalize_fix_pr_reference(value, default_repo=default_repo)
        == expected
    )


async def test_backfill_dry_run_writes_nothing(session):
    records = await _seed_backfill_records(session)
    before_data = {
        name: deepcopy(record.data)
        for name, record in records.items()
    }
    before_versions = {
        name: record.version
        for name, record in records.items()
    }

    report = await backfill.backfill_deploy_fix_refs(
        session,
        org_id=ORG_ID,
        pull_request_lookup=_github_lookup(),
    )

    assert report["applied"] is False
    assert len(report["records"]) == 7
    assert records["archived"].id not in {
        row["record_id"] for row in report["records"]
    }
    for name, record in records.items():
        await session.refresh(record)
        assert record.data == before_data[name]
        assert record.version == before_versions[name]


async def test_backfill_apply_normalizes_enriches_and_stays_in_scope(session):
    records = await _seed_backfill_records(session)
    before_data = {
        name: deepcopy(record.data)
        for name, record in records.items()
    }
    github_lookup = _github_lookup()

    report = await backfill.backfill_deploy_fix_refs(
        session,
        org_id=ORG_ID,
        apply=True,
        pull_request_lookup=github_lookup,
    )

    assert report["applied"] is True
    assert {
        row["record_id"]: row["new_fix_pr"] for row in report["records"]
    } == {
        records["canonical"].id: f"{REPO}#1264",
        records["full_url"].id: f"{REPO}#1237",
        records["internal_key"].id: f"{REPO}#1178",
        records["invalid_merge_sha"].id: f"{REPO}#555",
        records["bare_number"].id: "uwear-ai/uwearaiapp#591",
        records["no_reference"].id: None,
        records["deployed"].id: f"{REPO}#999",
    }
    rows_by_id = {
        row["record_id"]: row
        for row in report["records"]
    }
    assert rows_by_id[records["canonical"].id]["sha_found"] is True
    assert rows_by_id[records["full_url"].id]["sha_found"] is False
    assert rows_by_id[records["full_url"].id]["unresolvable_reason"] is None
    assert rows_by_id[records["invalid_merge_sha"].id]["sha_found"] is False
    assert rows_by_id[records["invalid_merge_sha"].id]["unresolvable_reason"] == (
        "merged PR has no valid merge_commit_sha"
    )
    assert rows_by_id[records["no_reference"].id]["unresolvable_reason"] == (
        "no fix PR reference"
    )
    assert rows_by_id[records["deployed"].id]["sha_found"] is False
    assert all(
        call.args != (REPO, 999)
        for call in github_lookup.await_args_list
    )

    for name, record in records.items():
        await session.refresh(record)
        changed_keys = {
            key
            for key in set(before_data[name]) | set(record.data)
            if before_data[name].get(key) != record.data.get(key)
        }
        assert changed_keys <= ALLOWED_PATCH_FIELDS
        assert record.data.get("deploy_state") == before_data[name].get(
            "deploy_state"
        )
    assert records["canonical"].data["fix_merge_sha"] == "a" * 40
    assert records["full_url"].data.get("fix_merge_sha") is None
    assert records["internal_key"].data["fix_merge_sha"] == "c" * 40
    assert records["invalid_merge_sha"].data.get("fix_merge_sha") is None
    assert records["bare_number"].data["fix_merge_sha"] == "d" * 40
    assert records["no_reference"].data["progress_note"].splitlines().count(
        "needs-human: no fix PR reference"
    ) == 1
    assert records["archived"].data == before_data["archived"]

    events = (
        await session.scalars(
            select(DomainEvent).where(DomainEvent.reason == "deploy_backfill")
        )
    ).all()
    assert len(events) == 6
    assert all(event.actor_kind == "system" for event in events)
    assert all(set(event.patch) <= ALLOWED_PATCH_FIELDS for event in events)


async def test_backfill_apply_is_idempotent(session):
    records = await _seed_backfill_records(session)
    github_lookup = _github_lookup()
    await backfill.backfill_deploy_fix_refs(
        session,
        org_id=ORG_ID,
        apply=True,
        pull_request_lookup=github_lookup,
    )
    after_first_data = {
        name: deepcopy(record.data)
        for name, record in records.items()
    }
    after_first_versions = {
        name: record.version
        for name, record in records.items()
    }

    second_report = await backfill.backfill_deploy_fix_refs(
        session,
        org_id=ORG_ID,
        apply=True,
        pull_request_lookup=github_lookup,
    )

    assert second_report["applied"] is True
    for name, record in records.items():
        await session.refresh(record)
        assert record.data == after_first_data[name]
        assert record.version == after_first_versions[name]
    assert records["no_reference"].data["progress_note"].splitlines().count(
        "needs-human: no fix PR reference"
    ) == 1
    events = (
        await session.scalars(
            select(DomainEvent).where(DomainEvent.reason == "deploy_backfill")
        )
    ).all()
    assert len(events) == 6


async def test_backfill_validates_every_plan_before_first_write(session, monkeypatch):
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    first = await _record(
        session,
        domain,
        title="First",
        deploy_state="deployed",
        fix_pr=f"https://github.com/{REPO}/pull/1",
    )
    second = await _record(
        session,
        domain,
        title="Second",
        deploy_state="deployed",
        fix_pr=f"https://github.com/{REPO}/pull/2",
    )
    before = {
        first.id: (deepcopy(first.data), first.version),
        second.id: (deepcopy(second.data), second.version),
    }
    original_planner = backfill._plan_backfill_record

    async def out_of_scope_second_plan(record, *, pull_request_lookup):
        plan = await original_planner(
            record,
            pull_request_lookup=pull_request_lookup,
        )
        if record.id == second.id:
            return replace(plan, patch={"deploy_state": "verified"})
        return plan

    monkeypatch.setattr(
        backfill,
        "_plan_backfill_record",
        out_of_scope_second_plan,
    )

    with pytest.raises(backfill.BackfillScopeError, match="escaped scope"):
        await backfill.backfill_deploy_fix_refs(
            session,
            org_id=ORG_ID,
            apply=True,
            pull_request_lookup=AsyncMock(),
        )

    for record in (first, second):
        await session.refresh(record)
        assert (record.data, record.version) == before[record.id]
    events = (
        await session.scalars(
            select(DomainEvent).where(DomainEvent.reason == "deploy_backfill")
        )
    ).all()
    assert events == []


async def test_github_lookup_uses_read_only_app_token(monkeypatch):
    resolve = AsyncMock(return_value={"GITHUB_TOKEN": "app-token"})
    get_pr = AsyncMock(return_value={"pull_request": {"merged_at": None}})
    monkeypatch.setattr(
        backfill,
        "async_resolve_project_bound_env_tokens",
        resolve,
    )
    monkeypatch.setattr(
        backfill,
        "async_get_pull_request_deploy_info",
        get_pr,
    )

    lookup = backfill.github_app_pull_request_lookup(
        org_id=ORG_ID,
        actor_user_id="22222222-2222-4222-8222-222222222222",
    )
    await lookup(REPO, 1264)
    await lookup(REPO, 1265)

    resolve.assert_awaited_once_with(
        actor_user_id="22222222-2222-4222-8222-222222222222",
        org_id=ORG_ID,
        project_slug=REPO,
        github_app_only=True,
        github_app_permissions={"pull_requests": "read"},
    )
    assert get_pr.await_args_list[0].args == (REPO, 1264)
    assert get_pr.await_args_list[0].kwargs == {"token": "app-token"}
    assert get_pr.await_args_list[1].args == (REPO, 1265)
