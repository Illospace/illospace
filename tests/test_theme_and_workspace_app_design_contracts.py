from __future__ import annotations

from pathlib import Path

from brain.systems.workspace_apps.contracts import (
    build_contract_validation_report,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sandboxed_workspace_app_template_passes_app_kit_contract():
    template = (
        REPO_ROOT
        / "brain/systems/skills/builtin_skill_bundles/build-workspace-app/templates/sandboxed-html-app.html"
    ).read_text()
    report = build_contract_validation_report(
        renderer_key="sandboxed-html-app",
        source_kind="html",
        source_code=template,
        manifest={
            "contract_version": 1,
            "data_plan": {"mode": "app_local", "scope": "ui_state"},
            "design_contract": {
                "kit": "constellation-app-kit",
                "theme_modes": ["dark", "light"],
            },
        },
        visual_spec={
            "thumbnail": {
                "label": "State",
                "value": "Live",
            }
        },
        metadata={},
        initial_state={},
    )

    assert report["status"] == "passed"
    assert report["errors"] == []
