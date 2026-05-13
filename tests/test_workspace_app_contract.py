from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from brain.systems.workspace_apps.contracts import build_contract_validation_report
from brain.systems.workspace_apps.actions import WorkspaceAppActionExecutorMissing
from brain.systems.workspace_apps.service import WorkspaceAppContractError, _validate_contract_state_payload_or_raise


VALID_DOMAIN_MANIFEST = {
    "contract_version": 1,
    "data_plan": {
        "mode": "domain",
        "bindings": {
            "tasks": {
                "domain_id": 1,
                "object_key": "task",
                "operations": ["query", "create", "update", "archive", "schema"],
            }
        },
    },
    "design_contract": {
        "kit": "constellation-app-kit",
        "theme_modes": ["dark", "light"],
    },
}

VALID_SOURCE = """
<main class="illo-app">
  <section class="illo-panel illo-stack">
    <div class="illo-toolbar">
      <h1 class="illo-title">Work surface</h1>
      <button class="illo-button is-primary" type="button">Add</button>
    </div>
    <ul class="illo-list"></ul>
  </section>
</main>
<script>
  async function loadRecords() {
    return window.illo.domains.query({ alias: 'tasks' });
  }
  loadRecords();
</script>
"""

VALID_THUMBNAIL = {
    "thumbnail": {
        "label": "Work",
        "value": "Live",
        "secondary": "Domain-backed",
        "progress": 20,
    }
}

VALID_GENERATED_UI_SOURCE = json.dumps(
    {
        "schema_version": 1,
        "title": "Task Tracker",
        "primary_binding": "tasks",
        "views": [
            {
                "id": "tasks",
                "type": "table",
                "title": "Tasks",
                "binding": "tasks",
                "columns": [
                    {"key": "title", "label": "Title"},
                    {"key": "status", "label": "Status", "type": "status", "editable": True, "options": ["new", "done"]},
                ],
            }
        ],
    }
)


def _report(**overrides):
    payload = {
        "renderer_key": "sandboxed-html-app",
        "source_kind": "html",
        "source_code": VALID_SOURCE,
        "manifest": VALID_DOMAIN_MANIFEST,
        "visual_spec": VALID_THUMBNAIL,
        "metadata": {},
    }
    payload.update(overrides)
    return build_contract_validation_report(**payload)


def test_domain_backed_app_kit_payload_is_accepted():
    report = _report()

    assert report["status"] == "passed"
    assert report["errors"] == []

    rejected_state = _report(initial_state={"tasks": []})
    assert rejected_state["status"] == "failed"
    assert any("initial_state" in error and "Domain binding" in error for error in rejected_state["errors"])


def test_domain_binding_contract_accepts_generic_workspace_primitives():
    manifest = json.loads(json.dumps(VALID_DOMAIN_MANIFEST))
    manifest["data_plan"]["bindings"]["tasks"]["operations"] = [
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

    report = _report(manifest=manifest)

    assert report["status"] == "passed"
    assert report["errors"] == []


def test_structured_generated_ui_payload_is_accepted():
    report = _report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=VALID_GENERATED_UI_SOURCE,
    )

    assert report["status"] == "passed"
    assert report["errors"] == []

    rejected = _report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps({"schema_version": 1, "views": [{"type": "teleport"}]}),
    )
    assert rejected["status"] == "failed"
    assert any("spec.title" in error for error in rejected["errors"])
    assert any("views[0].type" in error for error in rejected["errors"])


def test_structured_generated_ui_actions_must_reference_manifest_actions():
    source = {
        "schema_version": 1,
        "title": "Task Tracker",
        "primary_binding": "tasks",
        "actions": [{"key": "tasks.syncExternal", "label": "Sync"}],
        "views": [{"id": "tasks", "type": "board", "title": "Tasks", "binding": "tasks"}],
    }
    manifest = {
        **VALID_DOMAIN_MANIFEST,
        "actions": {
            "tasks.syncExternal": {
                "kind": "connector",
                "effects": ["external.read", "domain.write"],
                "executor": {"type": "deferred"},
            }
        },
    }

    accepted = _report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(source),
        manifest=manifest,
    )
    assert accepted["status"] == "passed"

    rejected = _report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(source),
    )
    assert rejected["status"] == "failed"
    assert any("must reference a manifest.actions declaration" in error for error in rejected["errors"])

    secret_source = {
        **source,
        "actions": [{"key": "tasks.syncExternal", "payload": {"github_token": "ghp_123456789012345678901234567890123456"}}],
    }
    secret_rejected = _report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(secret_source),
        manifest=manifest,
    )
    assert secret_rejected["status"] == "failed"
    assert any("must not contain raw credentials" in error for error in secret_rejected["errors"])


def test_old_quick_todo_style_payload_is_rejected():
    report = _report(
        manifest={
            "contract_version": 1,
            "data_plan": {"mode": "app_local", "scope": "ui_state"},
            "design_contract": {"kit": "constellation-app-kit", "theme_modes": ["dark", "light"]},
        },
        visual_spec={
            "thumbnail": {
                "source_kind": "html",
                "source_code": "<div style='background:#7c3aed'>Quick To-Do</div>",
            }
        },
        source_code="""
          <style>
            body{background:#f7f3ff;color:#20133a}
            h1{letter-spacing:-.05em}
          </style>
          <main class="card"><input><button>Add</button></main>
          <script>localStorage.quickTodo = JSON.stringify({tasks: []})</script>
        """,
        initial_state={"todos": []},
    )

    assert report["status"] == "failed"
    assert any("record-like collections" in error for error in report["errors"])
    assert any("thumbnail" in error and "metadata" in error for error in report["errors"])
    assert any("illo-app" in error for error in report["errors"])
    assert any("browser storage" in error for error in report["errors"])
    assert any("hardcode visual colors" in error for error in report["errors"])
    assert any("negative letter spacing" in error for error in report["errors"])
    assert any("fixed body background" in error for error in report["errors"])
    assert any("illo-panel" in error for error in report["errors"])
    assert any("illo-input" in error for error in report["errors"])
    assert any("illo-button" in error for error in report["errors"])


def test_raw_form_controls_are_rejected_even_inside_app_root():
    report = _report(
        source_code="""
          <main class="illo-app">
            <section class="illo-panel">
              <input placeholder="Title">
              <textarea placeholder="Body"></textarea>
              <select><option>Open</option></select>
              <button type="button">Save</button>
              <ul><li>One</li></ul>
            </section>
          </main>
        """,
    )

    assert report["status"] == "failed"
    assert any("illo-input" in error for error in report["errors"])
    assert any("illo-textarea" in error for error in report["errors"])
    assert any("illo-select" in error for error in report["errors"])
    assert any("illo-button" in error for error in report["errors"])
    assert any("illo-list" in error for error in report["errors"])


def test_app_local_state_is_accepted_only_for_ui_state():
    report = _report(
        manifest={
            "contract_version": 1,
            "data_plan": {"mode": "app_local", "scope": "preferences"},
            "design_contract": {"kit": "constellation-app-kit", "theme_modes": ["dark", "light"]},
        },
        initial_state={"filter": "mine"},
    )
    assert report["status"] == "passed"

    rejected = _report(
        manifest={
            "contract_version": 1,
            "data_plan": {"mode": "app_local"},
            "design_contract": {"kit": "constellation-app-kit", "theme_modes": ["dark", "light"]},
        },
    )
    assert rejected["status"] == "failed"
    assert any("UI-only scope" in error for error in rejected["errors"])


def test_legacy_and_prototype_apps_remain_readable():
    legacy = _report(manifest={}, require_contract=False)
    assert legacy["status"] == "legacy"

    prototype = _report(
        manifest={},
        visual_spec={"thumbnail": {"source_code": "<div>legacy</div>"}},
        source_code="<script src='https://example.com/app.js'></script>",
        metadata={"prototype": True},
    )
    assert prototype["status"] == "skipped"


def test_contract_state_updates_reject_record_collections():
    app = SimpleNamespace(app_metadata={}, renderer_key="sandboxed-html-app", visual_spec={})
    version = SimpleNamespace(manifest={"contract_version": 1})

    _validate_contract_state_payload_or_raise(app, None, {"tasks": []})

    with pytest.raises(WorkspaceAppContractError) as exc:
        _validate_contract_state_payload_or_raise(app, version, {"filter": "open", "tasks": []})

    assert exc.value.report["status"] == "failed"
    assert "Domain records" in exc.value.report["errors"][0]


class _FakeUow:
    session = object()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        return None


class _FakeAsyncDb:
    def __init__(self):
        self.sync_session = MagicMock()

    async def run_sync(self, fn):
        return fn(self.sync_session)


def test_manage_workspace_app_surfaces_contract_errors():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    report = _report(source_code="<main></main>")

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.create_app",
        side_effect=WorkspaceAppContractError(report),
    ):
        result = json.loads(_handle_manage_workspace_app(action="create", name="Bad App"))

    assert result["contract_validation"]["status"] == "failed"
    assert "Workspace app contract validation failed" in result["error"]


@pytest.mark.asyncio
async def test_workspace_app_action_route_uses_sync_session_bridge():
    from fastapi import HTTPException

    from brain.app.api.routers.workspace_apps import run_workspace_app_declared_action
    from brain.app.api.schemas.workspace_apps import WorkspaceAppActionRun

    db = _FakeAsyncDb()

    with patch(
        "brain.app.api.routers.workspace_apps.run_workspace_app_action",
        side_effect=WorkspaceAppActionExecutorMissing("missing executor"),
    ) as run_action:
        with pytest.raises(HTTPException) as exc:
            await run_workspace_app_declared_action(
                "app-1",
                WorkspaceAppActionRun(action_key="tickets.syncExternal", payload={}),
                db=db,
                user={"id": "user-1", "org_id": "org-1"},
            )

    assert exc.value.status_code == 501
    assert exc.value.detail == "missing executor"
    assert run_action.call_args.args[0] is db.sync_session


def test_manage_workspace_app_schema_ignores_extra_tool_args():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    result = json.loads(
        _handle_manage_workspace_app(
            action="schema",
            operation="create",
            limit=10,
        )
    )

    assert result["tool"] == "manage_workspace_app"
    assert result["operation"] == "create"


def test_manage_workspace_app_archived_lookup_requires_confirmation():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork") as uow:
        result = json.loads(_handle_manage_workspace_app(action="get", app_id="app-1", include_archived=True))

    assert "confirm_include_archived=true" in result["error"]
    uow.assert_not_called()


def test_manage_workspace_app_extracts_embedded_contract_fields_from_source_code():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    wrapped_source = json.dumps(
        {
            **json.loads(VALID_GENERATED_UI_SOURCE),
            "manifest": VALID_DOMAIN_MANIFEST,
            "visual_spec": VALID_THUMBNAIL,
            "metadata": {"created_from": "wrapped-source"},
        }
    )
    app = object()
    serialized = {"id": "app-1", "key": "tasks", "name": "Task Tracker"}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.create_app",
        return_value=app,
    ) as create_app, patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ):
        result = json.loads(
            _handle_manage_workspace_app(
                action="create",
                key="tasks",
                name="Task Tracker",
                renderer_key="generated-ui-app",
                source_code=wrapped_source,
            )
        )

    assert result["app"] == serialized
    kwargs = create_app.call_args.kwargs
    assert kwargs["source_kind"] == "json"
    assert kwargs["manifest"] == VALID_DOMAIN_MANIFEST
    assert kwargs["visual_spec"] == VALID_THUMBNAIL
    assert kwargs["metadata"] == {"created_from": "wrapped-source"}
    saved_source = json.loads(kwargs["source_code"])
    assert saved_source["title"] == "Task Tracker"
    assert "manifest" not in saved_source
    assert "visual_spec" not in saved_source
    assert "metadata" not in saved_source


def test_manage_workspace_app_compiles_minimal_generated_ui_create_payload():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    app = object()
    serialized = {"id": "app-1", "key": "leads", "name": "Lead CRM"}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.create_app",
        return_value=app,
    ) as create_app, patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ):
        result = json.loads(
            _handle_manage_workspace_app(
                action="create",
                key="leads",
                name="Lead CRM",
                source_code=json.dumps({"rows": [{"company": "Acme", "status": "new"}]}),
            )
        )

    assert result["app"] == serialized
    assert result["compiler_repairs"]
    kwargs = create_app.call_args.kwargs
    assert kwargs["renderer_key"] == "generated-ui-app"
    assert kwargs["source_kind"] == "json"
    assert kwargs["manifest"]["contract_version"] == 1
    assert kwargs["manifest"]["data_plan"] == {"mode": "app_local", "scope": "ui_state"}
    assert kwargs["visual_spec"]["thumbnail"]["label"] == "Lead CRM"
    saved_source = json.loads(kwargs["source_code"])
    assert saved_source["title"] == "Lead CRM"
    assert saved_source["views"][0]["type"] == "table"


def test_manage_workspace_app_normalizes_wrapped_source_before_contract_errors():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    wrapped_source = json.dumps(
        {
            "schema_version": 2,
            "manifest": VALID_DOMAIN_MANIFEST,
            "visual_spec": VALID_THUMBNAIL,
        }
    )

    def validate_then_raise(_session, **kwargs):
        report = build_contract_validation_report(
            renderer_key=kwargs["renderer_key"],
            source_kind=kwargs["source_kind"],
            source_code=kwargs["source_code"],
            manifest=kwargs["manifest"],
            visual_spec=kwargs["visual_spec"],
            metadata=kwargs["metadata"],
        )
        raise WorkspaceAppContractError(report)

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.create_app",
        side_effect=validate_then_raise,
    ):
        result = json.loads(
            _handle_manage_workspace_app(
                action="create",
                key="tasks",
                name="Task Tracker",
                renderer_key="generated-ui-app",
                source_code=wrapped_source,
            )
        )

    errors = result["contract_validation"]["errors"]
    assert result["contract_validation"]["contract_version"] == 1
    assert "manifest.contract_version must be 1" not in errors
    assert "manifest.data_plan is required" not in errors
    assert "manifest.design_contract is required" not in errors
    assert "visual_spec.thumbnail must be structured metadata" not in errors
    assert "generated UI schema_version must be 1 when provided" in errors
    assert result["repair_guidance"]["failure_kind"] == "generated_ui_source_shape"


def test_manage_workspace_app_extracts_embedded_contract_fields_on_update():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    explicit_manifest = {
        "contract_version": 1,
        "data_plan": {"mode": "app_local", "scope": "ui_state"},
        "design_contract": {"kit": "constellation-app-kit", "theme_modes": ["dark", "light"]},
    }
    wrapped_source = json.dumps(
        {
            **json.loads(VALID_GENERATED_UI_SOURCE),
            "manifest": VALID_DOMAIN_MANIFEST,
            "visual_spec": VALID_THUMBNAIL,
            "metadata": {"created_from": "wrapped-source"},
        }
    )
    app = object()
    serialized = {"id": "app-1", "key": "tasks", "name": "Task Tracker"}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.update_app",
        return_value=app,
    ) as update_app, patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ):
        result = json.loads(
            _handle_manage_workspace_app(
                action="update",
                app_id="app-1",
                renderer_key="generated-ui-app",
                source_code=wrapped_source,
                manifest=explicit_manifest,
            )
        )

    assert result["app"] == serialized
    kwargs = update_app.call_args.kwargs
    assert kwargs["source_kind"] == "json"
    assert kwargs["manifest"] == explicit_manifest
    assert kwargs["visual_spec"] == VALID_THUMBNAIL
    assert kwargs["metadata"] == {"created_from": "wrapped-source"}
    assert "manifest" not in json.loads(kwargs["source_code"])


def test_manage_workspace_app_update_repairs_domain_ops_with_generic_http_action():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    manifest = json.loads(json.dumps(VALID_DOMAIN_MANIFEST))
    manifest["data_plan"]["bindings"]["tasks"]["operations"] = ["read", "write"]
    manifest["actions"] = {
        "tasks.syncExternal": {
            "kind": "connector",
            "effects": ["external.read", "domain.write"],
            "executor": {"type": "registered", "key": "generic.http"},
            "connector_spec": {
                "kind": "http_sync",
                "request": {
                    "method": "GET",
                    "url": "https://jsonplaceholder.typicode.com/todos",
                },
                "response": {"items_path": ""},
                "sync": {
                    "binding": "tasks",
                    "remote_id": "id",
                    "title": "title",
                    "fields": {
                        "status": {
                            "if": {"path": "completed", "equals": True},
                            "then": "Done",
                            "else": "Todo",
                        }
                    },
                },
            },
        }
    }
    source_code = json.dumps(
        {
            **json.loads(VALID_GENERATED_UI_SOURCE),
            "actions": [{"key": "tasks.syncExternal", "label": "Sync"}],
        }
    )
    app = object()
    serialized = {"id": "app-1", "key": "tasks", "name": "Task Tracker"}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.update_app",
        return_value=app,
    ) as update_app, patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ):
        result = json.loads(
            _handle_manage_workspace_app(
                action="update",
                app_id="app-1",
                renderer_key="generated-ui-app",
                source_kind="json",
                source_code=source_code,
                manifest=manifest,
            )
        )

    assert result["app"] == serialized
    assert result["compiler_repairs"] == [
        {
            "field": "manifest.data_plan.bindings.tasks.operations",
            "message": "normalized Domain binding operations",
        }
    ]
    kwargs = update_app.call_args.kwargs
    assert kwargs["manifest"]["data_plan"]["bindings"]["tasks"]["operations"] == [
        "schema",
        "list",
        "get",
        "query",
        "create",
        "update",
    ]
    assert kwargs["manifest"]["actions"]["tasks.syncExternal"]["executor"]["key"] == "generic.http"
    assert (
        build_contract_validation_report(
            renderer_key=kwargs["renderer_key"],
            source_kind=kwargs["source_kind"],
            source_code=kwargs["source_code"],
            manifest=kwargs["manifest"],
            visual_spec=VALID_THUMBNAIL,
        )["status"]
        == "passed"
    )


def test_manage_workspace_app_update_normalizes_empty_optional_fields():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    app = object()
    serialized = {"id": "app-1", "key": "tasks", "name": "Task Tracker"}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.update_app",
        return_value=app,
    ) as update_app, patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ):
        result = json.loads(
            _handle_manage_workspace_app(
                action="update",
                app_id="app-1",
                name="",
                description="",
                anchor_user_id="",
                include_archived="false",
                manifest="",
                visual_spec="",
            )
        )

    assert result["app"] == serialized
    kwargs = update_app.call_args.kwargs
    assert kwargs["name"] is None
    assert kwargs["description"] is None
    assert kwargs["anchor_user_id"] is None
    assert kwargs["manifest"] is None
    assert kwargs["visual_spec"] is None


def test_manage_workspace_app_update_parses_json_object_strings():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    app = object()
    serialized = {"id": "app-1", "key": "tasks", "name": "Task Tracker"}
    manifest = {"contract_version": 1, "data_plan": {"mode": "app_local", "scope": "ui_state"}}
    visual_spec = {"thumbnail": {"label": "Tasks", "status": "Ready"}}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.update_app",
        return_value=app,
    ) as update_app, patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ):
        result = json.loads(
            _handle_manage_workspace_app(
                action="update",
                app_id="app-1",
                manifest=json.dumps(manifest),
                visual_spec=json.dumps(visual_spec),
            )
        )

    assert result["app"] == serialized
    kwargs = update_app.call_args.kwargs
    assert kwargs["manifest"] == manifest
    assert kwargs["visual_spec"] == visual_spec


def test_manage_workspace_app_publishes_change_after_create():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    app = object()
    serialized = {"id": "app-1", "key": "quick", "name": "Quick App"}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.create_app",
        return_value=app,
    ), patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ) as publish:
        result = json.loads(_handle_manage_workspace_app(action="create", name="Quick App"))

    assert result["app"] == serialized
    publish.assert_called_once_with(
        org_id="11111111-1111-4111-8111-111111111111",
        action="create",
        app=serialized,
    )


def test_manage_workspace_app_publishes_change_after_archive():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    archive_result = {"archived": {"id": "app-1", "key": "quick"}}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.archive_app",
        return_value=archive_result,
    ), patch("brain.systems.workspace_apps.events.publish_workspace_app_change") as publish:
        result = json.loads(_handle_manage_workspace_app(action="archive", app_id="app-1"))

    assert result == archive_result
    publish.assert_called_once_with(
        org_id="11111111-1111-4111-8111-111111111111",
        action="archive",
        app_id="app-1",
        key="quick",
    )


def test_manage_workspace_app_publishes_change_after_restore():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    app = object()
    serialized = {"id": "app-1", "key": "quick", "name": "Quick App", "archived_at": None}

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.restore_app",
        return_value=app,
    ), patch("brain.systems.workspace_apps.service.serialize_app", return_value=serialized), patch(
        "brain.systems.workspace_apps.events.publish_workspace_app_change"
    ) as publish:
        result = json.loads(
            _handle_manage_workspace_app(
                action="restore",
                app_id="app-1",
                confirm_restore_archived=True,
            )
        )

    assert result["app"] == serialized
    publish.assert_called_once_with(
        org_id="11111111-1111-4111-8111-111111111111",
        action="restore",
        app=serialized,
    )


def test_manage_workspace_app_restore_requires_explicit_confirmation():
    from brain.systems.runs.tool_catalog.handlers.workspace_apps import _handle_manage_workspace_app

    with patch(
        "brain.systems.runs.tool_catalog.handlers.workspace_apps._workspace_app_context",
        return_value=("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
    ), patch("brain.platform.db.repositories.unit_of_work.UnitOfWork", return_value=_FakeUow()), patch(
        "brain.systems.workspace_apps.service.restore_app",
    ) as restore_app:
        result = json.loads(_handle_manage_workspace_app(action="restore", app_id="app-1"))

    assert "confirm_restore_archived=true" in result["error"]
    restore_app.assert_not_called()
