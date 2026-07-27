"""Alert-resolution harvest tests after extraction from deploy sweeping."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
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
from brain.systems.alert_resolution import (
    AlertThreadSource,
    _resolution_kind,
    alert_thread_source_from_run,
    run_alert_resolution_harvest,
)
from brain.systems.deploy_tracker import (
    ALERT_RESOLUTION_FIELD_DEFINITIONS,
    DEPLOY_VERIFICATION_FIELD_DEFINITIONS,
    ensure_alert_resolution_fields,
)
from brain.systems.user_domains.service import AsyncDomainService


ORG_ID = "11111111-1111-4111-8111-111111111111"
REPO = "uwear-ai/uwear-backend"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_alert_resolution_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    patched._alert_resolution_patch = True
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


async def _domain(session, *, object_key="github_ticket"):
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name=f"Engineering {object_key}",
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
            "status": "In Progress",
            "progress_note": "investigating",
            **data,
        },
    )


class _ReadOnlySlackClient:
    def __init__(self, messages):
        self.messages = list(messages)
        self.reads = []

    async def conversation_replies(self, *, channel, thread_ts, limit, cursor=None):
        self.reads.append((channel, thread_ts, limit, cursor))
        return {"messages": self.messages, "response_metadata": {"next_cursor": ""}}

    async def post_message(self, **kwargs):  # pragma: no cover
        raise AssertionError(f"resolution harvest attempted a Slack post: {kwargs}")


@pytest.mark.asyncio
async def test_alert_resolution_fields_are_idempotent_and_exclude_stored_state(session):
    await _domain(session)
    first = await ensure_alert_resolution_fields(session, org_id=ORG_ID)
    second = await ensure_alert_resolution_fields(session, org_id=ORG_ID)

    assert first["fields_added"] == (
        len(DEPLOY_VERIFICATION_FIELD_DEFINITIONS)
        + len(ALERT_RESOLUTION_FIELD_DEFINITIONS)
    )
    assert second["fields_added"] == 0
    fields = (await session.scalars(select(DomainFieldDefinition))).all()
    keys = {field.key for field in fields}
    assert {
        "fix_pr",
        "fix_merge_sha",
        "verified",
        "verified_at",
    } <= keys
    assert all(field.field_type != "enum" or field.key == "status" for field in fields)


@pytest.mark.asyncio
async def test_harvest_stores_verified_overlay_and_fix_reference(session):
    domain = await _domain(session)
    record = await _record(session, domain, title="ArtDirection label bleed")
    slack = _ReadOnlySlackClient(
        [
            {
                "ts": "1784490212.000000",
                "user": "U_JB",
                "text": f"PR {REPO}#1142 merged and deployed",
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
            )
        ],
    )

    await session.refresh(record)
    assert record.data["status"] == "Done"
    assert record.data["verified"] is True
    assert record.data["verified_at"] == datetime.fromtimestamp(
        1784492290,
        tz=timezone.utc,
    ).isoformat()
    assert record.data["fix_pr"] == f"{REPO}#1142"
    assert record.data["resolution_confirmed_ts"] == "1784492290.000000"
    assert "deploy_state" not in record.data
    assert "deployed_at" not in record.data
    assert summary["verified"] == 1
    assert summary["updated"] == 1


@pytest.mark.asyncio
async def test_deployed_signal_keeps_fix_reference_without_stored_state(session):
    domain = await _domain(session)
    record = await _record(session, domain, title="Alert fix shipped")
    slack = _ReadOnlySlackClient(
        [
            {
                "ts": "1784490212.000000",
                "user": "U_JB",
                "text": f"PR {REPO}#1142 merged and deployed",
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
    assert record.data["fix_pr"] == f"{REPO}#1142"
    assert record.data["verified"] is False
    assert record.data["verified_at"] is None
    assert "deploy_state" not in record.data
    assert "deployed_at" not in record.data
    assert summary["deployed"] == 1


@pytest.mark.asyncio
async def test_harvest_rejects_whole_patch_when_schema_is_incomplete(
    session,
    monkeypatch,
):
    domain = await _domain(session)
    record = await _record(session, domain, title="Atomic resolution")
    before = dict(record.data)
    version = record.version
    monkeypatch.setattr(
        "brain.systems.alert_resolution.deploy_tracker.ensure_alert_resolution_fields",
        AsyncMock(return_value={"object_types": 1, "fields_added": 0}),
    )
    slack = _ReadOnlySlackClient(
        [
            {
                "ts": "1784492290.000000",
                "user": "U_REDA",
                "text": f"PR {REPO}#1142 looks fixed",
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
    assert record.data == before
    assert record.version == version
    assert summary["updated"] == 0
    assert summary["errors"] == [record.id]


@pytest.mark.asyncio
async def test_later_reproduction_clears_verified_overlay_and_replay_is_idempotent(session):
    domain = await _domain(session)
    await ensure_alert_resolution_fields(session, org_id=ORG_ID)
    record = await _record(
        session,
        domain,
        title="Generation still bleeds labels",
        verified=True,
        verified_at="2026-07-10T00:00:00+00:00",
    )
    slack = _ReadOnlySlackClient(
        [
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
    source = AlertThreadSource(
        record=record,
        channel_id="C_ALERTS",
        thread_ts="1784484952.000000",
    )

    summary = await run_alert_resolution_harvest(
        session,
        org_id=ORG_ID,
        slack_client=slack,
        sources=[source],
    )
    await session.refresh(record)

    assert record.data["status"] == "Todo"
    assert record.data["verified"] is False
    assert record.data["verified_at"] is None
    assert record.data["resolution_reproduced_ts"] == "1784493000.000000"
    assert summary["reproduced"] == 1

    version = record.version
    replay = await run_alert_resolution_harvest(
        session,
        org_id=ORG_ID,
        slack_client=slack,
        sources=[source],
    )
    await session.refresh(record)
    assert replay["updated"] == 0
    assert record.version == version


def test_alert_source_is_recovered_only_from_monitored_slack_run_provenance():
    record = SimpleNamespace(id=1131, data={})
    monitored = SimpleNamespace(
        metadata_={"origin": "slack_channel_monitor", "slack_monitor": True},
        target_ref={
            "slack_trigger": {
                "channel_id": "C_ALERTS",
                "thread_ts": "1784484952.000000",
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
        (f"PR {REPO}#1142 merged", "deployed"),
        ("still reproduces for profile 154453", "reproduced"),
        ("c'est fix mais pas deploy", None),
        ("PR #1142 is not merged yet", None),
    ],
)
def test_phrase_classifier_handles_en_fr_and_unshipped_negation(text, expected):
    assert _resolution_kind(text) == expected
