from __future__ import annotations

import re
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import Session

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
from brain.systems.user_domains.service import DomainService
from brain.systems.workspace_apps.service import (
    WorkspaceAppError,
    WorkspaceAppContractError,
    active_version,
    archive_app,
    create_app,
    list_archived_apps,
    restore_app,
    update_app,
)
from brain.systems.workspace_apps.actions import (
    WorkspaceAppActionContractError,
    WorkspaceAppActionExecutorMissing,
    register_workspace_app_action_executor,
    run_workspace_app_action,
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
def session():
    _patch_sqlite_for_pg_types()
    engine = create_engine("sqlite://", echo=False)
    for table in [
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
    ]:
        table.create(engine, checkfirst=True)
    db = Session(engine)
    db.add(Org(id=ORG_ID, name="Test Org", slug="test"))
    db.add(Org(id=OTHER_ORG_ID, name="Other Org", slug="other"))
    db.add(User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@example.test"))
    db.flush()
    yield db
    db.close()


def _todo_domain(session: Session):
    return DomainService(session).create_domain(
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


def test_valid_domain_backed_manifest_saves(session):
    domain = _todo_domain(session)

    app = create_app(
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

    version = active_version(session, app.id)
    assert version is not None
    assert version.manifest["data_plan"]["bindings"]["todos"]["object_key"] == "todo_item"


def test_domain_binding_accepts_generic_app_primitives(session):
    domain = _todo_domain(session)
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

    app = create_app(
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

    version = active_version(session, app.id)
    assert version is not None
    assert version.manifest["data_plan"]["bindings"]["todos"]["operations"] == operations


def test_valid_structured_generated_ui_app_saves(session):
    domain = _todo_domain(session)

    app = create_app(
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

    version = active_version(session, app.id)
    assert version is not None
    assert version.renderer_key == "generated-ui-app"
    assert version.source_kind == "json"
    assert json.loads(version.source_code)["views"][0]["type"] == "table"


def test_valid_structured_board_generated_ui_app_saves(session):
    domain = _todo_domain(session)
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

    app = create_app(
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

    version = active_version(session, app.id)
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
def test_invalid_domain_bindings_block_save(session, overrides, message):
    domain = _todo_domain(session)

    with pytest.raises(WorkspaceAppError, match=message):
        create_app(
            session,
            org_id=ORG_ID,
            key=f"bad-{message.split()[0]}",
            name="Broken Todo Notes",
            source_code="<main></main>",
            manifest=_manifest(domain.id, **overrides),
        )


def test_domain_binding_blocks_archived_or_cross_org_domain(session):
    domain = _todo_domain(session)
    domain.archived_at = datetime.now(timezone.utc)

    with pytest.raises(WorkspaceAppError, match="archived Domain"):
        create_app(
            session,
            org_id=ORG_ID,
            key="archived-domain",
            name="Archived Domain App",
            source_code="<main></main>",
            manifest=_manifest(domain.id),
        )

    other_domain = DomainService(session).create_domain(
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
        create_app(
            session,
            org_id=ORG_ID,
            key="cross-org-domain",
            name="Cross Org Domain App",
            source_code="<main></main>",
            manifest=_manifest(other_domain.id),
        )


def test_local_state_manifest_without_domain_bindings_still_saves(session):
    app = create_app(
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

    assert active_version(session, app.id).manifest == _app_local_manifest()


def test_app_local_note_collections_are_record_like(session):
    with pytest.raises(WorkspaceAppError, match="record-like collections"):
        create_app(
            session,
            org_id=ORG_ID,
            key="local-notes",
            name="Local Notes",
            source_code=VALID_SOURCE,
            manifest=_app_local_manifest(),
            visual_spec=VALID_VISUAL_SPEC,
            initial_state={"notes": []},
        )


def test_update_validates_effective_domain_manifest(session):
    domain = _todo_domain(session)
    app = create_app(
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

    update_app(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        source_code='<main class="illo-app"><section class="illo-panel">patched</section></main>',
    )
    assert active_version(session, app.id).version == 2

    with pytest.raises(WorkspaceAppError, match="omits required field"):
        update_app(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            manifest=_manifest(domain.id, fields=["notes"]),
        )


def test_archive_and_restore_app_leave_domain_records_intact(session):
    domain = _todo_domain(session)
    record = DomainService(session).create_record(
        ORG_ID,
        domain.id,
        "todo_item",
        data={"title": "Follow up", "notes": "CRM lead", "completed": False},
        actor_id=USER_ID,
    )
    app = create_app(
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

    archive_app(session, org_id=ORG_ID, app_id=app.id)
    assert app.archived_at is not None
    assert list_archived_apps(session, ORG_ID)[0].id == app.id

    service = DomainService(session)
    assert service.get_record(ORG_ID, domain.id, record.id).archived_at is None

    restored = restore_app(session, org_id=ORG_ID, app_id=app.id)
    assert restored.archived_at is None


def test_workspace_app_action_requires_registered_executor(session):
    domain = _todo_domain(session)
    app = create_app(
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
        run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={"ticketId": 1},
            user_id=USER_ID,
        )


def test_workspace_app_action_registered_executor_runs(session):
    domain = _todo_domain(session)
    app = create_app(
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
        result = run_workspace_app_action(
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


def test_workspace_app_action_boundaries_reject_raw_secrets(session):
    domain = _todo_domain(session)

    with pytest.raises(WorkspaceAppContractError, match="raw credentials"):
        create_app(
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

    app = create_app(
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
        run_workspace_app_action(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            action_key="tickets.syncExternal",
            payload={"token": "github_pat_example_should_not_be_in_payload"},
            user_id=USER_ID,
        )
