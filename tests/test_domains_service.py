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
