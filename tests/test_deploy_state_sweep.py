"""Deploy-state persistence, verification, and safe-hook tests."""

from __future__ import annotations

import re
from copy import deepcopy
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
    AlertThreadSource,
    DEPLOY_STATE_FIELD_DEFINITIONS,
    alert_thread_source_from_run,
    ensure_deploy_state_fields,
    fix_pr_from_text,
    github_repo_from_issue_text,
    run_alert_resolution_harvest,
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


@pytest.mark.parametrize(
    ("value", "title", "expected"),
    [
        (
            "uwear-ai/uwear-backend#1264",
            "Canonical",
            "uwear-ai/uwear-backend#1264",
        ),
        (
            "https://github.com/uwear-ai/uwear-backend/pull/1237",
            "URL",
            "uwear-ai/uwear-backend#1237",
        ),
        (
            "github:uwear-ai/uwear-backend:pr:1178",
            "Internal key",
            "uwear-ai/uwear-backend#1178",
        ),
        (
            "591",
            "github:uwear-ai/uwearaiapp:issue:389",
            "uwear-ai/uwearaiapp#591",
        ),
        ("", "Nothing", None),
    ],
)
async def test_fix_pr_formats_share_one_normalizer(value, title, expected):
    repo = github_repo_from_issue_text(title)
    assert fix_pr_from_text(value, repo=repo) == expected


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


class _ReadOnlySlackClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.reads = []

    async def conversation_replies(self, *, channel, thread_ts, limit, cursor=None):
        self.reads.append(
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "limit": limit,
                "cursor": cursor,
            }
        )
        return {"messages": self.messages, "response_metadata": {"next_cursor": ""}}

    async def post_message(self, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError(f"resolution harvest attempted a Slack post: {kwargs}")


async def test_alert_thread_harvest_advances_and_cites_human_resolution_as_digest_movement(
    session, monkeypatch
):
    from brain.systems.change_notifications import render_outbound
    from brain.systems.change_notifications_cycle import _normalize_event

    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    record = await _record(
        session,
        domain,
        title="ArtDirection label bleed",
        status="In Progress",
        deploy_state="prod_pending",
    )
    slack = _ReadOnlySlackClient(
        [
            {
                "ts": "1784484952.000000",
                "bot_id": "B_RETOOL",
                "subtype": "bot_message",
                "text": "Customer alert parent",
            },
            {
                "ts": "1784490212.000000",
                "user": "U_JB",
                "text": (
                    "PR uwear-ai/uwear-backend#1142 merged; "
                    "ça devrait etre bon la c'est deploy"
                ),
            },
            {
                "ts": "1784492290.000000",
                "user": "U_REDA",
                "text": "top ca a l'air detre fix la",
            },
        ]
    )

    summary = await run_alert_resolution_harvest(
        session,
        org_id=ORG_ID,
        slack_client=slack,
        sources=[
            AlertThreadSource(
                record=record,
                channel_id="C_ALERTS",
                thread_ts="1784484952.000000",
                bot_user_id="B_ILLO",
            )
        ],
    )

    await session.refresh(record)
    assert record.data["status"] == "Done"
    assert record.data["deploy_state"] == "verified"
    assert record.data["fix_pr"] == "uwear-ai/uwear-backend#1142"
    assert record.data["deployed_at"] == datetime.fromtimestamp(
        1784490212, tz=timezone.utc
    ).isoformat()
    assert record.data["verified_at"] == datetime.fromtimestamp(
        1784492290, tz=timezone.utc
    ).isoformat()
    assert record.data["resolution_confirmed_ts"] == "1784492290.000000"
    assert record.data["alert_slack_channel"] == "C_ALERTS"
    assert record.data["alert_slack_thread_ts"] == "1784484952.000000"
    assert "Slack ts 1784492290.000000" in record.data["progress_note"]
    assert summary["verified"] == 1
    assert summary["updated"] == 1
    assert summary["movements"] == [
        {
            "record_id": record.id,
            "outcome": "verified",
            "message_ts": "1784492290.000000",
        }
    ]
    assert slack.reads == [
        {
            "channel": "C_ALERTS",
            "thread_ts": "1784484952.000000",
            "limit": 200,
            "cursor": None,
        }
    ]

    movement_event = (
        await session.scalars(
            select(DomainEvent)
            .where(
                DomainEvent.record_id == record.id,
                DomainEvent.reason.like("alert_resolution_harvest:%"),
            )
            .order_by(DomainEvent.id.desc())
        )
    ).first()
    assert movement_event is not None
    digest = render_outbound([_normalize_event(movement_event)])["digest"]
    assert digest is not None
    assert "outcome: fix verified in the alert thread" in digest
    assert "Slack ts 1784492290.000000" in digest
    assert "open hypothesis" not in digest.lower()


async def test_alert_thread_harvest_keeps_open_when_later_human_still_reproduces(
    session, monkeypatch
):
    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    record = await _record(
        session,
        domain,
        title="Generation still bleeds labels",
        status="In Progress",
        deploy_state="prod_pending",
    )
    slack = _ReadOnlySlackClient(
        [
            {
                "ts": "1784490212.000000",
                "user": "U_JB",
                "text": "c'est deploy, ça devrait etre bon",
            },
            {
                "ts": "1784492290.000000",
                "user": "U_REDA",
                "text": "top, ca a l'air d'etre fix",
            },
            {
                "ts": "1784493000.000000",
                "user": "U_AXEL",
                "text": "Still reproduces for profile 154453; the label bleed is not fixed.",
            },
        ]
    )

    summary = await run_alert_resolution_harvest(
        session,
        org_id=ORG_ID,
        slack_client=slack,
        sources=[
            AlertThreadSource(
                record=record,
                channel_id="C_ALERTS",
                thread_ts="1784484952.000000",
                bot_user_id="B_ILLO",
            )
        ],
    )

    await session.refresh(record)
    assert record.data["status"] == "Todo"
    assert record.data["deploy_state"] is None
    assert record.data["resolution_confirmed_ts"] == "1784492290.000000"
    assert record.data["resolution_reproduced_ts"] == "1784493000.000000"
    assert "Still reproduces for profile 154453" in record.data["progress_note"]
    assert "Slack ts 1784493000.000000" in record.data["progress_note"]
    assert summary["reproduced"] == 1
    assert summary["verified"] == 0
    assert summary["updated"] == 1
    assert len(slack.reads) == 1

    # Re-reading the same thread is idempotent: the older fix claims cannot
    # re-close a record after the later reproduction timestamp was persisted.
    version_after_reproduce = record.version
    replay = await run_alert_resolution_harvest(
        session,
        org_id=ORG_ID,
        slack_client=slack,
        sources=[
            AlertThreadSource(
                record=record,
                channel_id="C_ALERTS",
                thread_ts="1784484952.000000",
                bot_user_id="B_ILLO",
            )
        ],
    )
    await session.refresh(record)
    assert replay["updated"] == 0
    assert replay["skipped"] == 1
    assert record.version == version_after_reproduce
    assert record.data["status"] == "Todo"
    assert record.data["deploy_state"] is None


async def test_alert_thread_harvest_advances_deploy_claim_without_premature_done(
    session, monkeypatch
):
    monkeypatch.setenv("ILLO_DEPLOY_SWEEP_REPOS", REPO)
    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    record = await _record(
        session,
        domain,
        title="Deploy claimed, verification pending",
        status="Todo",
        deploy_state="prod_pending",
    )
    slack = _ReadOnlySlackClient(
        [
            {
                "ts": "1784490212.000000",
                "user": "U_JB",
                "text": "PR #1142 merged and deployed",
            }
        ]
    )

    summary = await run_alert_resolution_harvest(
        session,
        org_id=ORG_ID,
        slack_client=slack,
        sources=[
            AlertThreadSource(
                record=record,
                channel_id="C_ALERTS",
                thread_ts="1784484952.000000",
            )
        ],
    )

    await session.refresh(record)
    assert record.data["status"] == "In Review"
    assert record.data["deploy_state"] == "deployed"
    assert record.data["fix_pr"] == f"{REPO}#1142"
    assert record.data["resolution_confirmed_ts"] == "1784490212.000000"
    assert summary["deployed"] == 1
    assert summary["verified"] == 0


def test_alert_source_is_recovered_only_from_monitored_slack_run_provenance():
    from types import SimpleNamespace

    record = SimpleNamespace(id=1131, data={})
    monitored = SimpleNamespace(
        metadata_={"origin": "slack_channel_monitor", "slack_monitor": True},
        target_ref={
            "kind": "slack_message",
            "slack_trigger": {
                "channel_id": "C_ALERTS",
                "thread_ts": "1784484952.000000",
                "message_ts": "1784484952.000000",
                "bot_user_id": "B_ILLO",
            },
        },
    )
    ordinary = SimpleNamespace(
        metadata_={"origin": "slack_teammate"},
        target_ref=monitored.target_ref,
    )

    source = alert_thread_source_from_run(record, monitored)
    assert source is not None
    assert source.channel_id == "C_ALERTS"
    assert source.thread_ts == "1784484952.000000"
    assert alert_thread_source_from_run(record, ordinary) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("deployed", "deployed"),
        ("ça devrait etre bon la c'est deploy", "deployed"),
        ("top ca a l'air detre fix la", "verified"),
        ("PR uwear-ai/uwear-backend#1142 merged", "deployed"),
        ("still reproduces for profile 154453", "reproduced"),
        ("c'est fix mais pas deploy", None),
        ("PR #1142 is not merged yet", None),
    ],
)
def test_alert_resolution_phrase_classifier_handles_en_fr_and_unshipped_negation(
    text, expected
):
    from brain.systems.deploy_state_sweep import _resolution_kind

    assert _resolution_kind(text) == expected


async def test_deploy_fix_ref_backfill_is_dry_run_safe_scoped_and_idempotent(session):
    from scripts.backfill_deploy_fix_refs import backfill_deploy_fix_refs

    domain = await _domain(session)
    await ensure_deploy_state_fields(session, org_id=ORG_ID)
    canonical = await _record(
        session,
        domain,
        title="Canonical",
        deploy_state="staging",
        fix_pr="uwear-ai/uwear-backend#1264",
    )
    full_url = await _record(
        session,
        domain,
        title="Full URL",
        deploy_state="prod_pending",
        fix_pr="https://github.com/uwear-ai/uwear-backend/pull/1237",
    )
    internal_key = await _record(
        session,
        domain,
        title="Internal key",
        deploy_state="staging",
        fix_pr="github:uwear-ai/uwear-backend:pr:1178",
    )
    invalid_merge_sha = await _record(
        session,
        domain,
        title="Invalid merge SHA",
        deploy_state="staging",
        fix_pr="uwear-ai/uwear-backend#555",
    )
    bare_number = await _record(
        session,
        domain,
        title="github:uwear-ai/uwearaiapp:issue:389",
        deploy_state="prod_pending",
        fix_pr="591",
    )
    no_reference = await _record(
        session,
        domain,
        title="No reference",
        deploy_state="prod_pending",
        fix_pr="",
    )
    deployed = await _record(
        session,
        domain,
        title="Already deployed",
        deploy_state="deployed",
        fix_pr="https://github.com/uwear-ai/uwear-backend/pull/999",
    )
    archived = await _record(
        session,
        domain,
        title="Archived",
        deploy_state="staging",
        fix_pr="https://github.com/uwear-ai/uwear-backend/pull/777",
    )
    archived.archived_at = NOW
    await session.flush()

    records = [
        canonical,
        full_url,
        internal_key,
        invalid_merge_sha,
        bare_number,
        no_reference,
        deployed,
        archived,
    ]
    before_data = {record.id: deepcopy(record.data) for record in records}
    before_versions = {record.id: record.version for record in records}
    merge_shas = {
        ("uwear-ai/uwear-backend", 1264): "a" * 40,
        ("uwear-ai/uwear-backend", 1178): "c" * 40,
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

    github_lookup = AsyncMock(side_effect=github_response)
    dry_report = await backfill_deploy_fix_refs(
        session,
        org_id=ORG_ID,
        pull_request_lookup=github_lookup,
    )

    assert dry_report["applied"] is False
    assert len(dry_report["records"]) == 7
    assert archived.id not in {
        row["record_id"] for row in dry_report["records"]
    }
    for record in records:
        await session.refresh(record)
        assert record.data == before_data[record.id]
        assert record.version == before_versions[record.id]

    first_report = await backfill_deploy_fix_refs(
        session,
        org_id=ORG_ID,
        apply=True,
        pull_request_lookup=github_lookup,
    )
    assert first_report["applied"] is True
    assert {
        row["record_id"]: row["new_fix_pr"] for row in first_report["records"]
    } == {
        canonical.id: "uwear-ai/uwear-backend#1264",
        full_url.id: "uwear-ai/uwear-backend#1237",
        internal_key.id: "uwear-ai/uwear-backend#1178",
        invalid_merge_sha.id: "uwear-ai/uwear-backend#555",
        bare_number.id: "uwear-ai/uwearaiapp#591",
        no_reference.id: None,
        deployed.id: "uwear-ai/uwear-backend#999",
    }
    rows_by_id = {
        row["record_id"]: row for row in first_report["records"]
    }
    assert rows_by_id[canonical.id]["sha_found"] is True
    assert rows_by_id[full_url.id]["sha_found"] is False
    assert rows_by_id[full_url.id]["unresolvable_reason"] is None
    assert rows_by_id[invalid_merge_sha.id]["sha_found"] is False
    assert rows_by_id[invalid_merge_sha.id]["unresolvable_reason"] == (
        "merged PR has no valid merge_commit_sha"
    )
    assert rows_by_id[no_reference.id]["unresolvable_reason"] == (
        "no fix PR reference"
    )
    assert rows_by_id[deployed.id]["sha_found"] is False
    assert all(
        call.args != ("uwear-ai/uwear-backend", 999)
        for call in github_lookup.await_args_list
    )

    allowed_keys = {"fix_pr", "fix_merge_sha", "progress_note"}
    for record in records:
        await session.refresh(record)
        changed_keys = {
            key
            for key in set(before_data[record.id]) | set(record.data)
            if before_data[record.id].get(key) != record.data.get(key)
        }
        assert changed_keys <= allowed_keys
        assert record.data.get("deploy_state") == before_data[record.id].get(
            "deploy_state"
        )
    assert canonical.data["fix_merge_sha"] == "a" * 40
    assert full_url.data.get("fix_merge_sha") is None
    assert internal_key.data["fix_merge_sha"] == "c" * 40
    assert invalid_merge_sha.data.get("fix_merge_sha") is None
    assert bare_number.data["fix_merge_sha"] == "d" * 40
    assert no_reference.data["progress_note"].splitlines().count(
        "needs-human: no fix PR reference"
    ) == 1
    assert archived.data == before_data[archived.id]

    after_first_data = {record.id: deepcopy(record.data) for record in records}
    after_first_versions = {record.id: record.version for record in records}
    second_report = await backfill_deploy_fix_refs(
        session,
        org_id=ORG_ID,
        apply=True,
        pull_request_lookup=github_lookup,
    )
    assert second_report["applied"] is True
    for record in records:
        await session.refresh(record)
        assert record.data == after_first_data[record.id]
        assert record.version == after_first_versions[record.id]

    backfill_events = (
        await session.scalars(
            select(DomainEvent).where(DomainEvent.reason == "deploy_backfill")
        )
    ).all()
    assert len(backfill_events) == 6
    assert all(event.actor_kind == "system" for event in backfill_events)
    assert all(set(event.patch) <= allowed_keys for event in backfill_events)


async def test_deploy_fix_ref_backfill_github_lookup_uses_read_only_app_token(
    monkeypatch,
):
    import scripts.backfill_deploy_fix_refs as backfill

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
