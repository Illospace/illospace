from __future__ import annotations

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
