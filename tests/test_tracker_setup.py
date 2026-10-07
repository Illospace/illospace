from __future__ import annotations

import pytest
from sqlalchemy import select

from brain.platform.db.models.domain import DomainRecord
from brain.platform.db.models.inbound import InboundDomainProjectionRow
from brain.systems.inbound import admin, service as inbound
from brain.systems.inbound.github_webhook import github_event_to_envelope
from brain.systems.inbound.tracker_setup import configure_github_tracker
from brain.systems.inbound.tracker_recovery import recover_tracker_snapshot
from brain.systems import deploy_tracker
from brain.systems.user_domains.service import AsyncDomainService
from tests.inbound_admin_support import (
    ORG_ID, USER_ID, _bridge_connection,
    seeded_session as seeded_session, session as session,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def tracker_config(seeded_session, request):
    service = AsyncDomainService(seeded_session)
    identity = [
        {"key": "external_id", "field_type": "text"}, {"key": "repo", "field_type": "text"},
        {"key": "number", "field_type": "number"}, {"key": "url", "field_type": "url"},
    ]
    tracker = await service.create_domain(
        ORG_ID, name="GitHub Ticket Tracker", slug="github-ticket-tracker", objects=[
            {"key": "ticket", "fields": [*identity,
                {"key": "github_state", "field_type": "enum", "options": ["open", "closed"]},
                {"key": "status", "field_type": "enum", "options": ["Backlog", "In Progress", "In Review", "Done"]},
                {"key": "created_at", "field_type": "datetime"}, {"key": "updated_at", "field_type": "datetime"},
                {"key": "closed_at", "field_type": "datetime"}, {"key": "synced_at", "field_type": "datetime"},
                {"key": "body", "field_type": "long_text"}, {"key": "assignee", "field_type": "text"},
            ]},
            {"key": "pull_request", "fields": [*identity,
                {"key": "state", "field_type": "enum", "options": ["open", "closed", "merged", "draft"]},
                {"key": "author", "field_type": "text"}, {"key": "updated_at", "field_type": "datetime"},
                *([{"key": "merged", "field_type": "boolean"}] if getattr(request, "param", True) else []),
            ]},
        ],
    )
    feed_domain = await service.create_domain(ORG_ID, name="GitHub Events", objects=[{
        "key": "github_event", "fields": [{"key": "external_id", "field_type": "text"}],
    }])
    connection = await admin.create_connection(
        seeded_session, org_id=ORG_ID, owner_user_id=USER_ID,
        display_name="GitHub", agent_kind="github", transport="webhook",
    )
    policy = await admin.create_policy(
        seeded_session, org_id=ORG_ID, connection_id=connection.id,
        name="GitHub feed", origin_patterns=["github:*"], envelope_kinds=["github_event"],
    )
    feed = await admin.create_projection(
        seeded_session, org_id=ORG_ID, connection_id=connection.id, policy_id=policy.id,
        domain_id=feed_domain.id, object_key="github_event", external_id_path="hints.node_id",
        external_id_field="external_id", field_mapping={}, title_path="summary",
    )
    return tracker, feed, connection


async def test_additive_setup_is_dry_run_by_default_and_idempotent(seeded_session, tracker_config):
    tracker, feed, _connection = tracker_config
    before = admin.serialize_projection(feed)
    plan = await configure_github_tracker(seeded_session, org_id=ORG_ID)
    assert plan["applied"] is False and all(item["projection_id"] is None for item in plan["projections"])
    assert len(list(await seeded_session.scalars(select(InboundDomainProjectionRow)))) == 1
    applied = await configure_github_tracker(seeded_session, org_id=ORG_ID, apply=True)
    repeated = await configure_github_tracker(seeded_session, org_id=ORG_ID, apply=True)
    assert all(item["operation"] == "existing" for item in repeated["projections"])
    assert [row["projection_id"] for row in repeated["projections"]] == [row["projection_id"] for row in applied["projections"]]
    assert admin.serialize_projection(feed) == before
    assert applied["tracker_domain_id"] == tracker.id


@pytest.mark.parametrize("tracker_config", [False, True], indirect=True)
@pytest.mark.parametrize("state, merged, draft, expected", [
    ("open", False, False, "open"), ("open", False, True, "draft"),
    ("closed", False, False, "closed"), ("closed", True, False, "merged"),
])
async def test_setup_projects_pr_state_with_original_and_extended_schema(
    seeded_session, tracker_config, state, merged, draft, expected,
):
    tracker, feed, connection = tracker_config
    setup = await configure_github_tracker(seeded_session, org_id=ORG_ID, apply=True)
    projection_id = next(row["projection_id"] for row in setup["projections"] if row["object_key"] == "pull_request")
    projection = await seeded_session.get(InboundDomainProjectionRow, projection_id)
    envelope = github_event_to_envelope("pull_request", {
        "repository": {"full_name": "owner/repo"}, "action": "snapshot", "pull_request": {
            "node_id": "pr-node", "number": 1, "title": "PR title", "state": state,
            "merged": merged, "draft": draft, "html_url": "https://github.com/owner/repo/pull/1",
            "user": {"login": "original-author"}, "updated_at": "2026-10-07T12:00:00Z",
        },
    })
    result = await inbound.submit_inbound_envelope(
        seeded_session, connection=_bridge_connection(admin.serialize_connection(connection)), envelope=envelope,
    )
    assert result["status"] == inbound.STATUS_PROCESSED and result["error"] is None
    record = (await seeded_session.scalars(select(DomainRecord).where(DomainRecord.domain_id == tracker.id))).one()
    assert record.data["state"] == expected and record.data["author"] == "original-author"
    if "merged" in projection.field_mapping:
        assert record.data["merged"] is merged
    else:
        assert "merged" not in record.data
    assert len(list(await seeded_session.scalars(select(DomainRecord).where(DomainRecord.domain_id == feed.domain_id)))) == 1


async def test_real_tracker_setup_preserves_editorial_owner_and_dual_outputs(seeded_session, tracker_config):
    tracker, feed, connection = tracker_config
    await configure_github_tracker(seeded_session, org_id=ORG_ID, apply=True)
    domain_service = AsyncDomainService(seeded_session)
    existing = await domain_service.create_record(ORG_ID, tracker.id, "ticket", data={
        "external_id": "github:owner/repo:issue:1", "status": "In Progress", "assignee": "next-action-owner",
    })
    payload = {
        "repository": {"full_name": "owner/repo"}, "action": "opened", "issue": {
            "node_id": "issue-node", "number": 1, "title": "Current issue", "state": "open",
            "html_url": "https://github.com/owner/repo/issues/1", "user": {"login": "original-author"},
            "assignee": {"login": "github-assignee"}, "updated_at": "2026-10-07T12:00:00Z",
        },
    }
    principal = _bridge_connection(admin.serialize_connection(connection))
    async def submit(event, body):
        return await inbound.submit_inbound_envelope(
            seeded_session, connection=principal, envelope=github_event_to_envelope(event, body),
        )
    await submit("issues", payload)
    assert existing.data["status"] == "In Progress" and existing.data["assignee"] == "next-action-owner"
    payload["issue"].update(number=2, node_id="second-node", html_url="https://github.com/owner/repo/issues/2")
    await submit("issues", payload)
    tickets = list(await seeded_session.scalars(select(DomainRecord).where(DomainRecord.domain_id == tracker.id)))
    new_ticket = next(row for row in tickets if row.id != existing.id)
    assert new_ticket.data["status"] == "Backlog"
    payload["issue"].update(number=1, node_id="issue-node", html_url="https://github.com/owner/repo/issues/1", state="closed", closed_at="2026-10-07T13:00:00Z")
    await submit("issues", payload)
    assert existing.data["github_state"] == "closed" and existing.data["status"] == "In Progress"
    assert existing.data["assignee"] == "next-action-owner"
    payload["comment"] = {"user": {"login": "last-commenter"}, "updated_at": "2026-10-07T14:00:00Z"}
    result = await submit("issue_comment", payload)
    assert all(row["result"]["operation"] == "skipped" for row in result["ilo_outcome"]["projections"] if row["domain_id"] == tracker.id)
    feed_records = list(await seeded_session.scalars(select(DomainRecord).where(DomainRecord.domain_id == feed.domain_id)))
    assert len(feed_records) == 2 and len(tickets) == 2


async def test_closed_source_snapshot_and_webhook_preserve_production_gated_status(seeded_session, tracker_config):
    tracker, _feed, connection = tracker_config
    setup = await configure_github_tracker(seeded_session, org_id=ORG_ID, apply=True)
    projection_id = next(row["projection_id"] for row in setup["projections"] if row["object_key"] == "ticket")
    await deploy_tracker.ensure_production_gate_fields(seeded_session, org_id=ORG_ID, domain_id=tracker.id)
    row = await AsyncDomainService(seeded_session).create_record(ORG_ID, tracker.id, "ticket", data={
        "external_id": "github:owner/repo:issue:1", "status": "In Progress", "assignee": "release-owner",
    })
    await deploy_tracker.mark_prod_pending(
        seeded_session, row, fix_pr="owner/repo#3", fix_merge_sha="a" * 40,
        progress_lines=[], reason="test:pending_release",
    )
    subject = {
        "number": 1, "node_id": "issue-node", "title": "Closed on GitHub", "state": "closed",
        "html_url": "https://github.com/owner/repo/issues/1", "updated_at": "2026-10-07T11:00:00Z",
        "closed_at": "2026-10-07T11:00:00Z", "user": {"login": "original-author"},
    }
    snapshot = {"captured_at": "2026-10-07T12:00:00Z", "items": [{"event": "issues", "repository": "owner/repo", "subject": subject}]}
    plan = await recover_tracker_snapshot(seeded_session, org_id=ORG_ID, projection_id=projection_id, snapshot=snapshot)
    await recover_tracker_snapshot(seeded_session, org_id=ORG_ID, projection_id=projection_id, snapshot=snapshot, apply=True, reviewed_plan=plan)
    assert row.data["github_state"] == "closed" and row.data["closed_at"] == subject["closed_at"]
    assert row.data["status"] == "In Review" and row.data["production_gate"] == "prod_pending"
    assert row.data["assignee"] == "release-owner"
    result = await inbound.submit_inbound_envelope(
        seeded_session, connection=_bridge_connection(admin.serialize_connection(connection)),
        envelope=github_event_to_envelope("issues", {"repository": {"full_name": "owner/repo"}, "issue": subject, "action": "edited"}),
    )
    assert result["status"] == inbound.STATUS_PROCESSED and result["error"] is None
    assert row.data["status"] == "In Review" and row.data["production_gate"] == "prod_pending"
    assert row.data["assignee"] == "release-owner" and row.data["fix_merge_sha"] == "a" * 40


@pytest.mark.parametrize("object_key", ["ticket", "pull_request"])
@pytest.mark.parametrize("title_key", ["title", "summary"])
async def test_setup_populates_required_title_field_for_recovery_and_webhook(
    seeded_session, tracker_config, object_key, title_key,
):
    tracker, _feed, connection = tracker_config
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(tracker.id, object_key)
    await service.add_field_definition(obj, {"key": title_key, "field_type": "text", "required": True})
    if title_key == "summary":
        obj.title_field = title_key
    setup = await configure_github_tracker(seeded_session, org_id=ORG_ID, apply=True)
    projection_id = next(row["projection_id"] for row in setup["projections"] if row["object_key"] == object_key)
    event, subject_key, url_type = ("issues", "issue", "issues") if object_key == "ticket" else ("pull_request", "pull_request", "pull")
    subject = {
        "number": 1, "node_id": "source-node", "title": "Source title", "state": "closed", "merged": False,
        "html_url": f"https://github.com/owner/repo/{url_type}/1", "updated_at": "2026-10-07T11:00:00Z",
        "user": {"login": "original-author"},
    }
    snapshot = {"captured_at": "2026-10-07T12:00:00Z", "items": [{"event": event, "repository": "owner/repo", "subject": subject}]}
    plan = await recover_tracker_snapshot(seeded_session, org_id=ORG_ID, projection_id=projection_id, snapshot=snapshot)
    await recover_tracker_snapshot(seeded_session, org_id=ORG_ID, projection_id=projection_id, snapshot=snapshot, apply=True, reviewed_plan=plan)
    row = (await seeded_session.scalars(select(DomainRecord).where(DomainRecord.domain_id == tracker.id))).one()
    assert row.data[title_key] == row.title == "Source title"
    subject["title"] = "Updated source title"
    result = await inbound.submit_inbound_envelope(
        seeded_session, connection=_bridge_connection(admin.serialize_connection(connection)),
        envelope=github_event_to_envelope(event, {"repository": {"full_name": "owner/repo"}, subject_key: subject, "action": "edited"}),
    )
    assert result["status"] == inbound.STATUS_PROCESSED and result["error"] is None
    assert row.data[title_key] == row.title == "Updated source title"


async def test_recovery_dry_run_uses_canonical_required_creation_defaults(seeded_session, tracker_config):
    tracker, _feed, _connection = tracker_config
    result = await configure_github_tracker(seeded_session, org_id=ORG_ID, apply=True)
    projection_id = next(row["projection_id"] for row in result["projections"] if row["object_key"] == "ticket")
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(tracker.id, "ticket")
    next(field for field in await service.list_fields(obj.id) if field.key == "status").required = True
    snapshot = {"captured_at": "2026-10-07T12:00:00Z", "items": [{
        "event": "issues", "repository": "owner/repo", "subject": {
            "number": 1, "title": "Open issue", "html_url": "https://github.com/owner/repo/issues/1",
            "state": "open", "updated_at": "2026-10-07T11:00:00Z", "user": {"login": "original-author"},
        },
    }]}
    plan = await recover_tracker_snapshot(seeded_session, org_id=ORG_ID, projection_id=projection_id, snapshot=snapshot)
    await recover_tracker_snapshot(seeded_session, org_id=ORG_ID, projection_id=projection_id, snapshot=snapshot, apply=True, reviewed_plan=plan)
    record = (await seeded_session.scalars(select(DomainRecord).where(DomainRecord.domain_id == tracker.id))).one()
    assert record.data["status"] == "Backlog"
