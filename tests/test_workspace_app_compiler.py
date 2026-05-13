from __future__ import annotations

import json

from brain.systems.workspace_apps.compiler import compile_workspace_app_input, contract_repair_guidance
from brain.systems.workspace_apps.contracts import build_contract_validation_report


def _validation_report(compiled, *, initial_state=None):
    return build_contract_validation_report(
        renderer_key=compiled.renderer_key,
        source_kind=compiled.source_kind,
        source_code=compiled.source_code,
        manifest=compiled.manifest,
        visual_spec=compiled.visual_spec,
        metadata=compiled.metadata,
        initial_state=initial_state,
    )


def test_compiler_defaults_minimal_generated_ui_rows_into_valid_app_contract():
    compiled = compile_workspace_app_input(
        action="create",
        name="Lead CRM",
        source_code=json.dumps(
            {
                "rows": [
                    {"company": "Acme", "status": "new"},
                    {"company": "Northwind", "status": "contacted"},
                ]
            }
        ),
    )

    assert compiled.renderer_key == "generated-ui-app"
    assert compiled.source_kind == "json"
    assert compiled.manifest["contract_version"] == 1
    assert compiled.manifest["data_plan"] == {"mode": "app_local", "scope": "ui_state"}
    assert compiled.manifest["design_contract"] == {
        "kit": "constellation-app-kit",
        "theme_modes": ["dark", "light"],
    }
    assert compiled.visual_spec["thumbnail"]["label"] == "Lead CRM"
    assert compiled.visual_spec["thumbnail"]["status"] == "Ready"

    source = json.loads(compiled.source_code)
    assert source["schema_version"] == 1
    assert source["title"] == "Lead CRM"
    assert source["views"][0]["type"] == "table"
    assert source["views"][0]["columns"] == [
        {"key": "company", "label": "Company"},
        {"key": "status", "label": "Status"},
    ]
    assert _validation_report(compiled)["status"] == "passed"


def test_compiler_extracts_source_envelope_and_completes_partial_manifest():
    compiled = compile_workspace_app_input(
        action="create",
        name="Sourcing Queue",
        source_code=json.dumps(
            {
                "source_code": {
                    "rows": [{"person": "Ada Lovelace", "priority": "high"}],
                },
                "manifest": {
                    "data_plan": {"scope": "filters"},
                },
                "visual_spec": {
                    "thumbnail": {"value": "2"},
                },
            }
        ),
    )

    source = json.loads(compiled.source_code)
    assert "manifest" not in source
    assert source["title"] == "Sourcing Queue"
    assert compiled.manifest["contract_version"] == 1
    assert compiled.manifest["data_plan"] == {"scope": "filters", "mode": "app_local"}
    assert compiled.manifest["design_contract"]["kit"] == "constellation-app-kit"
    assert compiled.visual_spec["thumbnail"]["label"] == "Sourcing Queue"
    assert compiled.visual_spec["thumbnail"]["value"] == "2"
    assert _validation_report(compiled)["status"] == "passed"


def test_compiler_infers_domain_backed_table_from_single_binding():
    compiled = compile_workspace_app_input(
        action="create",
        name="Lead CRM",
        source_code=json.dumps({"description": "Review active leads"}),
        manifest={
            "contract_version": 1,
            "data_plan": {
                "mode": "domain",
                "bindings": {
                    "leads": {
                        "domain_id": 1,
                        "object_key": "lead",
                        "fields": ["title", "company", "status"],
                        "operations": ["schema", "list", "query", "update"],
                    }
                },
            },
        },
    )

    source = json.loads(compiled.source_code)
    assert source["views"] == [
        {
            "id": "leads",
            "type": "table",
            "title": "Lead CRM",
            "binding": "leads",
            "columns": [
                {"key": "title", "label": "Title"},
                {"key": "company", "label": "Company"},
                {"key": "status", "label": "Status"},
            ],
        }
    ]
    assert compiled.manifest["design_contract"]["theme_modes"] == ["dark", "light"]
    assert _validation_report(compiled)["status"] == "passed"


def test_compiler_accepts_domain_backed_board_view():
    compiled = compile_workspace_app_input(
        action="create",
        name="GitHub Ticket Tracker",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(
            {
                "schema_version": 1,
                "title": "GitHub Ticket Tracker",
                "primary_binding": "tickets",
                "actions": [{"key": "tickets.syncExternal", "label": "Sync GitHub"}],
                "views": [
                    {
                        "id": "ticket-board",
                        "type": "kanban",
                        "title": "Tickets",
                        "binding": "tickets",
                        "groups": ["Backlog", "Todo", "In Progress", "In Review", "Done"],
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
                        "domain_id": 1,
                        "object_key": "ticket",
                        "fields": ["title", "status", "priority", "repo", "assignee"],
                        "operations": ["schema", "list", "query", "create", "update", "archive"],
                    }
                },
            },
            "actions": {
                "tickets.syncExternal": {
                    "kind": "connector",
                    "description": "Sync GitHub issues into the tickets Domain.",
                    "effects": ["external.read", "domain.write"],
                    "connectors": [{"key": "github", "provider": "github", "auth": "project_vault_binding"}],
                    "executor": {"type": "deferred"},
                }
            },
        },
    )

    source = json.loads(compiled.source_code)
    assert source["actions"] == [{"key": "tickets.syncExternal", "label": "Sync GitHub"}]
    board = source["views"][0]
    assert board["type"] == "board"
    assert board["group_by"] == "status"
    assert board["card"] == {
        "title": "title",
        "subtitle": "repo",
        "badges": ["priority", "status"],
    }
    assert _validation_report(compiled)["status"] == "passed"


def test_compiler_normalizes_generated_ui_string_columns():
    compiled = compile_workspace_app_input(
        action="create",
        name="Ticket Board",
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code=json.dumps(
            {
                "schema_version": 1,
                "title": "Ticket Board",
                "views": [
                    {
                        "type": "table",
                        "title": "Tickets",
                        "columns": ["title", "status", {"field": "assignee"}],
                    }
                ],
            }
        ),
    )

    source = json.loads(compiled.source_code)
    assert source["views"][0]["columns"] == [
        {"key": "title", "label": "Title"},
        {"key": "status", "label": "Status"},
        {"field": "assignee", "key": "assignee", "label": "Assignee"},
    ]
    assert _validation_report(compiled)["status"] == "passed"


def test_compiler_does_not_silently_repair_recordful_app_local_state():
    compiled = compile_workspace_app_input(
        action="create",
        name="Todo List",
        source_code=json.dumps({"rows": [{"title": "Follow up"}]}),
        initial_state={"todos": []},
    )

    report = _validation_report(compiled, initial_state={"todos": []})
    assert report["status"] == "failed"
    assert any("record-like collections" in error for error in report["errors"])
    guidance = contract_repair_guidance(report)
    assert guidance["failure_kind"] == "data_model_requires_domain"
