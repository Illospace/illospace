from __future__ import annotations

import json
import re
from pathlib import Path

from brain.systems.workspace_apps import service
from brain.systems.workspace_apps.contracts import (
    STRUCTURED_UI_RENDERER_KEY,
    STRUCTURED_UI_SOURCE_KIND,
    build_contract_validation_report,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _theme_color_schemes_from_store(theme_store: str) -> dict[str, str]:
    return dict(re.findall(r"id: '([^']+)'.*?colorScheme: '([^']+)'", theme_store, re.DOTALL))


def _theme_color_schemes_from_boot(app_html: str) -> dict[str, str]:
    return dict(re.findall(r"([a-zA-Z][a-zA-Z0-9_-]*): \{ colorScheme: '([^']+)' \}", app_html))


def _last_style_block(source: str) -> str:
    style_blocks = re.findall(r"<style>(.*?)</style>", source, re.DOTALL)
    assert style_blocks
    return style_blocks[-1]


def test_frontend_theme_uses_named_theme_and_color_scheme_axis():
    app_html = (REPO_ROOT / "frontend/src/app.html").read_text()
    theme_store = (REPO_ROOT / "frontend/src/lib/stores/theme.svelte.ts").read_text()
    frontend_sources = [
        path
        for path in (REPO_ROOT / "frontend/src").rglob("*")
        if path.suffix in {".css", ".svelte", ".ts", ".html"}
    ]

    assert 'data-theme="constellation"' in app_html
    assert 'data-color-scheme="dark"' in app_html
    assert "id: 'constellation'" in theme_store
    assert "id: 'daylight'" in theme_store

    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in frontend_sources
        if "data-theme='light'" in path.read_text() or 'data-theme="light"' in path.read_text()
    ]
    assert offenders == []


def test_frontend_theme_bootstrap_registry_matches_store_registry():
    app_html = (REPO_ROOT / "frontend/src/app.html").read_text()
    theme_store = (REPO_ROOT / "frontend/src/lib/stores/theme.svelte.ts").read_text()

    assert _theme_color_schemes_from_boot(app_html) == _theme_color_schemes_from_store(theme_store)


def test_workspace_app_defaults_prefer_structured_generated_ui():
    schema_path = (
        REPO_ROOT
        / "brain/systems/skills/builtin_skill_bundles/build-workspace-app/schemas/workspace-app-output.schema.json"
    )
    schema = json.loads(schema_path.read_text())

    assert service.DEFAULT_RENDERER_KEY == STRUCTURED_UI_RENDERER_KEY
    assert service.DEFAULT_SOURCE_KIND == STRUCTURED_UI_SOURCE_KIND
    assert schema["properties"]["renderer"]["default"] == STRUCTURED_UI_RENDERER_KEY
    assert STRUCTURED_UI_RENDERER_KEY in schema["properties"]["renderer"]["enum"]
    assert schema["properties"]["source_kind"]["default"] == STRUCTURED_UI_SOURCE_KIND


def test_workspace_app_agent_instructions_are_primitive_first():
    from brain.systems.runs.tool_definitions import WORKSPACE_APP_TOOLS

    tool = next(item for item in WORKSPACE_APP_TOOLS if item["name"] == "manage_workspace_app")
    tool_text = json.dumps(tool).lower()
    skill_text = (
        REPO_ROOT / "brain/systems/skills/builtin_skill_bundles/build-workspace-app/SKILL.md"
    ).read_text().lower()
    bridge_text = (
        REPO_ROOT / "brain/systems/skills/builtin_skill_bundles/build-workspace-app/references/host-bridge.md"
    ).read_text().lower()

    combined = "\n".join([tool_text, skill_text, bridge_text])
    assert "full-code" in combined
    assert "window.illo.domain" in combined
    assert "use-case-specific templates" in combined
    assert "escape-hatch" not in combined
    assert "legacy/escape" not in combined


def test_workspace_app_db_defaults_are_in_canonical_model():
    from brain.platform.db.models.workspace_app import WorkspaceAppVersion

    assert service.DEFAULT_RENDERER_KEY == STRUCTURED_UI_RENDERER_KEY
    assert service.DEFAULT_SOURCE_KIND == STRUCTURED_UI_SOURCE_KIND
    assert str(WorkspaceAppVersion.__table__.c.renderer_key.server_default.arg) == "generated-ui-app"
    assert str(WorkspaceAppVersion.__table__.c.source_kind.server_default.arg) == "json"


def test_generated_app_host_styles_use_theme_tokens():
    checked_files = [
        REPO_ROOT / "frontend/src/lib/components/cortex/GeneratedUiRenderer.svelte",
        REPO_ROOT / "frontend/src/lib/components/cortex/GeneratedHtmlAppRuntime.svelte",
        REPO_ROOT / "frontend/src/lib/components/cortex/GeneratedAppRenderer.svelte",
    ]
    constellation_css = (REPO_ROOT / "frontend/src/lib/styles/constellation.css").read_text()
    raw_color = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")

    assert ".generated-app-shell {" in constellation_css
    assert ".generated-app-shell.is-dock {" in constellation_css
    assert ".generated-app-shell__header {" in constellation_css

    offenders = []
    for path in checked_files:
        source = path.read_text()
        style = _last_style_block(source)
        if (
            "generated-app-shell" not in source
            or raw_color.search(style)
            or "--constellation-surface-floating" in style
            or "backdrop-filter" in style
        ):
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_constellation_global_foundation_light_mode_is_root_tokenized():
    source = (REPO_ROOT / "frontend/src/lib/styles/constellation.css").read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [":root[data-color-scheme='light'] {"]
    assert "--constellation-nav-rail-background" in source
    assert "--constellation-signal-blob-surface-background" in source
    assert "--constellation-astre-core-shadow" in source
    assert "--constellation-orbit-lane-dot-background" in source
    assert ":root[data-color-scheme='light'] ." not in source


def test_daylight_signal_blob_root_tokens_do_not_depend_on_blob_locals():
    source = (REPO_ROOT / "frontend/src/lib/styles/constellation.css").read_text()
    light_block = re.search(r":root\[data-color-scheme='light'\] \{(?P<body>.*?)\n\}", source, re.DOTALL)
    assert light_block is not None

    declarations = [
        declaration.strip()
        for declaration in light_block.group("body").split(";")
        if declaration.strip().startswith("--constellation-signal-blob-")
    ]
    offenders = [declaration for declaration in declarations if "var(--blob" in declaration]

    assert offenders == []


def test_workspace_app_object_preview_style_uses_theme_tokens():
    source = (REPO_ROOT / "frontend/src/lib/components/cortex/CortexWorkspaceAppObject.svelte").read_text()
    style = _last_style_block(source)
    raw_color = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(")

    assert "--workspace-app-accent: var(--positive)" in style
    assert "--workspace-app-hover-shadow: var(--constellation-surface-panel-hover-shadow)" in style
    assert "clip-path: polygon" not in style
    assert re.search(r"\.cortex-workspace-app-object__body\s*\{.*?overflow: hidden;", style, re.DOTALL)
    assert raw_color.search(style) is None


def test_chat_dock_light_mode_is_tokenized_at_shell_boundary():
    source = (REPO_ROOT / "frontend/src/lib/components/chat/ChatDock.svelte").read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [
        ":global(:root[data-color-scheme='light']) .chat-dock-shell,",
        ":global(:root[data-color-scheme='light']) .chat-image-preview-layer {",
    ]
    assert "--chat-message-body-text" in source
    assert "--chat-attachment-preview-background" in source
    assert ":global(:root[data-color-scheme='light']) .chat-message" not in source


def test_cortex_workspace_chat_light_mode_is_page_tokenized():
    source = (REPO_ROOT / "frontend/src/routes/cortex/+page.svelte").read_text()

    assert ":global(:root[data-color-scheme='light']) .cortex-page {" in source
    assert "--workspace-chat-expanded-background" in source
    assert "background: var(--cortex-panel-open-overlay-background)" in source
    assert "data-color-scheme='light']) .workspace-chat" not in source
    assert "data-color-scheme='light']) .cortex-main::after" not in source


def test_thread_stage_sits_between_compact_and_foreground_chat_layers():
    page = (REPO_ROOT / "frontend/src/routes/cortex/+page.svelte").read_text()
    workspace_chat = (REPO_ROOT / "frontend/src/lib/components/cortex/WorkspaceChatDock.css").read_text()
    thread_shell = (REPO_ROOT / "frontend/src/lib/components/cortex/ThreadStageShell.svelte").read_text()

    backdrop_start = page.index("<ConstellationWorkspaceBackdrop")
    backdrop_end = page.index("</ConstellationWorkspaceBackdrop>", backdrop_start)
    backdrop_markup = page[backdrop_start:backdrop_end]

    scaffold_start = page.index("<CortexWorkspaceMigrationScaffold")
    scaffold_end = page.index("</CortexWorkspaceMigrationScaffold>", scaffold_start)
    scaffold_markup = page[scaffold_start:scaffold_end]

    assert "CortexThreadStageLiveBridgeComponent" not in backdrop_markup
    assert "{#snippet overlays()}" in scaffold_markup
    assert "CortexThreadStageLiveBridgeComponent" in scaffold_markup
    assert re.search(r"\.workspace-chat-dock\s*\{.*?z-index: 2;", workspace_chat, re.DOTALL)
    assert re.search(r"\.thread-stage-shell\s*\{.*?z-index: 25;", thread_shell, re.DOTALL)
    assert re.search(r"\.workspace-chat-dock\.is-foreground\s*\{.*?z-index: 120;", workspace_chat, re.DOTALL)


def test_cortex_list_view_light_mode_is_component_tokenized():
    source = (REPO_ROOT / "frontend/src/lib/components/cortex/CortexListView.svelte").read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [":global(:root[data-color-scheme='light']) .list-view {"]
    assert "--list-item-selected-background-base" in source
    assert "data-color-scheme='light']) .list-item" not in source


def test_project_context_picker_light_mode_is_component_tokenized():
    source = (
        REPO_ROOT / "frontend/src/lib/components/cortex/project-context/projectContextPicker.css"
    ).read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [
        ":root[data-color-scheme='light'] .project-context-composer {",
        ":root[data-color-scheme='light'] .project-context-modal-backdrop {",
    ]
    assert "--project-context-popover-background" in source
    assert "--project-context-repo-option-hover-background" in source
    assert "--project-context-chip-invalid-border" in source
    assert "data-color-scheme='light'] .project-context-chip" not in source
    assert "data-color-scheme='light'] .github-repo-option" not in source


def test_cortex_panel_light_mode_is_boundary_tokenized():
    source = (REPO_ROOT / "frontend/src/lib/components/cortex/CortexPanel.svelte").read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [
        ":global(:root[data-color-scheme='light']) .panel-main {",
        ":global(:root[data-color-scheme='light']) .panel-utility,",
        ":global(:root[data-color-scheme='light']) .panel-utility-content-bare {",
    ]
    assert "--thread-project-context-chip-border" in source
    assert "--thread-mention-dropdown-background" in source
    assert "--panel-utility-card-background" in source
    assert "data-color-scheme='light']) .thread-composer-textarea" not in source
    assert "data-color-scheme='light']) .audit-card" not in source


def test_cortex_thread_stage_main_column_light_mode_is_boundary_tokenized():
    source = (
        REPO_ROOT
        / "frontend/src/lib/components/cortex/migration/CortexThreadStageMainColumn.svelte"
    ).read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [
        ":global(:root[data-color-scheme='light']) .cortex-thread-stage-main-column {",
    ]
    assert "--thread-run-border-running" in source
    assert "--thread-run-chevron-text" in source
    assert "--thread-run-evidence-surface-background" in source
    assert "--thread-reply-placeholder-background" in source
    assert "data-color-scheme='light']) .cortex-thread-stage-main-column .run-" not in source
    assert "data-color-scheme='light']) .cortex-thread-stage-main-column .thread-thinking" not in source


def test_cortex_thread_stage_migration_surfaces_keep_light_mode_at_root_boundary():
    expected_boundaries = {
        "frontend/src/lib/components/cortex/migration/CortexThreadStageRightDock.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-thread-stage-right-dock {",
        ],
        "frontend/src/lib/components/cortex/migration/CortexThreadStageLiveBridge.svelte": [
            ":global(:root[data-color-scheme='light']) .thread-stage-panel {",
        ],
        "frontend/src/lib/components/cortex/migration/CortexThreadStageMigrationScreen.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-thread-stage-migration-screen {",
        ],
    }

    for relative_path, expected_lines in expected_boundaries.items():
        source = (REPO_ROOT / relative_path).read_text()
        light_lines = [
            line.strip()
            for line in source.splitlines()
            if "data-color-scheme='light'" in line
        ]
        assert light_lines == expected_lines

    right_dock = (
        REPO_ROOT / "frontend/src/lib/components/cortex/migration/CortexThreadStageRightDock.svelte"
    ).read_text()
    live_bridge = (
        REPO_ROOT / "frontend/src/lib/components/cortex/migration/CortexThreadStageLiveBridge.svelte"
    ).read_text()
    migration_screen = (
        REPO_ROOT
        / "frontend/src/lib/components/cortex/migration/CortexThreadStageMigrationScreen.svelte"
    ).read_text()

    assert "--right-dock-add-menu-background" in right_dock
    assert "--thread-bridge-mention-dropdown-background" in live_bridge
    assert "--migration-browser-surface-background" in migration_screen
    assert "data-color-scheme='light']) .right-dock-tab" not in right_dock
    assert "data-color-scheme='light']) .mention-dropdown" not in live_bridge
    assert "data-color-scheme='light']) .thread-browser-surface" not in migration_screen


def test_cortex_auxiliary_surfaces_keep_light_mode_at_root_boundary():
    expected_boundaries = {
        "frontend/src/lib/components/cortex/CortexWorkspacePin.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-workspace-pin {",
        ],
        "frontend/src/lib/components/cortex/CortexDeepField.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-deep-field {",
        ],
        "frontend/src/lib/components/cortex/CortexWorkspaceMenu.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-workspace-menu {",
        ],
        "frontend/src/lib/components/cortex/CortexWorkspacePinMenu.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-workspace-pin-menu {",
        ],
        "frontend/src/lib/components/cortex/AiPromptComposer.svelte": [
            ":global(:root[data-color-scheme='light']) .ai-prompt-composer {",
        ],
    }

    for relative_path, expected_lines in expected_boundaries.items():
        source = (REPO_ROOT / relative_path).read_text()
        light_lines = [
            line.strip()
            for line in source.splitlines()
            if "data-color-scheme='light'" in line
        ]
        assert light_lines == expected_lines


def test_sandboxed_workspace_app_template_still_passes_app_kit_contract():
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
            "design_contract": {"kit": "constellation-app-kit", "theme_modes": ["dark", "light"]},
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
