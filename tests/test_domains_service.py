from __future__ import annotations

from datetime import datetime, timezone
import json
import re

import pytest
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
from brain.systems.user_domains.service import AsyncDomainService, DomainError

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory([
        Domain.__table__,
        DomainObjectType.__table__,
        DomainFieldDefinition.__table__,
        DomainRelationType.__table__,
        DomainRecord.__table__,
        DomainRelation.__table__,
        DomainEvent.__table__,
    ])


async def test_create_custom_domain_schema_and_record(session):
    service = AsyncDomainService(session)

    domain = await service.create_domain(
        ORG_ID,
        name="Hooks Tried",
        objects=[
            {
                "key": "hook",
                "name": "Hook",
                "title_field": "text",
                "fields": [
                    {"key": "text", "field_type": "text", "required": True},
                    {
                        "key": "status",
                        "field_type": "enum",
                        "options": ["new", "tested", "winner"],
                        "default_value": "new",
                    },
                    {"key": "score", "field_type": "number"},
                ],
            }
        ],
        actor_id=USER_ID,
    )

    schema = await service.serialize_domain_schema(domain)
    assert schema["slug"] == "hooks-tried"
    assert schema["objects"][0]["key"] == "hook"
    assert [field["key"] for field in schema["objects"][0]["fields"]] == [
        "text",
        "status",
        "score",
    ]

    record = await service.create_record(
        ORG_ID,
        domain.id,
        "hook",
        data={"text": "Start with the pain", "score": "8"},
        actor_id=USER_ID,
    )

    assert record.title == "Start with the pain"
    assert record.data == {"text": "Start with the pain", "status": "new", "score": 8}
    assert record.version == 1
    assert [event.event_type for event in await service.list_events(ORG_ID, domain.id)] == [
        "record.created",
        "domain.created",
    ]


async def test_list_records_filters_by_data_fields_and_record_metadata(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Work Ledger",
        objects=[
            {
                "key": "ticket",
                "name": "Ticket",
                "title_field": "title",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text"},
                    {"key": "assignee", "field_type": "text"},
                    {"key": "status", "field_type": "enum", "options": ["todo", "doing", "done"]},
                    {"key": "labels", "field_type": "json"},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    reda = await service.create_record(
        ORG_ID,
        domain.id,
        "ticket",
        data={
            "title": "Ship MCP filters",
            "repo": "uwear-ai/illospace-project",
            "assignee": "Reda",
            "status": "todo",
            "labels": ["mcp", "coordination"],
        },
    )
    await service.create_record(
        ORG_ID,
        domain.id,
        "ticket",
        data={
            "title": "Polish docs",
            "repo": "uwear-ai/uwear-website",
            "assignee": "Axel",
            "status": "todo",
            "labels": ["docs"],
        },
    )

    records = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="ticket",
        filters={
            "assignee": "reda",
            "repo": "uwear-ai/illospace-project",
            "status": ["todo", "doing"],
            "labels": {"contains": "mcp"},
            "title": {"contains": "filters"},
        },
    )

    assert [record.id for record in records] == [reda.id]


async def _create_pr_tracker(service: AsyncDomainService) -> Domain:
    return await service.create_domain(
        ORG_ID,
        name="GitHub Pull Request Tracker",
        objects=[
            {
                "key": "pull_request",
                "name": "Pull Request",
                "title_field": "title",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text", "required": True},
                    {"key": "pr_number", "field_type": "number", "required": True},
                    {"key": "pr_url", "field_type": "url"},
                    {
                        "key": "status",
                        "field_type": "enum",
                        "options": ["open", "in_review", "merged"],
                    },
                    {"key": "sync_source", "field_type": "text"},
                    {"key": "reviewer", "field_type": "text"},
                ],
            }
        ],
    )


def _chantier_object_definition() -> dict:
    github_issue_pattern = (
        r"github:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:issue:[1-9][0-9]*"
    )
    return {
        "key": "chantier",
        "name": "Chantier",
        "title_field": "title",
        "fields": [
            {
                "key": "slug",
                "field_type": "text",
                "required": True,
                "validation": {
                    "immutable": True,
                    "max_length": 80,
                    "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
                },
            },
            {"key": "title", "field_type": "text", "required": True},
            {
                "key": "goal",
                "field_type": "long_text",
                "required": True,
                "validation": {"pattern": r"(?is)^done means\s+\S.*$"},
            },
            {
                "key": "kind",
                "field_type": "enum",
                "required": True,
                "options": ["feature", "incident", "quality", "gtm", "sunset"],
            },
            {
                "key": "state",
                "field_type": "enum",
                "required": True,
                "options": [
                    "exploring",
                    "building",
                    "shipping",
                    "verifying",
                    "done",
                    "paused",
                ],
            },
            {
                "key": "owner",
                "field_type": "text",
                "validation": {"max_length": 120},
            },
            {
                "key": "refs",
                "field_type": "json",
                "required": True,
                "validation": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["source", "ref"],
                        "additional_properties": False,
                        "properties": {
                            "source": {
                                "type": "string",
                                "enum": ["github", "doc", "slack", "posthog", "url"],
                            },
                            "ref": {"type": "string", "min_length": 1},
                            "title": {"type": "string", "min_length": 1},
                        },
                        "source_ref_patterns": {"github": github_issue_pattern},
                    },
                },
            },
            {
                "key": "parent_issue",
                "field_type": "text",
                "validation": {"pattern": f"^{github_issue_pattern}$"},
            },
            {
                "key": "next_step",
                "field_type": "text",
                "required": True,
                "validation": {"pattern": r"^[^\r\n]+$"},
            },
            {"key": "progress_note", "field_type": "long_text"},
            {
                "key": "superseded_by",
                "field_type": "text",
                "validation": {
                    "max_length": 80,
                    "pattern": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
                },
            },
            {"key": "created_at", "field_type": "datetime"},
            {"key": "updated_at", "field_type": "datetime"},
        ],
    }


def _chantier_record_data() -> dict:
    return {
        "slug": "agent-runtime-keystone",
        "title": "Agent runtime chantier layer",
        "goal": "Done means chantier members can be coordinated across repositories.",
        "kind": "feature",
        "state": "building",
        "owner": "Reda",
        "refs": [
            {
                "source": "github",
                "ref": "github:Illospace/illospace:issue:326",
                "title": "Chantier layer umbrella",
            },
            {
                "source": "doc",
                "ref": "brain/references/chantier-record-contract.md",
            },
        ],
        "parent_issue": "github:Illospace/illospace:issue:326",
        "next_step": "Land the record contract and unblock member-ticket work.",
        "progress_note": "Schema implementation is in review.",
        "superseded_by": None,
        "created_at": "2026-07-16T14:00:00Z",
        "updated_at": "2026-07-16T15:00:00Z",
    }


def test_slack_chantier_declaration_parses_mechanical_contract():
    from brain.systems.slack.chantier_declare import parse_chantier_declaration

    declaration = parse_chantier_declaration(
        "<@BILLO> chantier: clothing-intake hardening — "
        "done means zero pool alerts for a week kind: quality owner: Axel "
        "next_step: verify prod for seven days "
        "https://github.com/Illospace/illospace/issues/331 https://example.com/spec"
    )

    assert declaration is not None
    assert declaration.slug == "clothing-intake-hardening"
    assert declaration.title == "clothing-intake hardening"
    assert declaration.goal == "Done means zero pool alerts for a week"
    assert declaration.kind == "quality"
    assert declaration.kind_is_explicit is True
    assert declaration.owner == "Axel"
    assert declaration.next_step == "verify prod for seven days"
    assert declaration.mirror_repo_suggestion == "Illospace/illospace"
    assert declaration.refs == (
        {
            "source": "github",
            "ref": "github:Illospace/illospace:issue:331",
            "title": "GitHub issue Illospace/illospace#331",
        },
        {"source": "url", "ref": "https://example.com/spec"},
    )
    assert parse_chantier_declaration("<@BILLO> what chantier are active?") is None


def test_slack_chantier_declaration_requires_declare_intent_without_colon():
    from brain.systems.slack.chantier_declare import parse_chantier_declaration

    assert parse_chantier_declaration("super, so this chantier is locked") is None
    assert parse_chantier_declaration("is the chantier ready?") is None
    assert parse_chantier_declaration("the chantier looks good") is None
    assert parse_chantier_declaration("chantier update please") is None

    declared = parse_chantier_declaration("declare chantier reliability")
    assert declared is not None and declared.title == "reliability"
    colon = parse_chantier_declaration("chantier: Reliability push")
    assert colon is not None and colon.title == "Reliability push"
    french = parse_chantier_declaration("nouveau chantier fiabilité")
    assert french is not None and french.title == "fiabilité"


async def test_slack_chantier_declare_persists_explicit_sunset_kind(session):
    from brain.systems.slack.chantier_declare import maybe_declare_chantier_from_slack

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )

    result = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text=(
            "<@BILLO> chantier: shopify app sunset — "
            "done means the retired app is removed with its rider kind: sunset"
        ),
    )

    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert result is not None and result.operation == "created"
    assert result.data["kind"] == "sunset"
    assert len(records) == 1
    assert records[0].data["kind"] == "sunset"


async def test_slack_chantier_duplicate_declare_updates_one_record(session):
    from brain.systems.slack.chantier_declare import maybe_declare_chantier_from_slack

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )

    created = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text=(
            "<@BILLO> chantier: clothing-intake hardening — "
            "done means zero pool alerts for a week "
            "https://github.com/Illospace/illospace/issues/331"
        ),
    )
    updated = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text=(
            "<@BILLO> chantier: Clothing intake hardening — "
            "done means no pool alerts for fourteen days kind: incident owner: Axel "
            "next_step: watch the production pool through Friday "
            "https://example.com/runbook"
        ),
    )

    records = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="chantier",
    )
    assert created is not None and created.operation == "created"
    assert created.needs_next_step is True
    assert updated is not None and updated.operation == "updated"
    assert updated.record_id == created.record_id
    assert updated.version == 2
    assert updated.needs_next_step is False
    assert len(records) == 1
    assert records[0].data["slug"] == "clothing-intake-hardening"
    assert records[0].data["goal"] == "Done means no pool alerts for fourteen days"
    assert records[0].data["kind"] == "incident"
    assert records[0].data["owner"] == "Axel"
    assert records[0].data["next_step"] == "watch the production pool through Friday"
    assert records[0].data["refs"] == [
        {
            "source": "github",
            "ref": "github:Illospace/illospace:issue:331",
            "title": "GitHub issue Illospace/illospace#331",
        },
        {"source": "url", "ref": "https://example.com/runbook"},
    ]


async def test_slack_chantier_declare_matches_obvious_title_and_reuses_parent_mirror(session):
    from brain.systems.slack.chantier_declare import maybe_declare_chantier_from_slack

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    existing = await service.create_record(
        ORG_ID,
        domain.id,
        "chantier",
        data=_chantier_record_data(),
    )

    result = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text=(
            "<@BILLO> chantier: Agent runtime chantier layer — "
            "done means every member arrives with the same outcome context"
        ),
    )

    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert result is not None and result.operation == "updated"
    assert result.record_id == existing.id
    assert result.data["slug"] == "agent-runtime-keystone"
    assert result.mirror_status == "linked"
    assert result.data["parent_issue"] == "github:Illospace/illospace:issue:326"
    assert [record.id for record in records] == [existing.id]


async def test_slack_chantier_declare_attaches_high_confidence_family_without_insert(session):
    from brain.systems.slack.chantier_declare import maybe_declare_chantier_from_slack

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    canonical_data = _chantier_record_data()
    canonical_data.update(
        slug="agent-mcp-repositioning",
        title="Agent MCP Repositioning",
        goal="Done means the agent MCP surface has one canonical product position.",
    )
    canonical = await service.create_record(
        ORG_ID,
        domain.id,
        "chantier",
        data=canonical_data,
    )

    attached = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text=(
            "<@BILLO> chantier: V3 canonical agent MCP repositioning automation builder chat — "
            "done means the agent MCP surface has one canonical product position"
        ),
    )

    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert attached is not None and attached.operation == "updated"
    assert attached.record_id == canonical.id
    assert attached.data["slug"] == "agent-mcp-repositioning"
    assert attached.data["title"] == "Agent MCP Repositioning"
    assert [record.id for record in records] == [canonical.id]


async def test_slack_chantier_router_does_not_create_without_keyword(session):
    from brain.systems.slack.chantier_declare import maybe_declare_chantier_from_slack

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )

    ordinary_mention = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text="<@BILLO> please harden clothing intake until pool alerts stay at zero",
    )
    dm_with_keyword = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.direct_message",
        text="chantier: clothing intake hardening — done means no pool alerts",
    )

    records = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="chantier",
    )
    assert ordinary_mention is None
    assert dm_with_keyword is None
    assert records == []


async def test_slack_chantier_declare_skips_bound_chantier_thread(session):
    from brain.systems.slack.chantier_declare import maybe_declare_chantier_from_slack

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    data = _chantier_record_data()
    data["refs"] = [
        *data["refs"],
        {
            "source": "slack",
            "ref": "slack:TTEAM:CCHANTIER:1784408445.531609",
        },
    ]
    existing = await service.create_record(
        ORG_ID,
        domain.id,
        "chantier",
        data=data,
    )

    result = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text="<@BILLO> chantier: junk declaration",
        channel_id="CCHANTIER",
        thread_ts="1784408445.531609",
    )

    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    assert result is None
    assert [record.id for record in records] == [existing.id]


async def test_slack_chantier_declare_run_contract_requires_threaded_echo_and_mirror(session):
    from brain.systems.slack.chantier_declare import (
        apply_chantier_declare_run_contract,
        maybe_declare_chantier_from_slack,
    )
    from brain.systems.slack.triggers import build_slack_work_intake_payload
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    service = AsyncDomainService(session)
    await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    result = await maybe_declare_chantier_from_slack(
        session,
        org_id=ORG_ID,
        actor_user_id=USER_ID,
        origin="slack.app_mention",
        text=(
            "<@BILLO> chantier: clothing-intake hardening — "
            "done means zero pool alerts for a week"
        ),
    )
    assert result is not None
    trigger = build_slack_work_intake_payload(
        org_id=ORG_ID,
        authority_user_id=USER_ID,
        payload={
            "origin": "slack.app_mention",
            "team_id": "T789",
            "channel_id": "C456",
            "channel_type": "channel",
            "message_ts": "1716900000.000100",
            "thread_ts": "1716900000.000100",
            "slack_user_id": "U123",
            "text": "<@BILLO> chantier: clothing-intake hardening",
        },
    )

    apply_chantier_declare_run_contract(trigger, result=result)
    request = await build_agent_run_request(
        object(),
        WorkIntakeEvent.from_trigger_payload(trigger),
    )

    metadata = trigger["payload"]["metadata"]
    run_message = trigger["payload"]["run_message"]
    assert metadata["slack_trigger"]["response_target"]["thread_ts"] == "1716900000.000100"
    assert request.target_ref["slack_trigger"]["response_target"]["thread_ts"] == "1716900000.000100"
    assert metadata["chantier_declare"]["record_id"] == result.record_id
    assert "Reply with post_slack_reply in the declaration thread" in run_message
    assert "create_github_issue" in run_message
    assert "add_github_sub_issue" in run_message
    assert "mirror pending: <specific reason>" in run_message
    assert "Ask the teammate for `next_step`" in run_message


async def _insert_legacy_pr_record(
    session,
    service: AsyncDomainService,
    domain: Domain,
    *,
    title: str,
    repo: str,
    pr_number: int,
    **data,
) -> DomainRecord:
    obj = await service.get_object_type(domain.id, "pull_request")
    fields = await service.list_fields(obj.id)
    normalized = service.validate_record_data(
        fields,
        {"title": title, "repo": repo, "pr_number": pr_number, **data},
    )
    record = DomainRecord(
        org_id=ORG_ID,
        domain_id=domain.id,
        object_type_id=obj.id,
        title=title,
        data=normalized,
        search_text=title.lower(),
    )
    session.add(record)
    await session.flush()
    await session.refresh(record)
    return record


async def test_pr_tracker_create_record_upserts_repo_and_pr_number(session):
    service = AsyncDomainService(session)
    domain = await _create_pr_tracker(service)

    created = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "title": "Fix coordinator writes",
            "repo": "Illospace/illospace",
            "pr_number": 84,
            "status": "open",
            "sync_source": "mission-control",
        },
    )
    observed_again = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "title": "Fix coordinator writes",
            "repo": "Illospace/illospace",
            "pr_number": 84,
            "status": "in_review",
            "sync_source": "mission-control",
        },
    )

    records = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="pull_request",
    )
    assert [record.id for record in records] == [created.id]
    assert observed_again.id == created.id
    assert observed_again.version == 2
    assert observed_again.data["status"] == "in_review"


async def test_pr_tracker_upsert_normalizes_two_evidence_sources(session):
    service = AsyncDomainService(session)
    domain = await _create_pr_tracker(service)

    from_sync = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "title": "Fix coordinator writes",
            "repo": "https://github.com/Illospace/illospace.git",
            "pr_number": "84",
            "pr_url": "https://github.com/Illospace/illospace/pull/84",
            "status": "open",
            "sync_source": "mission-control",
        },
    )
    from_live_github = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "title": "Fix coordinator writes",
            "repo": "illospace/illospace",
            "pr_number": 84,
            "pr_url": "https://github.com/illospace/illospace/pull/84",
            "status": "in_review",
            "sync_source": "live-github",
        },
    )

    records = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="pull_request",
    )
    assert len(records) == 1
    assert from_live_github.id == from_sync.id
    assert records[0].data["sync_source"] == "live-github"


async def test_pr_tracker_write_merges_legacy_duplicates_into_lowest_id(session):
    service = AsyncDomainService(session)
    domain = await _create_pr_tracker(service)
    canonical = await _insert_legacy_pr_record(
        session,
        service,
        domain,
        title="Fix coordinator writes",
        repo="Illospace/illospace",
        pr_number=84,
        status="open",
        sync_source="mission-control",
    )
    duplicate = await _insert_legacy_pr_record(
        session,
        service,
        domain,
        title="PR #84",
        repo="illospace/illospace",
        pr_number=84,
        status="in_review",
        sync_source="live-github",
        reviewer="Reda",
    )

    merged = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "title": "Fix coordinator writes",
            "repo": "Illospace/illospace",
            "pr_number": 84,
            "status": "merged",
            "sync_source": "github-event",
        },
    )

    active = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="pull_request",
    )
    assert [record.id for record in active] == [canonical.id]
    assert merged.id == canonical.id < duplicate.id
    assert merged.data["status"] == "merged"
    assert merged.data["reviewer"] == "Reda"
    assert (await session.get(DomainRecord, duplicate.id)).archived_at is not None


async def test_legacy_pr_duplicates_can_still_be_archived_explicitly(session):
    service = AsyncDomainService(session)
    domain = await _create_pr_tracker(service)
    canonical = await _insert_legacy_pr_record(
        session,
        service,
        domain,
        title="Fix coordinator writes",
        repo="Illospace/illospace",
        pr_number=84,
    )
    duplicate = await _insert_legacy_pr_record(
        session,
        service,
        domain,
        title="PR #84",
        repo="Illospace/illospace",
        pr_number=84,
    )

    repair = await service.remove_record(
        ORG_ID,
        domain.id,
        duplicate.id,
        mode="archive",
        reason="Legacy duplicate repair",
    )

    active = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="pull_request",
    )
    assert repair["archived"] is True
    assert [record.id for record in active] == [canonical.id]


async def test_pr_tracker_distinct_pr_numbers_create_distinct_records(session):
    service = AsyncDomainService(session)
    domain = await _create_pr_tracker(service)

    first = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "title": "Fix coordinator writes",
            "repo": "Illospace/illospace",
            "pr_number": 84,
        },
    )
    second = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "title": "Keep archive repair",
            "repo": "Illospace/illospace",
            "pr_number": 85,
        },
    )

    records = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="pull_request",
    )
    assert {record.id for record in records} == {first.id, second.id}


async def test_domain_one_pr_contract_handles_open_draft_and_merged_first_attempt(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[
            {
                "key": "pull_request",
                "name": "Pull Request",
                "title_field": "external_id",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "repo", "field_type": "text", "required": True},
                    {"key": "number", "field_type": "number", "required": True},
                    {"key": "url", "field_type": "url"},
                    {
                        "key": "state",
                        "field_type": "enum",
                        "options": ["open", "closed", "merged", "draft"],
                    },
                    {"key": "author", "field_type": "text"},
                    {
                        "key": "review_status",
                        "field_type": "enum",
                        "options": [
                            "pending",
                            "changes_requested",
                            "approved",
                            "merged",
                        ],
                    },
                    {"key": "linked_ticket", "field_type": "record_ref"},
                    {"key": "updated_at", "field_type": "datetime"},
                    {"key": "assignee", "field_type": "text"},
                    {"key": "progress_note", "field_type": "long_text"},
                ],
            }
        ],
    )

    created = await service.create_record(
        ORG_ID,
        domain.id,
        "pull_request",
        data={
            "external_id": "Illospace/illospace#323",
            "repo": "Illospace/illospace",
            "number": 323,
            "state": "open",
            "review_status": "pending",
            "assignee": "Reda",
            "progress_note": "Finish tests, then request review.",
        },
    )

    stored = await service.get_record(ORG_ID, domain.id, created.id)
    assert stored.data["state"] == "open"
    assert stored.data["review_status"] == "pending"
    assert stored.data["assignee"] == "Reda"
    assert stored.data["progress_note"] == "Finish tests, then request review."

    draft = await service.update_record(
        ORG_ID,
        domain.id,
        created.id,
        data_patch={"state": "draft", "review_status": "pending"},
    )
    assert draft.data["state"] == "draft"
    assert draft.data["review_status"] == "pending"

    merged = await service.update_record(
        ORG_ID,
        domain.id,
        created.id,
        data_patch={
            "state": "merged",
            "review_status": "merged",
            "assignee": "Axel",
            "progress_note": "Verify staging and close the linked ticket.",
        },
    )
    assert merged.data["state"] == "merged"
    assert merged.data["review_status"] == "merged"
    assert merged.data["assignee"] == "Axel"
    assert merged.data["progress_note"] == "Verify staging and close the linked ticket."


async def test_chantier_contract_creates_updates_and_queries_full_records(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    data = _chantier_record_data()

    created = await service.create_record(
        ORG_ID,
        domain.id,
        "chantier",
        data=data,
    )
    updated = await service.update_record(
        ORG_ID,
        domain.id,
        created.id,
        data_patch={
            "state": "verifying",
            "next_step": "Verify the migration against the complete Domain test suite.",
            "updated_at": "2026-07-16T16:00:00Z",
        },
        expected_version=1,
    )
    queried = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="chantier",
        search="agent runtime",
    )

    assert created.title == data["title"]
    assert updated.version == 2
    assert updated.data == {
        **data,
        "state": "verifying",
        "next_step": "Verify the migration against the complete Domain test suite.",
        "updated_at": "2026-07-16T16:00:00Z",
    }
    assert [record.id for record in queried] == [created.id]


@pytest.mark.parametrize("kind", ["feature", "incident", "quality", "gtm"])
async def test_chantier_contract_still_accepts_existing_kinds(session, kind):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    data = _chantier_record_data()
    data.update(slug=f"{kind}-chantier", title=f"{kind.title()} chantier", kind=kind)

    record = await service.create_record(
        ORG_ID,
        domain.id,
        "chantier",
        data=data,
    )

    assert record.data["kind"] == kind


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda data: data.pop("title"), "title.*required"),
        (lambda data: data.update(kind="project"), "kind.*one of"),
        (lambda data: data.update(state="todo"), "state.*one of"),
        (lambda data: data.update(slug="Not Kebab"), "slug.*invalid format"),
        (lambda data: data.update(goal="Coordinate all member tickets."), "goal.*invalid format"),
        (lambda data: data.update(refs={"source": "doc", "ref": "x"}), "refs.*array"),
        (lambda data: data.update(refs=["doc:x"]), "refs.*object"),
        (
            lambda data: data.update(refs=[{"source": "jira", "ref": "PROJ-1"}]),
            "refs.*one of",
        ),
        (lambda data: data.update(refs=[{"source": "doc"}]), "refs.*requires.*ref"),
        (
            lambda data: data.update(refs=[{"source": "doc", "ref": "x", "extra": True}]),
            "refs.*unknown key.*extra",
        ),
        (
            lambda data: data.update(refs=[{"source": "doc", "ref": "x", "title": ""}]),
            "refs.*at least 1",
        ),
        (
            lambda data: data.update(refs=[{"source": "github", "ref": "#326"}]),
            "refs.*invalid format.*github",
        ),
        (
            lambda data: data.update(parent_issue="https://github.com/Illospace/illospace/issues/326"),
            "parent_issue.*invalid format",
        ),
        (lambda data: data.update(next_step="First sentence.\nSecond sentence."), "next_step.*invalid format"),
        (lambda data: data.update(owner="x" * 121), "owner.*at most 120"),
        (lambda data: data.update(created_at="yesterday"), "created_at.*ISO datetime"),
    ],
)
async def test_chantier_contract_rejects_invalid_records(session, mutate, match):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    data = _chantier_record_data()
    mutate(data)

    with pytest.raises(DomainError, match=match):
        await service.create_record(ORG_ID, domain.id, "chantier", data=data)


async def test_chantier_contract_rejects_vaporware_placeholder_pattern(session):
    from brain.systems.chantiers import MISSING_NEXT_STEP, placeholder_goal

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    data = _chantier_record_data()
    data.update(
        slug="empty-placeholder",
        title="Empty placeholder",
        goal=placeholder_goal("Empty placeholder"),
        owner=None,
        refs=[],
        parent_issue=None,
        next_step=MISSING_NEXT_STEP,
        progress_note=None,
    )

    with pytest.raises(DomainError, match="placeholder records are rejected"):
        await service.create_record(ORG_ID, domain.id, "chantier", data=data)

    assert await service.list_records(ORG_ID, domain.id, object_key="chantier") == []


async def test_chantier_slug_is_stable_after_creation(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    created = await service.create_record(
        ORG_ID,
        domain.id,
        "chantier",
        data=_chantier_record_data(),
    )

    with pytest.raises(DomainError, match="slug.*immutable"):
        await service.update_record(
            ORG_ID,
            domain.id,
            created.id,
            data_patch={"slug": "renamed-keystone"},
        )


async def test_domain_field_validation_contract_must_be_an_object(session):
    service = AsyncDomainService(session)

    with pytest.raises(DomainError, match="validation must be an object"):
        await service.create_domain(
            ORG_ID,
            name="Invalid field contract",
            objects=[
                {
                    "key": "item",
                    "fields": [
                        {
                            "key": "title",
                            "field_type": "text",
                            "validation": ["not", "an", "object"],
                        }
                    ],
                }
            ],
        )


async def test_manage_domain_round_trips_a_chantier_record(session, monkeypatch):
    from brain.platform.db.repositories import unit_of_work
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.domains import _handle_manage_domain

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )

    class SessionUnitOfWork:
        def __init__(self, *args, **kwargs):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await session.flush()
            return False

    monkeypatch.setattr(unit_of_work, "UnitOfWork", SessionUnitOfWork)
    data = _chantier_record_data()
    with bind_agent_context({"org_id": ORG_ID, "user_id": USER_ID}):
        created = json.loads(
            await _handle_manage_domain(
                action="create_record",
                domain_id=domain.id,
                object_key="chantier",
                data=data,
            )
        )["record"]
        updated = json.loads(
            await _handle_manage_domain(
                action="update_record",
                domain_id=domain.id,
                record_id=created["id"],
                data_patch={
                    "state": "shipping",
                    "next_step": "Merge the schema migration after checks pass.",
                },
                expected_version=1,
            )
        )["record"]
        queried = json.loads(
            await _handle_manage_domain(
                action="query_records",
                domain_id=domain.id,
                object_key="chantier",
                search="keystone",
            )
        )

    assert created["object_key"] == "chantier"
    assert created["data"] == data
    assert updated["version"] == 2
    assert updated["data"]["state"] == "shipping"
    assert queried["returned"] == queried["total_matching"] == 1
    assert queried["records"][0]["id"] == created["id"]


async def test_manage_domain_create_is_a_proposal_without_explicit_schema_confirmation(
    session,
    monkeypatch,
):
    from brain.platform.db.repositories import unit_of_work
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.domains import _handle_manage_domain

    class UnexpectedUnitOfWork:
        def __init__(self, *args, **kwargs):
            pytest.fail("proposal-only create_domain must not open a write unit of work")

    monkeypatch.setattr(unit_of_work, "UnitOfWork", UnexpectedUnitOfWork)

    with bind_agent_context({"org_id": ORG_ID, "user_id": USER_ID}):
        payload = json.loads(
            await _handle_manage_domain(
                action="create_domain",
                name="Customer Support Tickets",
                objects=[
                    {
                        "key": "ticket",
                        "fields": [{"key": "title", "field_type": "text"}],
                    }
                ],
            )
        )

    assert payload["status"] == "proposal"
    assert payload["created"] is False
    assert payload["requires_confirmation"] is True
    assert payload["confirmation_parameter"] == "confirm_schema_change"
    assert payload["proposal"]["name"] == "Customer Support Tickets"
    assert "filing side effect" in payload["message"]
    assert await AsyncDomainService(session).list_domains(ORG_ID) == []


async def test_manage_domain_confirmed_create_preserves_authorized_path_and_typed_errors(
    session,
    monkeypatch,
):
    from brain.platform.db.repositories import unit_of_work
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.domains import _handle_manage_domain

    class SessionUnitOfWork:
        def __init__(self, *args, **kwargs):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await session.flush()
            return False

    monkeypatch.setattr(unit_of_work, "UnitOfWork", SessionUnitOfWork)

    with bind_agent_context({"org_id": ORG_ID, "user_id": USER_ID}):
        created = json.loads(
            await _handle_manage_domain(
                action="create_domain",
                name="Explicitly Requested CRM",
                confirm_schema_change=True,
            )
        )
        invalid = json.loads(
            await _handle_manage_domain(
                action="add_object",
                domain_id=created["domain"]["id"],
                object_key="contact",
                fields=[{"key": "name", "field_type": "string"}],
            )
        )

    assert created["domain"]["name"] == "Explicitly Requested CRM"
    assert invalid["error_code"] == "invalid_field_type"
    assert invalid["field"] == "field_type"
    assert invalid["received"] == "string"
    assert "text" in invalid["allowed_values"]
    assert "Unsupported field_type 'string'" in invalid["error"]


def test_merge_chantier_tool_is_registered_as_a_versioned_domain_write():
    from brain.systems.runs.tool_catalog.definitions.domain_inbound import DOMAIN_TOOLS
    from brain.systems.runs.tool_catalog.registry import get_tool_registration
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS, WORKER_TOOLS
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    definition = next(tool for tool in DOMAIN_TOOLS if tool["name"] == "merge_chantier")
    assert definition["input_schema"]["required"] == [
        "duplicate_record_id",
        "canonical_record_id",
        "expected_duplicate_version",
        "expected_canonical_version",
        "reason",
    ]
    assert "merge_chantier" in {tool["name"] for tool in COORDINATOR_TOOLS}
    assert "merge_chantier" in {tool["name"] for tool in WORKER_TOOLS}
    assert "merge_chantier" in _get_tool_handlers()
    registration = get_tool_registration("merge_chantier")
    assert registration is not None
    assert registration.permission == "write_domain"
    assert registration.action_manifest is True


def test_merge_chantier_refs_folds_duplicate_evidence_without_duplicates():
    from brain.systems.chantiers import merge_chantier_refs

    canonical_ref = {"source": "github", "ref": "github:Illospace/illospace:issue:386"}
    duplicate_ref = {"source": "url", "ref": "https://example.com/duplicate-prd"}

    assert merge_chantier_refs(
        [canonical_ref],
        [canonical_ref, duplicate_ref],
    ) == [canonical_ref, duplicate_ref]


async def test_merge_chantier_2096_into_1993_removes_duplicate_from_digest_set(
    session,
    monkeypatch,
):
    from brain.platform.db.repositories import unit_of_work
    from brain.systems.chantiers import (
        MISSING_NEXT_STEP,
        active_chantier_records,
        is_placeholder_chantier,
        placeholder_goal,
    )
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.domains import _handle_merge_chantier

    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="GitHub Ticket Tracker",
        slug="github-ticket-tracker",
        objects=[_chantier_object_definition()],
    )
    object_type = await service.get_object_type(domain.id, "chantier")
    fields = await service.list_fields(object_type.id)

    async def seed_record(record_id: int, data: dict) -> DomainRecord:
        normalized = service.validate_record_data(fields, data)
        record = DomainRecord(
            id=record_id,
            org_id=ORG_ID,
            domain_id=domain.id,
            object_type_id=object_type.id,
            title=data["title"],
            data=normalized,
            search_text=data["title"],
        )
        session.add(record)
        await session.flush()
        return record

    for record_id, slug in (
        (1990, "coordinator-reliability"),
        (1991, "clothing-intake"),
        (1992, "shopify-sunset"),
    ):
        data = _chantier_record_data()
        data.update(slug=slug, title=slug.replace("-", " ").title())
        await seed_record(record_id, data)

    canonical_data = _chantier_record_data()
    canonical_data.update(
        slug="agent-mcp-repositioning",
        title="Agent MCP Repositioning",
        goal="Done means the agent MCP product position is canonical.",
    )
    canonical = await seed_record(1993, canonical_data)

    duplicate_title = "V3 canonical agent MCP repositioning automation builder chat"
    duplicate_data = _chantier_record_data()
    duplicate_data.update(
        slug="v3-canonical-agent-mcp-repositioning-automation-builder-chat",
        title=duplicate_title,
        goal=placeholder_goal(duplicate_title),
        state="exploring",
        owner=None,
        refs=[],
        parent_issue=None,
        next_step=MISSING_NEXT_STEP,
        progress_note=None,
    )
    duplicate = await seed_record(2096, duplicate_data)

    class SessionUnitOfWork:
        def __init__(self, *args, **kwargs):
            self.session = session

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await session.flush()
            return False

    monkeypatch.setattr(unit_of_work, "UnitOfWork", SessionUnitOfWork)
    with bind_agent_context({"org_id": ORG_ID, "user_id": USER_ID}):
        merged = json.loads(
            await _handle_merge_chantier(
                duplicate_record_id=2096,
                canonical_record_id=1993,
                expected_duplicate_version=1,
                expected_canonical_version=1,
                reason="Issue #386 duplicate retirement",
            )
        )
        repeated = json.loads(
            await _handle_merge_chantier(
                duplicate_record_id=2096,
                canonical_record_id=1993,
                expected_duplicate_version=1,
                expected_canonical_version=1,
                reason="Issue #386 duplicate retirement retry",
            )
        )

    assert merged["status"] == "merged"
    assert merged["canonical"]["id"] == canonical.id == 1993
    assert merged["duplicate"]["id"] == duplicate.id == 2096
    assert merged["duplicate"]["data"]["state"] == "paused"
    assert merged["duplicate"]["data"]["superseded_by"] == "agent-mcp-repositioning"
    assert merged["active_chantier_count"] == 4
    assert set(merged["digest_record_ids"]) == {1990, 1991, 1992, 1993}
    assert repeated["status"] == "already_merged"

    records = await service.list_records(ORG_ID, domain.id, object_key="chantier")
    digest_records = active_chantier_records(records)
    assert {record.id for record in digest_records} == {1990, 1991, 1992, 1993}
    assert 2096 not in {record.id for record in digest_records}
    assert not any(is_placeholder_chantier(record.data) for record in digest_records)


async def test_domain_events_drop_non_uuid_idea_ids(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Tickets",
        objects=[
            {
                "key": "ticket",
                "fields": [{"key": "title", "field_type": "text", "required": True}],
            }
        ],
    )

    await service.create_record(
        ORG_ID,
        domain.id,
        "ticket",
        data={"title": "Persist from inbound run"},
        run_id=123,
        idea_id="inbound:abc",
    )

    events = await service.list_events(ORG_ID, domain.id)
    created = next(event for event in events if event.event_type == "record.created")
    assert created.run_id == 123
    assert created.idea_id is None


async def test_record_validation_rejects_missing_unknown_and_bad_enum(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Tasks",
        objects=[
            {
                "key": "task",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "state", "field_type": "enum", "options": ["open", "done"]},
                ],
            }
        ],
    )

    with pytest.raises(DomainError, match="title"):
        await service.create_record(ORG_ID, domain.id, "task", data={})

    with pytest.raises(DomainError, match="Unknown field"):
        await service.create_record(ORG_ID, domain.id, "task", data={"title": "One", "bogus": 1})

    with pytest.raises(DomainError, match="state"):
        await service.create_record(
            ORG_ID,
            domain.id,
            "task",
            data={"title": "One", "state": "later"},
        )


async def test_update_record_uses_expected_version_and_archives_or_deletes(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Todos",
        objects=[
            {
                "key": "todo",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "done", "field_type": "boolean", "default_value": False},
                ],
            }
        ],
    )
    record = await service.create_record(
        ORG_ID,
        domain.id,
        "todo",
        data={"title": "Ship domains"},
    )

    updated = await service.update_record(
        ORG_ID,
        domain.id,
        record.id,
        data_patch={"done": True},
        expected_version=1,
    )
    assert updated.version == 2
    assert updated.data["done"] is True

    with pytest.raises(DomainError, match="version mismatch"):
        await service.update_record(
            ORG_ID,
            domain.id,
            record.id,
            data_patch={"done": False},
            expected_version=1,
        )

    archived = await service.remove_record(ORG_ID, domain.id, record.id, mode="archive")
    assert archived["archived"] is True
    assert (await service.get_record(ORG_ID, domain.id, record.id)).archived_at is not None

    second = await service.create_record(
        ORG_ID,
        domain.id,
        "todo",
        data={"title": "Delete me"},
    )
    deleted = await service.remove_record(ORG_ID, domain.id, second.id, mode="delete")
    assert deleted["deleted"] is True
    assert await session.get(DomainRecord, second.id) is None


async def test_domain_slug_edges_and_archive_or_delete(session):
    service = AsyncDomainService(session)
    short = await service.create_domain(ORG_ID, name="X")
    assert short.slug == "x"

    long = await service.create_domain(ORG_ID, name="Very " * 30 + "Long Domain")
    assert len(long.slug) <= 80
    assert not long.slug.endswith("-")

    archived = await service.remove_domain(ORG_ID, short.id, mode="archive")
    assert archived["archived"] is True
    assert short not in await service.list_domains(ORG_ID)
    assert (await service.get_domain(ORG_ID, short.id, include_archived=True)).archived_at is not None

    deleted = await service.remove_domain(ORG_ID, long.id, mode="delete")
    assert deleted["deleted"] is True
    assert await session.get(Domain, long.id) is None


async def test_relations_validate_object_types(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Hooks",
        objects=[
            {"key": "hook", "fields": [{"key": "text", "field_type": "text", "required": True}]},
            {"key": "trial", "fields": [{"key": "name", "field_type": "text", "required": True}]},
        ],
        relations=[
            {
                "key": "trial_tests_hook",
                "source_object": "trial",
                "target_object": "hook",
                "cardinality": "many_to_one",
            }
        ],
    )
    hook = await service.create_record(ORG_ID, domain.id, "hook", data={"text": "Pain first"})
    trial = await service.create_record(ORG_ID, domain.id, "trial", data={"name": "TikTok A"})

    relation = await service.create_relation(
        ORG_ID,
        domain.id,
        "trial_tests_hook",
        source_record_id=trial.id,
        target_record_id=hook.id,
    )
    assert (await service.serialize_relation(relation))["relation_key"] == "trial_tests_hook"
    assert (await service.list_relations(ORG_ID, domain.id, source_record_id=trial.id))[0].id == relation.id
    assert (await service.list_relations(ORG_ID, domain.id, target_record_id=hook.id))[0].id == relation.id
    assert (await service.remove_relation(ORG_ID, domain.id, relation.id))["archived"] is True
    assert await service.list_relations(ORG_ID, domain.id) == []
    assert (await service.list_relations(ORG_ID, domain.id, include_archived=True))[0].id == relation.id
    assert (await service.list_events(ORG_ID, domain.id, limit=2))[0].event_type == "relation.archived"

    with pytest.raises(DomainError, match="source_record_id"):
        await service.create_relation(
            ORG_ID,
            domain.id,
            "trial_tests_hook",
            source_record_id=hook.id,
            target_record_id=trial.id,
        )


async def test_serialize_record_compact_projects_fields_in_caller_order_and_trims(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Compact Records",
        objects=[
            {
                "key": "ticket",
                "title_field": "title",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "status", "field_type": "text"},
                    {"key": "details", "field_type": "long_text"},
                    {"key": "metadata", "field_type": "json"},
                ],
            }
        ],
    )
    metadata = {"notes": "m" * 250}
    record = await service.create_record(
        ORG_ID,
        domain.id,
        "ticket",
        data={
            "title": "Compact me",
            "status": "open",
            "details": "d" * 250,
            "metadata": metadata,
        },
    )

    compact = await service.serialize_record_compact(
        record,
        fields=["missing", "metadata", "details", "status"],
    )

    assert list(compact) == ["id", "object_key", "title", "version", "updated_at", "data"]
    assert compact["object_key"] == "ticket"
    assert list(compact["data"]) == ["metadata", "details", "status"]
    assert compact["data"]["metadata"] == f"{json.dumps(metadata, default=str)[:200]}…"
    assert compact["data"]["details"] == f"{'d' * 200}…"
    assert compact["data"]["status"] == "open"
    assert "archived_at" not in compact
    assert (await service.serialize_record_compact(record, fields=[]))["data"] == {}


async def test_serialize_record_compact_defaults_to_short_scalars(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Compact Defaults",
        objects=[
            {
                "key": "item",
                "title_field": "short_text",
                "fields": [
                    {"key": "short_text", "field_type": "text", "required": True},
                    {"key": "boundary_text", "field_type": "long_text"},
                    {"key": "long_text", "field_type": "long_text"},
                    {"key": "count", "field_type": "number"},
                    {"key": "ratio", "field_type": "number"},
                    {"key": "enabled", "field_type": "boolean"},
                    {"key": "nothing", "field_type": "text"},
                    {"key": "metadata", "field_type": "json"},
                ],
            }
        ],
    )
    record = await service.create_record(
        ORG_ID,
        domain.id,
        "item",
        data={
            "short_text": "short",
            "boundary_text": "b" * 120,
            "long_text": "l" * 121,
            "count": 7,
            "ratio": 1.5,
            "enabled": True,
            "metadata": ["nested"],
        },
    )

    compact = await service.serialize_record_compact(record)

    assert compact["data"] == {
        "short_text": "short",
        "boundary_text": "b" * 120,
        "count": 7,
        "ratio": 1.5,
        "enabled": True,
        "nothing": None,
    }


async def test_list_records_supports_oldest_first_order_and_rejects_invalid_order(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Ordered Records",
        objects=[
            {
                "key": "item",
                "title_field": "title",
                "fields": [{"key": "title", "field_type": "text", "required": True}],
            }
        ],
    )
    newest = await service.create_record(ORG_ID, domain.id, "item", data={"title": "Newest"})
    oldest = await service.create_record(ORG_ID, domain.id, "item", data={"title": "Oldest"})
    middle = await service.create_record(ORG_ID, domain.id, "item", data={"title": "Middle"})
    oldest.updated_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    middle.updated_at = datetime(2024, 1, 2, tzinfo=timezone.utc)
    newest.updated_at = datetime(2024, 1, 3, tzinfo=timezone.utc)
    await session.flush()

    records = await service.list_records(ORG_ID, domain.id, order="updated_asc")

    assert [record.id for record in records] == [oldest.id, middle.id, newest.id]
    with pytest.raises(DomainError, match="updated_desc.*updated_asc"):
        await service.list_records(ORG_ID, domain.id, order="oldest_first")


async def test_count_records_matches_list_scope_without_limit_or_order(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Counted Records",
        objects=[
            {
                "key": "ticket",
                "title_field": "title",
                "fields": [{"key": "title", "field_type": "text", "required": True}],
            },
            {
                "key": "note",
                "title_field": "title",
                "fields": [{"key": "title", "field_type": "text", "required": True}],
            },
        ],
    )
    await service.create_record(ORG_ID, domain.id, "ticket", data={"title": "Alpha ticket"})
    await service.create_record(ORG_ID, domain.id, "ticket", data={"title": "Beta ticket"})
    await service.create_record(ORG_ID, domain.id, "note", data={"title": "Alpha note"})
    archived = await service.create_record(
        ORG_ID,
        domain.id,
        "ticket",
        data={"title": "Alpha archived"},
    )
    await service.remove_record(ORG_ID, domain.id, archived.id, mode="archive")

    records = await service.list_records(ORG_ID, domain.id)
    assert await service.count_records(ORG_ID, domain.id) == len(records) == 3
    ticket_records = await service.list_records(ORG_ID, domain.id, object_key="ticket")
    assert await service.count_records(ORG_ID, domain.id, object_key="ticket") == len(ticket_records) == 2
    alpha_tickets = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="ticket",
        search=" alpha ",
    )
    assert await service.count_records(
        ORG_ID,
        domain.id,
        object_key="ticket",
        search=" alpha ",
    ) == len(alpha_tickets) == 1
    all_alpha_tickets = await service.list_records(
        ORG_ID,
        domain.id,
        object_key="ticket",
        search="alpha",
        include_archived=True,
    )
    assert await service.count_records(
        ORG_ID,
        domain.id,
        object_key="ticket",
        search="alpha",
        include_archived=True,
    ) == len(all_alpha_tickets) == 2
    assert len(await service.list_records(ORG_ID, domain.id, limit=1)) == 1
    assert await service.count_records(ORG_ID, domain.id) == 3


async def test_record_and_event_list_offsets_fetch_following_pages(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Paginated Tracker",
        objects=[
            {
                "key": "ticket",
                "title_field": "title",
                "fields": [{"key": "title", "field_type": "text", "required": True}],
            }
        ],
    )
    created = [
        await service.create_record(
            ORG_ID,
            domain.id,
            "ticket",
            data={"title": f"Ticket {index}"},
        )
        for index in range(5)
    ]

    first_records = await service.list_records(ORG_ID, domain.id, limit=2)
    second_records = await service.list_records(ORG_ID, domain.id, limit=2, offset=2)
    third_records = await service.list_records(ORG_ID, domain.id, limit=2, offset=4)
    assert [record.id for record in first_records + second_records + third_records] == [
        record.id for record in reversed(created)
    ]

    all_events = await service.list_events(ORG_ID, domain.id, limit=20)
    paged_events = []
    for offset in range(0, len(all_events), 2):
        paged_events.extend(
            await service.list_events(ORG_ID, domain.id, limit=2, offset=offset)
        )
    assert [event.id for event in paged_events] == [event.id for event in all_events]
    assert await service.count_events(ORG_ID, domain.id) == len(all_events)
