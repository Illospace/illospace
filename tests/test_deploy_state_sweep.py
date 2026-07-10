"""Deploy-state persistence, verification, and safe-hook tests."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)
from brain.systems.deploy_state_sweep import (
    DEPLOY_STATE_FIELD_DEFINITIONS,
    ensure_deploy_state_fields,
    run_deploy_sweep,
    run_deploy_verification,
)
from brain.systems.user_domains.service import AsyncDomainService


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
REPO = "uwear-ai/uwear-backend"
NOW = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)


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


async def _domain(session, *, name="Engineering", object_key="github_ticket"):
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name=name,
        objects=[
            {
                "key": object_key,
                "name": "GitHub Ticket",
                "title_field": "title",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text"},
                    {
                        "key": "status",
                        "field_type": "enum",
                        "options": ["Todo", "In Progress", "Done"],
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


async def test_ensure_fields_is_runtime_id_agnostic_and_idempotent(session):
    first_domain = await _domain(session, name="First")
    second_domain = await _domain(session, name="Second")
    assert first_domain.id != second_domain.id

    first = await ensure_deploy_state_fields(session, org_id=ORG_ID)
    second = await ensure_deploy_state_fields(session, org_id=ORG_ID)

    assert first == {"object_types": 2, "fields_added": 2 * len(DEPLOY_STATE_FIELD_DEFINITIONS)}
    assert second == {"object_types": 2, "fields_added": 0}
    fields = (await session.scalars(select(DomainFieldDefinition))).all()
    deploy_fields = [field for field in fields if field.key == "deploy_state"]
    assert len(deploy_fields) == 2
    assert all(field.options == ["staging", "prod_pending", "deployed", "verified"] for field in deploy_fields)


async def test_ensure_fields_matches_live_ticket_object_key(session, monkeypatch):
    """The deployed tracker's object key is `ticket`, not `github_ticket` —
    both must match by default, and the env override must narrow it."""
    live_shaped = await _domain(session, name="Live Tracker", object_key="ticket")
    unrelated = await _domain(session, name="Docs", object_key="doc_page")

    result = await ensure_deploy_state_fields(session, org_id=ORG_ID)
    assert result == {"object_types": 1, "fields_added": len(DEPLOY_STATE_FIELD_DEFINITIONS)}
    added = (
        await session.scalars(
            select(DomainFieldDefinition).where(DomainFieldDefinition.key == "deploy_state")
        )
    ).all()
    assert [field.domain_id for field in added] == [live_shaped.id]

    monkeypatch.setenv("ILLO_DEPLOY_TICKET_OBJECT_KEYS", "doc_page")
    narrowed = await ensure_deploy_state_fields(session, org_id=ORG_ID)
    assert narrowed["object_types"] == 1  # now the doc_page type instead
    assert unrelated.id in {
        field.domain_id
        for field in (
            await session.scalars(
                select(DomainFieldDefinition).where(DomainFieldDefinition.key == "deploy_state")
            )
        ).all()
    }


async def test_promotion_sweep_flips_only_confirmed_records(session, monkeypatch):
    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    shipped = await _record(
        session, domain, title="Shipped", deploy_state="prod_pending",
        fix_merge_sha="in-main", fix_pr=f"{REPO}#801",
    )
    post_cutoff = await _record(
        session, domain, title="Post cutoff", deploy_state="prod_pending",
        fix_merge_sha="after-cutoff", fix_pr=f"{REPO}#802",
    )
    unknown = await _record(
        session, domain, title="Unknown", deploy_state="prod_pending",
        fix_merge_sha="unknown", fix_pr=f"{REPO}#803",
    )
    staging = await _record(
        session, domain, title="Staging", deploy_state="staging",
        fix_merge_sha="on-staging", fix_pr=f"{REPO}#804",
    )
    # An app-repo ticket fixed by a PR in THIS repo: swept by fix_pr identity,
    # not the ticket's own repo field.
    cross_repo = await _record(
        session, domain, title="Cross repo", repo="uwear-ai/uwearaiapp",
        deploy_state="prod_pending", fix_merge_sha="in-main",
        fix_pr=f"{REPO}#805",
    )
    # A ticket whose fix lives in ANOTHER repo must not be touched by this
    # repo's promotion even though its own repo field matches.
    other_fix_repo = await _record(
        session, domain, title="Other fix repo", deploy_state="prod_pending",
        fix_merge_sha="in-main", fix_pr="uwear-ai/uwearaiapp#42",
    )

    async def fake_ancestry(repo, sha, branch):
        return {
            ("in-main", "main"): True,
            ("after-cutoff", "main"): False,
            ("unknown", "main"): None,
            ("on-staging", "main"): False,
            ("on-staging", "staging"): True,
        }[(sha, branch)]

    monkeypatch.setattr("brain.systems.deploy_state_sweep.is_ancestor_of", fake_ancestry)
    summary = await run_deploy_sweep(
        session,
        org_id=ORG_ID,
        repo=REPO,
        merge_event={
            "merged": True,
            "base_ref": "main",
            "head_ref": "staging",
            "number": 900,
            "merge_commit_sha": "promotion",
            "merged_at": NOW.isoformat(),
        },
    )

    await session.refresh(shipped)
    await session.refresh(post_cutoff)
    await session.refresh(unknown)
    await session.refresh(staging)
    await session.refresh(cross_repo)
    await session.refresh(other_fix_repo)
    assert shipped.data["deploy_state"] == "deployed"
    assert shipped.data["deployed_at"] == NOW.isoformat()
    assert post_cutoff.data["deploy_state"] == "prod_pending"
    assert unknown.data["deploy_state"] == "prod_pending"
    assert staging.data["deploy_state"] == "prod_pending"
    assert cross_repo.data["deploy_state"] == "deployed"
    assert other_fix_repo.data["deploy_state"] == "prod_pending"
    assert other_fix_repo.data["deployed_at"] is None
    assert summary["deployed"] == 2
    assert summary["prod_pending"] == 1
    assert summary["indeterminate"] == 1


async def test_fix_to_staging_stamps_only_matching_ticket(session, monkeypatch):
    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    matched = await _record(session, domain, title="Matched", fix_pr=f"{REPO}#905")
    other = await _record(session, domain, title="Other", fix_pr=f"{REPO}#906")

    async def not_in_main(repo, sha, branch):
        assert (repo, sha, branch) == (REPO, "fix-sha", "main")
        return False

    monkeypatch.setattr("brain.systems.deploy_state_sweep.is_ancestor_of", not_in_main)
    summary = await run_deploy_sweep(
        session,
        org_id=ORG_ID,
        repo=REPO,
        merge_event={
            "merged": True,
            "base_ref": "staging",
            "head_ref": "fix/905",
            "number": 905,
            "merge_commit_sha": "fix-sha",
            "merged_at": NOW.isoformat(),
        },
    )

    await session.refresh(matched)
    await session.refresh(other)
    assert matched.data["fix_merge_sha"] == "fix-sha"
    assert matched.data["fix_merged_at"] == NOW.isoformat()
    assert matched.data["deploy_state"] == "prod_pending"
    assert other.data["fix_merge_sha"] is None
    assert summary["updated"] == 1


async def test_env_unset_makes_sweep_inert(session, monkeypatch):
    monkeypatch.delenv("ILLO_DEPLOY_SWEEP_REPOS", raising=False)
    summary = await run_deploy_sweep(
        session,
        org_id=ORG_ID,
        repo=REPO,
        merge_event={"merged": True, "base_ref": "main", "head_ref": "staging"},
    )
    assert summary["disabled"] is True
    assert summary["examined"] == 0
    assert (await session.scalars(select(DomainFieldDefinition))).all() == []


async def test_verification_closes_only_quiet_through_window_and_never_reopens(
    session, monkeypatch
):
    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)
    monkeypatch.setenv("ILLO_DEPLOY_SETTLE_MINUTES", "30")
    monkeypatch.setenv("ILLO_DEPLOY_QUIET_HOURS", "24")
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    deployed_at = NOW - timedelta(hours=25)
    quiet = await _record(
        session,
        domain,
        title="Quiet",
        deploy_state="deployed",
        deployed_at=deployed_at.isoformat(),
    )
    before_settle = await _record(
        session,
        domain,
        title="Seen during settle",
        deploy_state="deployed",
        deployed_at=deployed_at.isoformat(),
        alert_last_seen_at=(deployed_at + timedelta(minutes=20)).isoformat(),
    )
    refired = await _record(
        session,
        domain,
        title="Refired",
        deploy_state="deployed",
        deployed_at=deployed_at.isoformat(),
        alert_last_seen_at=(deployed_at + timedelta(hours=2)).isoformat(),
    )
    too_recent = await _record(
        session,
        domain,
        title="Recent",
        deploy_state="deployed",
        deployed_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    already_verified = await _record(
        session,
        domain,
        title="Already verified",
        deploy_state="verified",
        deployed_at=(NOW - timedelta(days=2)).isoformat(),
        verified_at=(NOW - timedelta(days=1)).isoformat(),
        alert_last_seen_at=NOW.isoformat(),
        status="Done",
    )

    summary = await run_deploy_verification(session, org_id=ORG_ID, now=NOW)

    for record in (quiet, before_settle, refired, too_recent, already_verified):
        await session.refresh(record)
    assert quiet.data["deploy_state"] == "verified"
    assert quiet.data["status"] == "Done"
    assert f"verified quiet since deploy at {deployed_at.isoformat()}" in quiet.data["progress_note"]
    assert before_settle.data["deploy_state"] == "verified"
    assert refired.data["deploy_state"] == "deployed"
    assert refired.data["status"] == "Todo"
    assert too_recent.data["deploy_state"] == "deployed"
    assert already_verified.data["deploy_state"] == "verified"
    assert already_verified.data["status"] == "Done"
    assert summary["verified"] == 2
    assert summary["not_quiet"] == 1


async def test_sweep_exception_does_not_propagate_from_inbound_hook(session, monkeypatch):
    import brain.systems.deploy_state_sweep as sweep
    from brain.systems.inbound.service import _maybe_run_deploy_sweep

    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)

    async def fail(*args, **kwargs):
        raise RuntimeError("github unavailable")

    monkeypatch.setattr(sweep, "run_deploy_sweep", fail)
    result = await _maybe_run_deploy_sweep(
        session,
        org_id=ORG_ID,
        normalized={
            "kind": "github_event",
            "hints": {
                "event": "pull_request",
                "merged": True,
                "repo": REPO,
                "base_ref": "main",
                "head_ref": "staging",
            },
        },
    )
    assert result is None


async def test_inbound_hook_env_unset_is_noop_without_session(monkeypatch):
    from brain.systems.inbound.service import _maybe_run_deploy_sweep

    monkeypatch.delenv("ILLO_DEPLOY_SWEEP_REPOS", raising=False)
    result = await _maybe_run_deploy_sweep(
        object(),
        org_id=ORG_ID,
        normalized={"kind": "github_event", "hints": {"merged": True, "repo": REPO}},
    )
    assert result is None


async def test_inbound_hook_passes_selected_read_tokens_to_sweep(session, monkeypatch):
    import brain.systems.deploy_state_sweep as sweep
    import brain.systems.runs.tool_catalog.handlers.github as github_handlers
    from brain.systems.inbound.service import _maybe_run_deploy_sweep

    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)
    selected = AsyncMock(
        return_value=[
            {"token": "bound-read-token", "source": "project_binding:GITHUB_TOKEN"},
            {"token": None, "source": "public"},
        ]
    )
    run = AsyncMock(return_value={"updated": 0})
    monkeypatch.setattr(github_handlers, "_github_token_candidates", selected)
    monkeypatch.setattr(sweep, "run_deploy_sweep", run)

    await _maybe_run_deploy_sweep(
        session,
        org_id=ORG_ID,
        actor_user_id="22222222-2222-4222-8222-222222222222",
        normalized={
            "kind": "github_event",
            "hints": {
                "event": "pull_request",
                "merged": True,
                "repo": REPO,
                "base_ref": "main",
                "head_ref": "staging",
            },
        },
    )

    assert run.await_args.kwargs["ancestry_tokens"] == ["bound-read-token", None]
