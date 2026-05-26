from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sqlalchemy import func, select
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
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.workspace_app import WorkspaceApp, WorkspaceAppState, WorkspaceAppVersion
from brain.systems.user_domains.service import AsyncDomainService
from brain.systems.workspace_apps.service import (
    WorkspaceAppError,
    WorkspaceAppContractError,
    a_active_version,
    a_archive_app,
    a_create_app,
    a_delete_archived_apps,
    a_list_archived_apps,
    a_restore_app,
    a_update_app,
)
from brain.systems.workspace_apps.bindings import WorkspaceAppBindingError, async_run_workspace_app_binding
from brain.systems.workspace_apps.actions import (
    WorkspaceAppActionContractError,
    WorkspaceAppActionExecutorMissing,
    async_run_workspace_app_action,
    register_workspace_app_action_executor,
    unregister_workspace_app_action_executor,
)

ORG_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ORG_ID = "33333333-3333-4333-8333-333333333333"
USER_ID = "22222222-2222-4222-8222-222222222222"
VALID_SOURCE = """
<main class="illo-app">
  <section class="illo-panel illo-stack">
    <h1 class="illo-title">Todo Notes</h1>
  </section>
</main>
"""
VALID_VISUAL_SPEC = {
    "thumbnail": {
        "label": "Todo",
        "value": "Live",
        "secondary": "Domain-backed",
    }
}

VALID_GENERATED_UI_SPEC = {
    "schema_version": 1,
    "title": "Todo Notes",
    "primary_binding": "todos",
    "views": [
        {
            "id": "todos",
            "type": "table",
            "title": "Todo items",
            "binding": "todos",
            "columns": [
                {"key": "title", "label": "Title"},
                {"key": "completed", "label": "Done", "type": "boolean", "editable": True},
            ],
        }
    ],
}


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
    db = await async_sqlite_session_factory([
        Org.__table__,
        User.__table__,
        Domain.__table__,
        DomainObjectType.__table__,
        DomainFieldDefinition.__table__,
        DomainRelationType.__table__,
        DomainRecord.__table__,
        DomainRelation.__table__,
        DomainEvent.__table__,
        WorkspaceApp.__table__,
        WorkspaceAppVersion.__table__,
        WorkspaceAppState.__table__,
    ])
    db.add_all([
        Org(id=ORG_ID, name="Test Org", slug="test"),
        Org(id=OTHER_ORG_ID, name="Other Org", slug="other"),
        User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@example.test"),
    ])
    await db.flush()
    return db


async def _todo_domain(session):
    return await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Todo Notes",
        slug="todo-notes",
        objects=[
            {
                "key": "todo_item",
                "name": "Todo Item",
                "title_field": "title",
                "fields": [
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "notes", "field_type": "text"},
                    {"key": "completed", "field_type": "boolean", "default_value": False},
                ],
            }
        ],
        actor_id=USER_ID,
    )


def _manifest(domain_id_value: int, **overrides):
    binding = {
        "domain_id": domain_id_value,
        "domain_slug": "todo-notes",
        "object_key": "todo_item",
        "fields": ["title", "notes", "completed"],
        "operations": ["schema", "list", "create", "update", "archive"],
    }
    binding.update(overrides)
    return {
        "contract_version": 1,
        "data_plan": {"mode": "domain", "bindings": {"todos": binding}},
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
    }


def _capsule_manifest(domain_id_value: int, **overrides):
    binding = {
        "kind": "domain",
        "domain_id": domain_id_value,
        "domain_slug": "todo-notes",
        "object_key": "todo_item",
        "fields": ["title", "notes", "completed"],
        "operations": ["schema", "list", "query", "get", "create", "update", "archive", "aggregate"],
    }
    binding.update(overrides)
    return {
        "contract_version": 1,
        "data_plan": {"mode": "capability", "bindings": {"todos": binding}},
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
    }


def _manifest_with_action(domain_id_value: int, action: dict | None = None):
    manifest = _manifest(domain_id_value)
    manifest["actions"] = {
        "tickets.syncExternal": action
        or {
            "kind": "connector",
            "description": "Sync one Domain ticket with the configured external ticketing system.",
            "effects": ["domain.read", "domain.write", "external.read", "external.write"],
            "connectors": [{"key": "ticketing", "provider": "configured_ticketing_system", "auth": "project_vault_binding"}],
            "executor": {"type": "deferred"},
        }
    }
    return manifest


def _app_local_manifest(**overrides):
    manifest = {
        "contract_version": 1,
        "state_key": "scratchpad",
        "data_plan": {"mode": "app_local", "scope": "draft"},
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
    }
    manifest.update(overrides)
    return manifest


async def _count(session, model, *criteria) -> int:
    stmt = select(func.count()).select_from(model)
    if criteria:
        stmt = stmt.where(*criteria)
    return int(await session.scalar(stmt))


async def _serialize_record_for_connector_test(_service, record):
    return {
        "id": record.id,
        "title": record.title,
        "data": record.data or {},
        "version": record.version,
    }


async def test_valid_domain_backed_manifest_saves(session):
    domain = await _todo_domain(session)

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-notes",
        name="Todo Notes",
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    version = await a_active_version(session, app.id)
    assert version is not None
    assert version.manifest["data_plan"]["bindings"]["todos"]["object_key"] == "todo_item"


async def test_domain_binding_accepts_generic_app_primitives(session):
    domain = await _todo_domain(session)
    operations = [
        "schema",
        "list",
        "query",
        "get",
        "create",
        "update",
        "archive",
        "aggregate",
        "bulkUpdate",
        "history",
        "listRelations",
        "createRelation",
        "archiveRelation",
    ]

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-workbench",
        name="Todo Workbench",
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_manifest(domain.id, operations=operations),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    version = await a_active_version(session, app.id)
    assert version is not None
    assert version.manifest["data_plan"]["bindings"]["todos"]["operations"] == operations


async def test_app_capsule_capability_manifest_saves(session):
    domain = await _todo_domain(session)

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-capsule",
        name="Todo Capsule",
        renderer_key="app-capsule",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_capsule_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    version = await a_active_version(session, app.id)
    assert version is not None
    assert version.renderer_key == "app-capsule"
    assert version.source_kind == "html"
    assert version.manifest["data_plan"]["mode"] == "capability"


async def test_workspace_app_binding_broker_routes_domain_operations(session):
    domain = await _todo_domain(session)
    record = await AsyncDomainService(session).create_record(
        ORG_ID,
        domain.id,
        "todo_item",
        data={"title": "Follow up", "notes": "CRM lead", "completed": False},
        actor_id=USER_ID,
    )
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-capsule-broker",
        name="Todo Capsule Broker",
        renderer_key="app-capsule",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_capsule_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    listed = await async_run_workspace_app_binding(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        alias="todos",
        operation="list",
        payload={},
        user_id=USER_ID,
    )
    assert listed["kind"] == "domain"
    assert [item["id"] for item in listed["data"]] == [record.id]

    created = await async_run_workspace_app_binding(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        alias="todos",
        operation="create",
        payload={"data": {"title": "Send note", "completed": False}},
        user_id=USER_ID,
        can_write=True,
    )
    assert created["data"]["title"] == "Send note"

    updated = await async_run_workspace_app_binding(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        alias="todos",
        operation="update",
        payload={"recordId": created["data"]["id"], "dataPatch": {"completed": True}},
        user_id=USER_ID,
        can_write=True,
    )
    assert updated["data"]["data"]["completed"] is True

    aggregated = await async_run_workspace_app_binding(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        alias="todos",
        operation="aggregate",
        payload={"groupBy": "completed"},
        user_id=USER_ID,
    )
    assert aggregated["data"]["total"] == 2
    assert {group["label"]: group["count"] for group in aggregated["data"]["groups"]} == {"False": 1, "True": 1}


async def test_workspace_app_binding_broker_rejects_disallowed_or_cross_org_access(session):
    domain = await _todo_domain(session)
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-capsule-guarded",
        name="Todo Capsule Guarded",
        renderer_key="app-capsule",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_capsule_manifest(domain.id, operations=["list"]),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with pytest.raises(WorkspaceAppBindingError, match="does not allow operation 'create'"):
        await async_run_workspace_app_binding(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            alias="todos",
            operation="create",
            payload={"data": {"title": "Blocked"}},
            user_id=USER_ID,
            can_write=True,
        )

    with pytest.raises(WorkspaceAppError, match="Workspace app not found"):
        await async_run_workspace_app_binding(
            session,
            org_id=OTHER_ORG_ID,
            app_id=app.id,
            alias="todos",
            operation="list",
            payload={},
            user_id=USER_ID,
        )


async def test_workspace_app_binding_broker_routes_system_reads(session):
    manifest = {
        "contract_version": 1,
        "data_plan": {
            "mode": "capability",
            "bindings": {
                "activity": {
                    "kind": "system",
                    "source": "activity",
                    "operations": ["schema", "query", "aggregate"],
                }
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
    }
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="activity-capsule",
        name="Activity Capsule",
        renderer_key="app-capsule",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=manifest,
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    expected = {"sources": {"activity": [{"id": "item-1"}]}, "counts": {"activity": 1}, "total_count": 1}
    with patch("brain.systems.workspace_apps.bindings.query_system_binding_source", AsyncMock(return_value=expected)) as query:
        result = await async_run_workspace_app_binding(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            alias="activity",
            operation="query",
            payload={"search": "handoff", "limit": 10},
            user_id=USER_ID,
        )

    assert result["kind"] == "system"
    assert result["data"] == expected
    assert query.await_args.kwargs["source"] == "activity"
    assert query.await_args.kwargs["org_id"] == ORG_ID
    assert query.await_args.kwargs["user_id"] == USER_ID
    assert query.await_args.kwargs["search"] == "handoff"


async def test_valid_structured_generated_ui_app_saves(session):
    domain = await _todo_domain(session)

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-notes-ui",
        name="Todo Notes UI",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    version = await a_active_version(session, app.id)
    assert version is not None
    assert version.renderer_key == "generated-ui-app"
    assert version.source_kind == "json"
    assert json.loads(version.source_code)["views"][0]["type"] == "table"


async def test_valid_structured_board_generated_ui_app_saves(session):
    domain = await _todo_domain(session)
    board_spec = {
        "schema_version": 1,
        "title": "Todo Board",
        "primary_binding": "todos",
        "actions": [{"key": "tickets.syncExternal", "label": "Sync external"}],
        "views": [
            {
                "id": "todo-board",
                "type": "board",
                "title": "Todo board",
                "binding": "todos",
                "group_by": "completed",
                "groups": [
                    {"label": "Open", "value": False},
                    {"label": "Done", "value": True},
                ],
                "card": {"title": "title", "badges": ["notes"]},
            }
        ],
    }

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-board-ui",
        name="Todo Board UI",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(board_spec),
        manifest=_manifest_with_action(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    version = await a_active_version(session, app.id)
    assert version is not None
    assert json.loads(version.source_code)["actions"][0]["key"] == "tickets.syncExternal"
    assert json.loads(version.source_code)["views"][0]["type"] == "board"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"domain_id": None}, "requires domain_id"),
        ({"object_key": ""}, "requires object_key"),
        ({"domain_id": 999}, "missing Domain 999"),
        ({"object_key": "missing_item"}, "missing object_key 'missing_item'"),
        ({"fields": ["title", "missing_field"]}, "missing field"),
        ({"fields": ["notes", "completed"]}, "omits required field"),
        ({"operations": ["schema", "teleport"]}, "unsupported operation"),
    ],
)
async def test_invalid_domain_bindings_block_save(session, overrides, message):
    domain = await _todo_domain(session)

    with pytest.raises(WorkspaceAppError, match=message):
        await a_create_app(
            session,
            org_id=ORG_ID,
            key=f"bad-{message.split()[0]}",
            name="Broken Todo Notes",
            source_code="<main></main>",
            manifest=_manifest(domain.id, **overrides),
        )


async def test_domain_binding_allows_virtual_record_title_field(session):
    domain = await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Ticket Board",
        slug="ticket-board",
        objects=[
            {
                "key": "ticket",
                "name": "Ticket",
                "fields": [
                    {"key": "status", "field_type": "enum", "options": ["Backlog", "Done"], "required": True},
                    {"key": "priority", "field_type": "text"},
                ],
            }
        ],
        actor_id=USER_ID,
    )

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="ticket-board",
        name="Ticket Board",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(
            {
                "schema_version": 1,
                "title": "Ticket Board",
                "primary_binding": "tickets",
                "views": [
                    {
                        "id": "tickets",
                        "type": "table",
                        "title": "Tickets",
                        "binding": "tickets",
                        "columns": [
                            {"key": "title", "label": "Title"},
                            {"key": "status", "label": "Status"},
                            {"key": "priority", "label": "Priority"},
                        ],
                    }
                ],
            }
        ),
        manifest={
            "contract_version": 1,
            "data_plan": {
                "mode": "domain",
                "bindings": {
                    "tickets": {
                        "domain_id": domain.id,
                        "object_key": "ticket",
                        "fields": ["title", "status", "priority"],
                        "operations": ["schema", "list", "create", "update"],
                    }
                },
            },
            "design_contract": {
                "kit": "constellation-app-kit",
                "theme_modes": ["dark", "light"],
            },
        },
        visual_spec=VALID_VISUAL_SPEC,
    )

    assert await a_active_version(session, app.id) is not None


async def test_domain_binding_blocks_archived_or_cross_org_domain(session):
    domain = await _todo_domain(session)
    domain.archived_at = datetime.now(timezone.utc)

    with pytest.raises(WorkspaceAppError, match="archived Domain"):
        await a_create_app(
            session,
            org_id=ORG_ID,
            key="archived-domain",
            name="Archived Domain App",
            source_code="<main></main>",
            manifest=_manifest(domain.id),
        )

    other_domain = await AsyncDomainService(session).create_domain(
        OTHER_ORG_ID,
        name="Other Todo Notes",
        slug="todo-notes",
        objects=[
            {
                "key": "todo_item",
                "fields": [{"key": "title", "field_type": "text", "required": True}],
            }
        ],
    )
    with pytest.raises(WorkspaceAppError, match=f"missing Domain {other_domain.id}"):
        await a_create_app(
            session,
            org_id=ORG_ID,
            key="cross-org-domain",
            name="Cross Org Domain App",
            source_code="<main></main>",
            manifest=_manifest(other_domain.id),
        )


async def test_local_state_manifest_without_domain_bindings_still_saves(session):
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="scratchpad",
        name="Scratchpad",
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_app_local_manifest(),
        visual_spec=VALID_VISUAL_SPEC,
        initial_state={"draft_text": ""},
    )

    version = await a_active_version(session, app.id)
    assert version.manifest == _app_local_manifest()


async def test_app_local_note_collections_are_record_like(session):
    with pytest.raises(WorkspaceAppError, match="record-like collections"):
        await a_create_app(
            session,
            org_id=ORG_ID,
            key="local-notes",
            name="Local Notes",
            source_code=VALID_SOURCE,
            manifest=_app_local_manifest(),
            visual_spec=VALID_VISUAL_SPEC,
            initial_state={"notes": []},
        )


async def test_update_validates_effective_domain_manifest(session):
    domain = await _todo_domain(session)
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="todo-notes",
        name="Todo Notes",
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
    )

    await a_update_app(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        source_code='<main class="illo-app"><section class="illo-panel">patched</section></main>',
    )
    version = await a_active_version(session, app.id)
    assert version.version == 2

    with pytest.raises(WorkspaceAppError, match="omits required field"):
        await a_update_app(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            manifest=_manifest(domain.id, fields=["notes"]),
        )


async def test_update_app_refreshes_server_updated_timestamp_before_returning(monkeypatch):
    from brain.systems.workspace_apps import service as workspace_app_service

    refreshed_at = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    app = type(
        "App",
        (),
        {
            "id": "app-1",
            "org_id": ORG_ID,
            "key": "movable-app",
            "name": "Movable App",
            "description": None,
            "renderer_key": "generated-ui-app",
            "visual_spec": {},
            "app_metadata": {},
            "created_by_user_id": USER_ID,
            "anchor_user_id": USER_ID,
            "archived_at": None,
            "created_at": datetime(2026, 5, 23, 11, 0, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 5, 23, 11, 0, tzinfo=timezone.utc),
        },
    )()

    class RefreshRequired:
        def __getattribute__(self, name):
            if name in {"__class__", "__repr__", "__str__"}:
                return object.__getattribute__(self, name)
            raise AssertionError("updated_at was returned before async refresh")

        def __bool__(self):
            raise AssertionError("updated_at was returned before async refresh")

    class RefreshRequiredSession:
        flushed = False
        refreshed = False

        async def flush(self):
            self.flushed = True
            app.updated_at = RefreshRequired()

        async def refresh(self, obj):
            assert obj is app
            self.refreshed = True
            obj.updated_at = refreshed_at

    async def fake_get_app(session, org_id, app_id=None, *, key=None, include_archived=False):
        return app

    async def fake_active_version(session, app_id):
        return None

    monkeypatch.setattr(workspace_app_service, "a_get_app", fake_get_app)
    monkeypatch.setattr(workspace_app_service, "a_active_version", fake_active_version)

    db = RefreshRequiredSession()

    result = await workspace_app_service.a_update_app(
        db,
        org_id=ORG_ID,
        app_id=app.id,
        name="Moved App",
    )

    assert result is app
    assert db.flushed is True
    assert db.refreshed is True
    assert result.updated_at == refreshed_at


async def test_archive_and_restore_app_leave_domain_records_intact(session):
    domain = await _todo_domain(session)
    record = await AsyncDomainService(session).create_record(
        ORG_ID,
        domain.id,
        "todo_item",
        data={"title": "Follow up", "notes": "CRM lead", "completed": False},
        actor_id=USER_ID,
    )
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="crm-list",
        name="CRM List",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    await a_archive_app(session, org_id=ORG_ID, app_id=app.id)
    assert app.archived_at is not None
    archived_apps = await a_list_archived_apps(session, ORG_ID)
    assert archived_apps[0].id == app.id

    service = AsyncDomainService(session)
    persisted_record = await service.get_record(ORG_ID, domain.id, record.id)
    assert persisted_record.archived_at is None

    restored = await a_restore_app(session, org_id=ORG_ID, app_id=app.id)
    assert restored.archived_at is None


async def test_delete_archived_apps_permanently_removes_app_versions_and_state(session):
    domain = await _todo_domain(session)
    archived = await a_create_app(
        session,
        org_id=ORG_ID,
        key="trash-me",
        name="Trash Me",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
        initial_state={"view": "table"},
    )
    active = await a_create_app(
        session,
        org_id=ORG_ID,
        key="keep-me",
        name="Keep Me",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=_manifest(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )
    await a_archive_app(session, org_id=ORG_ID, app_id=archived.id)

    deleted = await a_delete_archived_apps(session, org_id=ORG_ID)

    assert deleted == 1
    assert await _count(session, WorkspaceApp, WorkspaceApp.id == archived.id) == 0
    assert await _count(session, WorkspaceAppVersion, WorkspaceAppVersion.app_id == archived.id) == 0
    assert await _count(session, WorkspaceAppState, WorkspaceAppState.app_id == archived.id) == 0
    assert await _count(session, WorkspaceApp, WorkspaceApp.id == active.id) == 1


async def test_workspace_app_action_requires_registered_executor(session):
    domain = await _todo_domain(session)
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="ticket-actions",
        name="Ticket Actions",
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_manifest_with_action(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with pytest.raises(WorkspaceAppActionExecutorMissing, match="no server-side action executor"):
        await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={"ticketId": 1},
            user_id=USER_ID,
        )


async def test_workspace_app_action_registered_executor_runs(session):
    domain = await _todo_domain(session)
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="ticket-action-runner",
        name="Ticket Action Runner",
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_manifest_with_action(
            domain.id,
            {
                "kind": "connector",
                "description": "Sync one Domain ticket with the configured external ticketing system.",
                "effects": ["domain.read", "domain.write", "external.read", "external.write"],
                "connectors": [{"key": "ticketing", "provider": "configured_ticketing_system"}],
                "executor": {"type": "registered", "key": "ticketing.sync"},
            },
        ),
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    def _executor(context, payload):
        assert context.org_id == ORG_ID
        assert context.action_key == "tickets.syncExternal"
        return {"synced": True, "ticketId": payload["ticketId"]}

    register_workspace_app_action_executor("ticketing.sync", _executor)
    try:
        result = await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={"ticketId": 42},
            user_id=USER_ID,
        )
    finally:
        unregister_workspace_app_action_executor("ticketing.sync")

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["effects"] == ["domain.read", "domain.write", "external.read", "external.write"]
    assert result["connector_keys"] == ["ticketing"]
    assert result["result"] == {"synced": True, "ticketId": 42}


async def test_workspace_app_action_generic_http_syncs_domain_records(session):
    domain = await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Tickets",
        slug="tickets",
        objects=[
            {
                "key": "ticket",
                "name": "Ticket",
                "fields": [
                    {"key": "external_id", "field_type": "text"},
                    {"key": "number", "field_type": "number"},
                    {"key": "status", "field_type": "enum", "options": ["Todo", "Done"]},
                    {"key": "url", "field_type": "url"},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    service = AsyncDomainService(session)
    existing = await service.create_record(
        ORG_ID,
        domain.id,
        "ticket",
        title="Old issue",
        data={"external_id": "123", "number": 123, "status": "Todo", "url": "https://example.test/old"},
        actor_id=USER_ID,
    )
    manifest = {
        "contract_version": 1,
        "data_plan": {
            "mode": "domain",
            "bindings": {
                "tickets": {
                    "domain_id": domain.id,
                    "object_key": "ticket",
                    "fields": ["title", "external_id", "number", "status", "url"],
                    "operations": ["schema", "list", "create", "update"],
                }
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
        "actions": {
            "tickets.syncExternal": {
                "kind": "connector",
                "effects": ["external.read", "domain.write"],
                "executor": {"type": "registered", "key": "generic.http"},
                "connector_spec": {
                    "kind": "http_sync",
                    "request": {"method": "GET", "url": "https://api.example.test/issues"},
                    "response": {"items_path": "$"},
                    "sync": {
                        "binding": "tickets",
                        "remote_id": "id",
                        "remote_id_field": "external_id",
                        "title": "title",
                        "fields": {
                            "number": "number",
                            "status": {"const": "Done"},
                            "url": "html_url",
                        },
                    },
                },
            }
        },
    }
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="generic-http-ticket-sync",
        name="Generic HTTP Ticket Sync",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=manifest,
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with patch(
        "brain.systems.workspace_apps.generic_http._request_json",
        return_value=[
            {"id": 123, "number": 123, "title": "Updated issue", "html_url": "https://example.test/123"},
            {"id": 456, "number": 456, "title": "New issue", "html_url": "https://example.test/456"},
        ],
    ), patch.object(AsyncDomainService, "serialize_record", _serialize_record_for_connector_test):
        result = await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={},
            user_id=USER_ID,
        )

    assert result["ok"] is True
    assert result["result"]["created"] == 1
    assert result["result"]["updated"] == 1
    await session.refresh(existing)
    assert existing.title == "Updated issue"
    assert existing.data["status"] == "Done"
    records = await service.list_records(ORG_ID, domain.id, object_key="ticket", limit=10)
    assert {record.title for record in records} == {"Updated issue", "New issue"}


async def test_workspace_app_action_generic_http_supports_templates_and_conditionals(session):
    domain = await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Imported Tickets",
        slug="imported-tickets",
        objects=[
            {
                "key": "ticket",
                "name": "Ticket",
                "fields": [
                    {"key": "external_id", "field_type": "text"},
                    {"key": "identifier", "field_type": "text"},
                    {"key": "description", "field_type": "text"},
                    {"key": "assignee", "field_type": "text"},
                    {"key": "status", "field_type": "enum", "options": ["Todo", "Done"]},
                    {"key": "url", "field_type": "url"},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    service = AsyncDomainService(session)
    manifest = {
        "contract_version": 1,
        "data_plan": {
            "mode": "domain",
            "bindings": {
                "tickets": {
                    "domain_id": domain.id,
                    "object_key": "ticket",
                    "fields": ["title", "external_id", "identifier", "description", "assignee", "status", "url"],
                    "operations": ["schema", "list", "create", "update"],
                }
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
        "actions": {
            "tickets.syncExternal": {
                "kind": "connector",
                "effects": ["external.read", "domain.write"],
                "executor": {"type": "registered", "key": "generic.http"},
                "connector_spec": {
                    "kind": "http_sync",
                    "request": {
                        "method": "GET",
                        "url": "https://jsonplaceholder.typicode.com/todos",
                    },
                    "response": {"items_path": "$"},
                    "sync": {
                        "binding": "tickets",
                        "remote_id": "id",
                        "remote_id_field": "external_id",
                        "title": "title",
                        "fields": {
                            "external_id": "id",
                            "description": "title",
                            "identifier": {"template": "TODO-{id}"},
                            "assignee": {"template": "User {userId}"},
                            "url": {"template": "https://jsonplaceholder.typicode.com/todos/{id}"},
                            "status": {
                                "if": {"field": "completed", "equals": True},
                                "then": "Done",
                                "else": "Todo",
                            },
                        },
                    },
                },
            }
        },
    }
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="generic-http-ticket-template-sync",
        name="Generic HTTP Ticket Template Sync",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=manifest,
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with patch(
        "brain.systems.workspace_apps.generic_http._request_json",
        return_value=[
            {"id": 1, "userId": 7, "title": "Buy milk", "completed": False},
            {"id": 2, "userId": 8, "title": "Ship fix", "completed": True},
        ],
    ), patch.object(AsyncDomainService, "serialize_record", _serialize_record_for_connector_test):
        result = await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={},
            user_id=USER_ID,
        )

    assert result["ok"] is True
    assert result["result"]["created"] == 2
    records = await service.list_records(ORG_ID, domain.id, object_key="ticket", limit=10)
    by_external_id = {record.data["external_id"]: record for record in records}
    first = by_external_id["1"]
    assert first.title == "Buy milk"
    assert first.data["identifier"] == "TODO-1"
    assert first.data["assignee"] == "User 7"
    assert first.data["status"] == "Todo"
    assert first.data["url"] == "https://jsonplaceholder.typicode.com/todos/1"
    second = by_external_id["2"]
    assert second.data["identifier"] == "TODO-2"
    assert second.data["status"] == "Done"


async def test_workspace_app_action_generic_http_populates_required_title_field_from_sync_title(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Required Title Tickets",
        slug="required-title-tickets",
        objects=[
            {
                "key": "ticket",
                "name": "Ticket",
                "fields": [
                    {"key": "external_id", "field_type": "text", "required": True},
                    {"key": "title", "field_type": "text", "required": True},
                    {"key": "status", "field_type": "enum", "options": ["Todo", "Done"]},
                    {"key": "link", "field_type": "url"},
                    {"key": "synced_at", "field_type": "datetime"},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    manifest = {
        "contract_version": 1,
        "data_plan": {
            "mode": "domain",
            "bindings": {
                "tickets": {
                    "domain_id": domain.id,
                    "object_key": "ticket",
                    "fields": ["title", "external_id", "status", "link", "synced_at"],
                    "operations": ["schema", "list", "create", "update"],
                }
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
        "actions": {
            "tickets.syncExternal": {
                "kind": "connector",
                "effects": ["external.read", "domain.write"],
                "executor": {"type": "registered", "key": "generic.http"},
                "connector_spec": {
                    "kind": "http_sync",
                    "request": {"method": "GET", "url": "https://jsonplaceholder.typicode.com/todos"},
                    "response": {"items_path": "$"},
                    "sync": {
                        "binding": "tickets",
                        "remote_id": "id",
                        "remote_id_field": "external_id",
                        "title": "title",
                        "fields": {
                            "status": {
                                "if": {"path": "completed", "equals": True},
                                "then": "Done",
                                "else": "Todo",
                            },
                            "link": {"template": "https://jsonplaceholder.typicode.com/todos/{id}"},
                            "synced_at": {"now": True},
                        },
                    },
                },
            }
        },
    }
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="generic-http-required-title-sync",
        name="Generic HTTP Required Title Sync",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=manifest,
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with patch(
        "brain.systems.workspace_apps.generic_http._request_json",
        return_value=[{"id": 1, "title": "delectus aut autem", "completed": False}],
    ), patch("brain.systems.workspace_apps.generic_http._utc_now_iso", return_value="2026-05-15T14:30:00Z"):
        result = await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={},
            user_id=USER_ID,
        )

    assert result["ok"] is True
    records = await service.list_records(ORG_ID, domain.id, object_key="ticket", limit=10)
    assert len(records) == 1
    assert records[0].title == "delectus aut autem"
    assert records[0].data["title"] == "delectus aut autem"


async def test_workspace_app_action_generic_http_supports_now_mapping(session):
    service = AsyncDomainService(session)
    domain = await service.create_domain(
        ORG_ID,
        name="Timestamped Tickets",
        slug="timestamped-tickets",
        objects=[
            {
                "key": "ticket",
                "name": "Ticket",
                "fields": [
                    {"key": "external_id", "field_type": "text"},
                    {"key": "synced_at", "field_type": "datetime"},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    manifest = {
        "contract_version": 1,
        "data_plan": {
            "mode": "domain",
            "bindings": {
                "tickets": {
                    "domain_id": domain.id,
                    "object_key": "ticket",
                    "fields": ["title", "external_id", "synced_at"],
                    "operations": ["schema", "list", "create", "update"],
                }
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
        "actions": {
            "tickets.syncExternal": {
                "kind": "connector",
                "effects": ["external.read", "domain.write"],
                "executor": {"type": "registered", "key": "generic.http"},
                "connector_spec": {
                    "kind": "http_sync",
                    "request": {"method": "GET", "url": "https://jsonplaceholder.typicode.com/todos"},
                    "response": {"items_path": "$"},
                    "sync": {
                        "binding": "tickets",
                        "remote_id": "id",
                        "remote_id_field": "external_id",
                        "title": "title",
                        "fields": {
                            "synced_at": {"now": True},
                        },
                    },
                },
            }
        },
    }
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="generic-http-now-sync",
        name="Generic HTTP Now Sync",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=manifest,
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with patch(
        "brain.systems.workspace_apps.generic_http._request_json",
        return_value=[{"id": "todo-1", "title": "Synced todo"}],
    ), patch("brain.systems.workspace_apps.generic_http._utc_now_iso", return_value="2026-05-13T21:20:05Z"):
        result = await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={},
            user_id=USER_ID,
        )

    assert result["ok"] is True
    assert result["result"]["created"] == 1
    record = (await service.list_records(ORG_ID, domain.id, object_key="ticket", limit=1))[0]
    assert record.title == "Synced todo"
    assert record.data["external_id"] == "todo-1"
    assert record.data["synced_at"] == "2026-05-13T21:20:05Z"


async def test_workspace_app_action_generic_http_request_returns_compact_response(session):
    domain = await _todo_domain(session)
    manifest = _manifest_with_action(
        domain.id,
        {
            "kind": "connector",
            "effects": ["external.write"],
            "executor": {"type": "registered", "key": "generic.http"},
            "connector_spec": {
                "kind": "http_request",
                "request": {
                    "method": "POST",
                    "url": "https://api.example.test/issues",
                    "json": {"title": "{title}"},
                },
            },
        },
    )
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="generic-http-create-issue",
        name="Generic HTTP Create Issue",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(VALID_GENERATED_UI_SPEC),
        manifest=manifest,
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with patch("brain.systems.workspace_apps.generic_http._request_json", return_value={"id": "abc", "ok": True}):
        result = await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={"title": "Ship it"},
            user_id=USER_ID,
        )

    assert result["ok"] is True
    assert result["result"] == {"response": {"id": "abc", "ok": True}}


async def test_workspace_app_action_generic_http_rejects_invalid_mapping_at_save(session):
    domain = await _todo_domain(session)

    with pytest.raises(WorkspaceAppContractError, match="mapping expressions must use const, path, template, now, or if/then/else"):
        await a_create_app(
            session,
            org_id=ORG_ID,
            key="generic-http-bad-mapping",
            name="Generic HTTP Bad Mapping",
            renderer_key="generated-ui-app",
            source_kind="json",
            source_code=json.dumps(VALID_GENERATED_UI_SPEC),
            manifest=_manifest_with_action(
                domain.id,
                {
                    "kind": "connector",
                    "effects": ["external.read", "domain.write"],
                    "executor": {"type": "registered", "key": "generic.http"},
                    "connector_spec": {
                        "kind": "http_sync",
                        "request": {"method": "GET", "url": "https://api.example.test/todos"},
                        "response": {"items_path": "$"},
                        "sync": {
                            "binding": "todos",
                            "remote_id": "id",
                            "remote_id_field": "notes",
                            "fields": {"notes": {"field": "title"}},
                        },
                    },
                },
            ),
            visual_spec=VALID_VISUAL_SPEC,
            created_by_user_id=USER_ID,
        )


async def test_workspace_app_action_generic_http_rejects_unknown_sync_field_at_save(session):
    domain = await _todo_domain(session)

    with pytest.raises(
        WorkspaceAppContractError,
        match=r"connector_spec\.sync\.fields\.sync_status must be a field on binding 'todos'",
    ):
        await a_create_app(
            session,
            org_id=ORG_ID,
            key="generic-http-unknown-sync-field",
            name="Generic HTTP Unknown Sync Field",
            renderer_key="generated-ui-app",
            source_kind="json",
            source_code=json.dumps(VALID_GENERATED_UI_SPEC),
            manifest=_manifest_with_action(
                domain.id,
                {
                    "kind": "connector",
                    "effects": ["external.read", "domain.write"],
                    "executor": {"type": "registered", "key": "generic.http"},
                    "connector_spec": {
                        "kind": "http_sync",
                        "request": {"method": "GET", "url": "https://api.example.test/todos"},
                        "response": {"items_path": "$"},
                        "sync": {
                            "binding": "todos",
                            "remote_id": "id",
                            "remote_id_field": "notes",
                            "title": "title",
                            "fields": {
                                "notes": "id",
                                "sync_status": {"const": "imported"},
                            },
                        },
                    },
                },
            ),
            visual_spec=VALID_VISUAL_SPEC,
            created_by_user_id=USER_ID,
        )


async def test_workspace_app_action_generic_http_wraps_domain_validation_errors(session):
    domain = await AsyncDomainService(session).create_domain(
        ORG_ID,
        name="Validated Tickets",
        slug="validated-tickets",
        objects=[
            {
                "key": "ticket",
                "name": "Ticket",
                "fields": [
                    {"key": "external_id", "field_type": "text"},
                    {"key": "status", "field_type": "enum", "options": ["Todo", "Done"]},
                ],
            }
        ],
        actor_id=USER_ID,
    )
    source = {
        "schema_version": 1,
        "title": "Validated Tickets",
        "primary_binding": "tickets",
        "views": [
            {
                "id": "tickets",
                "type": "table",
                "title": "Tickets",
                "binding": "tickets",
                "columns": [
                    {"key": "title", "label": "Title"},
                    {"key": "status", "label": "Status"},
                ],
            }
        ],
    }
    manifest = {
        "contract_version": 1,
        "data_plan": {
            "mode": "domain",
            "bindings": {
                "tickets": {
                    "domain_id": domain.id,
                    "object_key": "ticket",
                    "fields": ["title", "external_id", "status"],
                    "operations": ["schema", "list", "create", "update"],
                }
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
        "actions": {
            "tickets.syncExternal": {
                "kind": "connector",
                "effects": ["external.read", "domain.write"],
                "executor": {"type": "registered", "key": "generic.http"},
                "connector_spec": {
                    "kind": "http_sync",
                    "request": {"method": "GET", "url": "https://api.example.test/tickets"},
                    "response": {"items_path": "$"},
                    "sync": {
                        "binding": "tickets",
                        "remote_id": "id",
                        "remote_id_field": "external_id",
                        "title": "title",
                        "fields": {
                            "external_id": "id",
                            "status": "completed",
                        },
                    },
                },
            }
        },
    }
    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="generic-http-invalid-domain-data",
        name="Generic HTTP Invalid Domain Data",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(source),
        manifest=manifest,
        visual_spec=VALID_VISUAL_SPEC,
        created_by_user_id=USER_ID,
    )

    with patch(
        "brain.systems.workspace_apps.generic_http._request_json",
        return_value=[{"id": 1, "title": "Invalid status", "completed": True}],
    ):
        with pytest.raises(
            WorkspaceAppActionContractError,
            match="connector_spec.sync produced invalid Domain data: Field 'status' must be one of",
        ):
            await async_run_workspace_app_action(
                session,
                org_id=ORG_ID,
                app_id=app.id,
                action_key="tickets.syncExternal",
                payload={},
                user_id=USER_ID,
            )


async def test_workspace_app_action_boundaries_reject_raw_secrets(session):
    domain = await _todo_domain(session)

    with pytest.raises(WorkspaceAppContractError, match="raw credentials"):
        await a_create_app(
            session,
            org_id=ORG_ID,
            key="bad-action-secret",
            name="Bad Action Secret",
            renderer_key="sandboxed-html-app",
            source_kind="html",
            source_code=VALID_SOURCE,
            manifest=_manifest_with_action(
                domain.id,
                {
                    "kind": "connector",
                    "description": "Bad connector declaration.",
                    "effects": ["external.write"],
                    "executor": {"type": "deferred"},
                    "api_key": "github_pat_example_should_not_be_in_manifest",
                },
            ),
            visual_spec=VALID_VISUAL_SPEC,
        )

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="payload-secret",
        name="Payload Secret",
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_manifest_with_action(domain.id),
        visual_spec=VALID_VISUAL_SPEC,
    )

    with pytest.raises(WorkspaceAppActionContractError, match="payload must not contain raw credentials"):
        await async_run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={"token": "github_pat_example_should_not_be_in_payload"},
            user_id=USER_ID,
        )
