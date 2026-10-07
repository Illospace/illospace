from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from brain.platform.db.models.domain import DomainEvent, DomainRecord
from brain.platform.db.models.inbound import InboundDecisionReceiptRow, InboundDomainProjectionKeyRow, InboundEventRow, InboundSourcePolicyRow
from brain.systems.inbound.errors import InboundValidationError
from brain.systems.inbound import admin
from brain.systems.inbound.tracker_recovery import recover_tracker_snapshot
from brain.systems.user_domains.service import AsyncDomainService, DomainError
from tests.inbound_admin_support import (
    ORG_ID, USER_ID, _create_issue_domain,
    seeded_session as seeded_session, session as session,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def projection(seeded_session):
    domain = await _create_issue_domain(seeded_session)
    connection = await admin.create_connection(
        seeded_session, org_id=ORG_ID, owner_user_id=USER_ID,
        display_name="GitHub", agent_kind="github", transport="webhook",
    )
    policy = await admin.create_policy(
        seeded_session, org_id=ORG_ID, connection_id=connection.id, name="Issues",
        origin_patterns=["github:owner/repo"], envelope_kinds=["github_event"],
    )
    return await admin.create_projection(
        seeded_session, org_id=ORG_ID, connection_id=connection.id, policy_id=policy.id,
        domain_id=domain.id, object_key="issue", external_id_field="external_id",
        external_id_path="github:{hints.repo}:issue:{hints.number}",
        field_mapping={"summary": "payload.issue.title", "status": "hints.issue_outcome"},
    )


def _snapshot(*numbers):
    return {"captured_at": "2026-10-07T12:00:00Z", "items": [
        {"event": "issues", "repository": "owner/repo", "subject": {
            "number": number, "html_url": f"https://github.com/owner/repo/issues/{number}",
            "title": f"Current issue {number}", "state": "closed", "updated_at": "2026-10-07T11:00:00Z",
            "user": {"login": "original-author"},
        }} for number in numbers
    ]}


async def _recover(session, projection, snapshot, **kwargs):
    return await recover_tracker_snapshot(
        session, org_id=ORG_ID, projection_id=projection.id, snapshot=snapshot, **kwargs,
    )


async def test_recovery_dry_run_then_atomic_upsert_merge_and_idempotent_repeat(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    original = await service.create_record(ORG_ID, projection.domain_id, "issue", data={
        "external_id": "github:owner/repo:issue:1", "summary": "Old issue", "status": "open",
    })
    duplicate = DomainRecord(
        org_id=ORG_ID, domain_id=projection.domain_id, object_type_id=original.object_type_id,
        title="Copy", search_text="", data=dict(original.data),
    )
    seeded_session.add(duplicate)
    await seeded_session.flush()
    key = InboundDomainProjectionKeyRow(
        org_id=ORG_ID, projection_id=projection.id, domain_id=projection.domain_id,
        external_id="github:owner/repo:issue:1", record_id=duplicate.id,
    )
    seeded_session.add(key)
    await seeded_session.flush()
    snapshot = _snapshot(1, 2)
    plan = await _recover(seeded_session, projection, snapshot)
    assert plan["applied"] is False
    assert plan["items"][0]["archive_ids"] == [duplicate.id]
    assert original.data["status"] == "open" and duplicate.archived_at is None
    assert await seeded_session.scalar(select(func.count()).select_from(DomainRecord)) == 2
    result = await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert result["items"][0]["record_id"] == original.id == key.record_id
    assert original.data["status"] == "closed" and duplicate.archived_at is not None
    active = list(await seeded_session.scalars(select(DomainRecord).where(DomainRecord.archived_at.is_(None))))
    assert len(active) == 2
    versions = {row.id: row.version for row in active}
    events = await seeded_session.scalar(select(func.count()).select_from(DomainEvent))
    repeated = await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert all(item["operation"] == "unchanged" for item in repeated["items"])
    assert {row.id: row.version for row in active} == versions
    assert await seeded_session.scalar(select(func.count()).select_from(DomainEvent)) == events
    assert await seeded_session.scalar(select(func.count()).select_from(InboundEventRow)) == 0
    assert await seeded_session.scalar(select(func.count()).select_from(InboundDecisionReceiptRow)) == 0


async def test_duplicate_only_repair_preserves_canonical_editorial_state_and_skips_missing(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    original = await service.create_record(ORG_ID, projection.domain_id, "issue", data={
        "external_id": "github:owner/repo:issue:1", "summary": "Canonical title", "status": "In Progress",
    })
    before_data, before_title = dict(original.data), original.title
    duplicate = DomainRecord(
        org_id=ORG_ID, domain_id=projection.domain_id, object_type_id=original.object_type_id,
        title="Copy", search_text="", data={**original.data, "status": "Done"},
    )
    seeded_session.add(duplicate)
    await seeded_session.flush()
    snapshot = _snapshot(1, 2)
    plan = await _recover(seeded_session, projection, snapshot, deduplicate_only=True)
    result = await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan, deduplicate_only=True)
    assert result["items"][0]["operation"] == "deduplicate" and result["items"][1]["operation"] == "missing"
    assert original.data == before_data and original.title == before_title
    assert duplicate.archived_at is not None
    assert await seeded_session.scalar(select(func.count()).select_from(DomainRecord)) == 2
    version = original.version
    await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan, deduplicate_only=True)
    assert original.version == version
    with pytest.raises(DomainError, match="matching reviewed"):
        await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert original.data == before_data


async def test_recovery_rejects_record_changes_after_review(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    row = await service.create_record(ORG_ID, projection.domain_id, "issue", data={
        "external_id": "github:owner/repo:issue:2", "summary": "Old issue", "status": "open",
    })
    snapshot = _snapshot(1, 2)
    plan = await _recover(seeded_session, projection, snapshot)
    await service.update_record(ORG_ID, projection.domain_id, row.id, data_patch={"summary": "Changed"})
    with pytest.raises(DomainError, match="changed after planning"):
        await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert await seeded_session.scalar(select(func.count()).select_from(DomainRecord)) == 1
    assert row.data["status"] == "open"


@pytest.mark.parametrize("change", ["snapshot", "projection"])
async def test_recovery_requires_matching_reviewed_snapshot_and_configuration(seeded_session, projection, change):
    snapshot = _snapshot(1)
    plan = await _recover(seeded_session, projection, snapshot)
    if change == "snapshot":
        snapshot["items"][0]["subject"]["title"] = "Changed"
    else:
        projection.field_mapping = {"summary": "payload.issue.title", "status": "hints.state"}
        await seeded_session.flush()
    with pytest.raises(DomainError, match="matching reviewed"):
        await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert await seeded_session.scalar(select(func.count()).select_from(DomainRecord)) == 0


@pytest.mark.parametrize("change", ["repo", "url", "event", "pr", "timestamp", "duplicate", "identity", "author"])
async def test_recovery_rejects_inconsistent_snapshot_before_writes(seeded_session, projection, change):
    snapshot = _snapshot(1)
    item = snapshot["items"][0]
    if change == "repo": item["repository"] = "different/repo"
    if change == "url": item["subject"]["html_url"] = "https://example.com"
    if change == "event": item["event"] = "issue_comment"
    if change == "pr": item["subject"]["pull_request"] = {}
    if change == "timestamp": item["subject"]["updated_at"] = "2026-10-07T13:00:00Z"
    if change == "duplicate": snapshot["items"].append(deepcopy(item))
    if change == "identity": projection.external_id_path = "github:{hints.repo}:pr:{hints.number}"
    if change == "author": del item["subject"]["user"]
    with pytest.raises(DomainError):
        await _recover(seeded_session, projection, snapshot)
    assert await seeded_session.scalar(select(func.count()).select_from(DomainRecord)) == 0


async def test_recovery_rejects_snapshot_older_than_mapped_freshness(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(projection.domain_id, "issue")
    await service.add_field_definition(obj, {"key": "synced_at", "field_type": "datetime"})
    projection.field_mapping = {**projection.field_mapping, "synced_at": {"now": True}}
    await service.create_record(ORG_ID, projection.domain_id, "issue", data={
        "external_id": "github:owner/repo:issue:1", "summary": "Newer", "status": "open",
        "synced_at": "2026-10-07T12:30:00Z",
    })
    with pytest.raises(DomainError, match="older than"):
        await _recover(seeded_session, projection, _snapshot(1))


@pytest.mark.parametrize("mapping", [
    "hints.source_updated_at", {"path": "hints.source_updated_at"}, "payload.issue.updated_at",
    "envelope.hints.source_updated_at", "envelope.payload.issue.updated_at",
])
async def test_recovery_uses_rendered_freshness_for_equivalent_mapping_forms(seeded_session, projection, mapping):
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(projection.domain_id, "issue")
    await service.add_field_definition(obj, {"key": "updated_at", "field_type": "datetime"})
    projection.field_mapping = {**projection.field_mapping, "updated_at": mapping}
    await service.create_record(ORG_ID, projection.domain_id, "issue", data={
        "external_id": "github:owner/repo:issue:1", "summary": "Newer", "status": "open",
        "updated_at": "2026-10-07T11:30:00Z",
    })
    with pytest.raises(DomainError, match="older than"):
        await _recover(seeded_session, projection, _snapshot(1))


async def test_recovery_completed_plan_survives_open_label_vocabulary_extension(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(projection.domain_id, "issue")
    field = await service.add_field_definition(obj, {"key": "labels", "field_type": "multi_enum", "options": ["bug"]})
    projection.field_mapping = {**projection.field_mapping, "labels": {"const": ["new-label"]}}
    snapshot = _snapshot(1)
    plan = await _recover(seeded_session, projection, snapshot)
    await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert "new-label" in field.options
    repeated = await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert repeated["items"][0]["operation"] == "unchanged"


async def test_recovery_honors_selected_projection_condition(seeded_session, projection):
    projection.metadata_ = {"when": {"path": "hints.event", "equals": "pull_request"}}
    with pytest.raises(DomainError, match="projection's condition"):
        await _recover(seeded_session, projection, _snapshot(1))


async def test_recovery_rejects_mapped_identity_override_before_selecting_target(seeded_session, projection):
    row = await AsyncDomainService(seeded_session).create_record(ORG_ID, projection.domain_id, "issue", data={
        "external_id": "github:owner/repo:issue:2", "summary": "Issue two", "status": "open",
    })
    original = dict(row.data)
    projection.field_mapping = {**projection.field_mapping, "external_id": {"const": "github:owner/repo:issue:2"}}
    with pytest.raises(DomainError, match="Rendered record identity"):
        await _recover(seeded_session, projection, _snapshot(1))
    assert row.data == original


@pytest.mark.parametrize("mapped_field, value", [("repo", "other/repo"), ("number", 2), ("url", "https://github.com/owner/repo/issues/2")])
async def test_recovery_rejects_mapped_natural_key_override(seeded_session, projection, mapped_field, value):
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(projection.domain_id, "issue")
    fields = await service.list_fields(obj.id)
    next(field for field in fields if field.key == "external_id").required = False
    for key, kind in (("repo", "text"), ("number", "number"), ("url", "text")):
        await service.add_field_definition(obj, {"key": key, "field_type": kind})
    row = await service.create_record(ORG_ID, projection.domain_id, "issue", data={
        "repo": "owner/repo", "number": 2, "summary": "Issue two", "status": "open",
    })
    original = dict(row.data)
    projection.field_mapping = {**projection.field_mapping, "repo": "hints.repo", "number": "hints.number", "url": "hints.url", mapped_field: {"const": value}}
    with pytest.raises(DomainError, match="must match the snapshot identity"):
        await _recover(seeded_session, projection, _snapshot(1))
    assert row.data == original and row.archived_at is None


@pytest.mark.parametrize("control", ["permission", "schema"])
async def test_recovery_honors_current_source_policy_controls(seeded_session, projection, control):
    policy = await seeded_session.get(InboundSourcePolicyRow, projection.policy_id)
    if control == "permission":
        policy.allowed_actions = []
        error, message = DomainError, "does not allow projection writes"
    else:
        policy.schema_config = {"required_paths": ["payload.issue.node_id"]}
        error, message = InboundValidationError, "Missing required inbound field"
    with pytest.raises(error, match=message):
        await _recover(seeded_session, projection, _snapshot(1))
    assert await seeded_session.scalar(select(func.count()).select_from(DomainRecord)) == 0


async def test_duplicate_only_archives_exact_reviewed_identity_set(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(projection.domain_id, "issue")
    fields = await service.list_fields(obj.id)
    next(field for field in fields if field.key == "external_id").required = False
    await service.add_field_definition(obj, {"key": "repo", "field_type": "text"})
    await service.add_field_definition(obj, {"key": "number", "field_type": "number"})
    projection.field_mapping = {**projection.field_mapping, "repo": "hints.repo", "number": "hints.number"}
    canonical = await service.create_record(ORG_ID, projection.domain_id, "issue", data={
        "summary": "Canonical", "repo": "owner/repo", "number": 1, "status": "In Progress",
    })
    unrelated = DomainRecord(
        org_id=ORG_ID, domain_id=projection.domain_id, object_type_id=obj.id, title="Unrelated", search_text="",
        data={**canonical.data, "external_id": "github:owner/repo:pr:1"},
    )
    duplicate = DomainRecord(
        org_id=ORG_ID, domain_id=projection.domain_id, object_type_id=obj.id, title="Duplicate", search_text="",
        data={**canonical.data, "external_id": "github:owner/repo:issue:1"},
    )
    seeded_session.add(unrelated)
    await seeded_session.flush()
    seeded_session.add(duplicate)
    await seeded_session.flush()
    snapshot = _snapshot(1)
    plan = await _recover(seeded_session, projection, snapshot, deduplicate_only=True)
    assert plan["items"][0]["archive_ids"] == [duplicate.id]
    before = dict(canonical.data)
    await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan, deduplicate_only=True)
    assert duplicate.archived_at is not None and unrelated.archived_at is None
    assert canonical.data == before


async def test_recovery_clock_fields_and_merged_pr_state_repeat_without_writes(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(projection.domain_id, "issue")
    await service.add_field_definition(obj, {"key": "synced_at", "field_type": "datetime"})
    projection.external_id_path = "github:{hints.repo}:pr:{hints.number}"
    projection.field_mapping = {
        "summary": "payload.pull_request.title", "status": "hints.pr_outcome", "synced_at": {"now": True},
    }
    snapshot = _snapshot(1)
    item = snapshot["items"][0]
    item["event"] = "pull_request"
    item["subject"].update(html_url="https://github.com/owner/repo/pull/1", merged=True)
    plan = await _recover(seeded_session, projection, snapshot)
    await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    row = (await seeded_session.scalars(select(DomainRecord))).one()
    assert row.data["status"] == "merged"
    assert row.data["synced_at"] == snapshot["captured_at"]
    version = row.version
    repeated = await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    assert repeated["items"][0]["operation"] == "unchanged" and row.version == version


async def test_recovery_maps_original_subject_author(seeded_session, projection):
    service = AsyncDomainService(seeded_session)
    obj = await service.get_object_type(projection.domain_id, "issue")
    await service.add_field_definition(obj, {"key": "author", "field_type": "text"})
    projection.field_mapping = {**projection.field_mapping, "author": "hints.author"}
    snapshot = _snapshot(1)
    snapshot["items"][0]["comment"] = {"user": {"login": "last-commenter"}}
    plan = await _recover(seeded_session, projection, snapshot)
    await _recover(seeded_session, projection, snapshot, apply=True, reviewed_plan=plan)
    row = (await seeded_session.scalars(select(DomainRecord))).one()
    assert row.data["author"] == "original-author"


@pytest.mark.parametrize("guard", ["org", "disabled", "mode"])
async def test_recovery_requires_explicit_enabled_org_scoped_upsert(seeded_session, projection, guard):
    if guard == "disabled": projection.enabled = False
    if guard == "mode": projection.upsert_mode = "create_only"
    with pytest.raises(DomainError, match="enabled upsert projection"):
        await recover_tracker_snapshot(
            seeded_session, org_id="different" if guard == "org" else ORG_ID,
            projection_id=projection.id, snapshot=_snapshot(1),
        )
    assert await seeded_session.scalar(select(func.count()).select_from(DomainRecord)) == 0
