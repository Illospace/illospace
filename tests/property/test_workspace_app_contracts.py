from brain.systems.workspace_apps.contracts import build_contract_validation_report


def _manifest(**overrides):
    manifest = {
        "contract_version": 1,
        "data_plan": {"mode": "app_local", "scope": "ui_state"},
        "design_contract": {"kit": "constellation-app-kit", "theme_modes": ["dark", "light"]},
    }
    manifest.update(overrides)
    return manifest


def test_workspace_app_contract_accepts_minimal_app_local_generated_ui():
    report = build_contract_validation_report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code='{"schema_version":1,"views":[{"id":"summary","type":"metrics","metrics":[]}]}',
        manifest=_manifest(),
        initial_state={"count": 1},
    )

    assert report["ok"] is True
    assert report["errors"] == []


def test_workspace_app_contract_rejects_missing_design_and_domain_bindings():
    missing_design = build_contract_validation_report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code='{"schema_version":1,"views":[]}',
        manifest={"contract_version": 1, "data_plan": {"mode": "app_local", "scope": "ui_state"}},
    )
    missing_bindings = build_contract_validation_report(
        renderer_key="generated-ui-app",
        source_kind="json",
        source_code='{"schema_version":1,"views":[]}',
        manifest=_manifest(data_plan={"mode": "domain"}),
    )

    assert "manifest.design_contract is required" in missing_design["errors"]
    assert "manifest.data_plan.bindings is required for Domain-backed apps" in missing_bindings["errors"]
