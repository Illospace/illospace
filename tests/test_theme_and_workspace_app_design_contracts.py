from __future__ import annotations

import json
import re
from pathlib import Path

from brain.systems.workspace_apps import service
from brain.systems.workspace_apps.contracts import (
    APP_CAPSULE_RENDERER_KEY,
    APP_CAPSULE_SOURCE_KIND,
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


def test_thread_markdown_uses_readable_prose_primitive():
    components_css = (REPO_ROOT / "frontend/src/lib/styles/components.css").read_text()
    transcript = (
        REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadTranscript.svelte"
    ).read_text()
    visual_block = (
        REPO_ROOT / "frontend/src/lib/features/threads/components/StreamVisualBlock.svelte"
    ).read_text()

    assert ".constellation-prose {" in components_css
    assert "letter-spacing: 0;" in components_css
    assert "text-transform: none;" in components_css
    assert ".constellation-prose code {" in components_css
    assert "font-family: var(--font-mono);" in components_css
    assert "color: inherit;" in components_css
    assert "border: 1px solid var(--content-code-border)" not in components_css.split(
        ".constellation-prose .md-inline-code", 1
    )[1].split(".constellation-prose .md-code-block", 1)[0]
    assert 'class="thread-message-html constellation-prose"' in transcript
    assert 'class="markdown-view constellation-prose"' in visual_block
    assert ".thread-message-html :global(h1)" not in transcript
    assert ".markdown-view :global(h1)" not in visual_block


def test_workspace_pages_open_as_cortex_modals():
    nav_rail = (REPO_ROOT / "frontend/src/lib/components/layout/ConstellationNavRail.svelte").read_text()
    cortex_route = (
        REPO_ROOT / "frontend/src/lib/features/cortex/components/CortexWorkspaceRoute.svelte"
    ).read_text()
    modal_shell = (
        REPO_ROOT / "frontend/src/lib/features/cortex/components/WorkspacePageModal.svelte"
    ).read_text()
    modal_contract = (
        REPO_ROOT / "frontend/src/lib/features/cortex/domain/workspacePageModal.ts"
    ).read_text()

    assert "buildCortexWorkspacePageHref" in nav_rail
    assert "workspacePageModalIdForPath(item.href)" in nav_rail
    assert "WorkspacePageModal" in cortex_route
    assert "activeWorkspacePageModalId" in cortex_route
    for section in ["cycles", "skills", "team", "vault", "system"]:
        assert f"case '{section}':" in cortex_route
        assert f"id: '{section}'" in modal_contract
        route_redirect = (REPO_ROOT / f"frontend/src/routes/{section}/+page.ts").read_text()
        assert f"buildCortexWorkspacePageHref('{section}'" in route_redirect

    assert 'role="dialog"' in modal_shell
    assert "workspace-page-modal__header" in modal_shell
    assert "workspace-page-modal__page-actions" in modal_shell
    assert "registerActions" in modal_shell
    assert "registerRefreshAction" in modal_shell
    assert 'name="refresh"' in modal_shell
    assert "ConstellationIconButton" in modal_shell
    page_frame = (REPO_ROOT / "frontend/src/lib/components/constellation/ConstellationPageFrame.svelte").read_text()
    assert "registerActions(actions)" in page_frame
    assert "!embeddedInWorkspacePageModal && Boolean(showHeaderCopy || actions)" in page_frame
    for section in ["cycles", "skills", "team", "vault", "system"]:
        route_page = (REPO_ROOT / f"frontend/src/routes/{section}/+page.svelte").read_text()
        assert "registerRefreshAction" in route_page
        assert 'name="refresh"' not in route_page
        assert ">Refresh<" not in route_page
        assert 'title="Refresh"' not in route_page


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


def test_workspace_app_defaults_prefer_app_capsules():
    schema_path = (
        REPO_ROOT
        / "brain/systems/skills/builtin_skill_bundles/build-workspace-app/schemas/workspace-app-output.schema.json"
    )
    schema = json.loads(schema_path.read_text())

    assert service.DEFAULT_RENDERER_KEY == APP_CAPSULE_RENDERER_KEY
    assert service.DEFAULT_SOURCE_KIND == APP_CAPSULE_SOURCE_KIND
    assert schema["properties"]["renderer"]["default"] == APP_CAPSULE_RENDERER_KEY
    assert APP_CAPSULE_RENDERER_KEY in schema["properties"]["renderer"]["enum"]
    assert STRUCTURED_UI_RENDERER_KEY in schema["properties"]["renderer"]["enum"]
    assert schema["properties"]["source_kind"]["default"] == APP_CAPSULE_SOURCE_KIND
    assert STRUCTURED_UI_SOURCE_KIND in schema["properties"]["source_kind"]["enum"]


def test_workspace_app_db_defaults_are_in_canonical_model():
    from brain.platform.db.models.workspace_app import WorkspaceAppVersion

    assert service.DEFAULT_RENDERER_KEY == APP_CAPSULE_RENDERER_KEY
    assert service.DEFAULT_SOURCE_KIND == APP_CAPSULE_SOURCE_KIND
    assert str(WorkspaceAppVersion.__table__.c.renderer_key.server_default.arg) == "app-capsule"
    assert str(WorkspaceAppVersion.__table__.c.source_kind.server_default.arg) == "html"


def test_generated_app_host_styles_use_theme_tokens():
    checked_files = [
        REPO_ROOT / "frontend/src/lib/features/workspace-apps/components/GeneratedUiRenderer.svelte",
        REPO_ROOT / "frontend/src/lib/features/workspace-apps/components/GeneratedHtmlAppRuntime.svelte",
        REPO_ROOT / "frontend/src/lib/features/workspace-apps/components/AppCapsuleRenderer.svelte",
        REPO_ROOT / "frontend/src/lib/features/workspace-apps/components/GeneratedAppRenderer.svelte",
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


def test_app_capsule_runtime_uses_new_bridge_and_responsive_surface():
    dispatcher = (
        REPO_ROOT / "frontend/src/lib/features/workspace-apps/components/GeneratedAppRenderer.svelte"
    ).read_text()
    capsule = (
        REPO_ROOT / "frontend/src/lib/features/workspace-apps/components/AppCapsuleRenderer.svelte"
    ).read_text()
    runtime_dir = REPO_ROOT / "frontend/src/lib/features/workspace-apps/runtime"
    capsule_runtime = "\n".join(
        (runtime_dir / filename).read_text()
        for filename in ["appCapsuleRuntime.ts", "appCapsuleBridge.ts", "appCapsuleStyle.ts"]
    )

    assert "AppCapsuleRenderer" in dispatcher
    assert "app.renderer_key === 'app-capsule'" in dispatcher
    assert "activeVersion?.source_kind === 'html'" not in dispatcher.split("const canRenderHtml", 1)[1]

    assert "runWorkspaceAppBinding" in capsule
    assert "window.illo.data(alias)" in capsule_runtime
    assert "state: stateApi" in capsule_runtime
    assert "window.illo.domain" not in capsule_runtime
    assert "illo:ready" in capsule_runtime
    assert "did not finish connecting" in capsule
    assert "width: min(1400px" in capsule
    assert "#57CFA0" not in capsule
    assert "rgba(252, 248" not in capsule_runtime


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


def test_constellation_skeletons_use_theme_tokens():
    constellation_css = (REPO_ROOT / "frontend/src/lib/styles/constellation.css").read_text()
    components_css = (REPO_ROOT / "frontend/src/lib/styles/components.css").read_text()
    skeleton_component = (
        REPO_ROOT / "frontend/src/lib/components/constellation/ConstellationSkeletonBlock.svelte"
    ).read_text()

    for token in [
        "--constellation-skeleton-fill",
        "--constellation-skeleton-fill-soft",
        "--constellation-skeleton-shimmer",
        "--constellation-skeleton-row-background",
        "--constellation-skeleton-row-shimmer",
        "--constellation-skeleton-panel-background",
    ]:
        assert constellation_css.count(token) >= 2
        assert f"var({token})" in skeleton_component or token.endswith("row-background") or token.endswith("row-shimmer")

    assert "var(--skeleton-base)" in components_css
    assert "var(--skeleton-highlight)" in components_css
    assert "rgba(255, 255, 255" not in _last_style_block(skeleton_component)

    for route in ["vault", "cycles", "skills"]:
        route_source = (REPO_ROOT / f"frontend/src/routes/{route}/+page.svelte").read_text()
        assert "var(--constellation-skeleton-row-shimmer)" in route_source
        assert "var(--constellation-skeleton-row-background)" in route_source


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
    source = (REPO_ROOT / "frontend/src/lib/features/workspace-scene/renderers/WorkspaceAppObject.svelte").read_text()
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
    preview_source = (REPO_ROOT / "frontend/src/lib/components/chat/AttachmentPreviewDialog.svelte").read_text()

    assert light_lines == [":global(:root[data-color-scheme='light']) .chat-dock-shell {"]
    assert ":global(:root[data-color-scheme='light']) .attachment-preview-dialog-layer {" in preview_source
    assert "--chat-message-body-text" in source
    assert "--chat-attachment-preview-background" in source
    assert ":global(:root[data-color-scheme='light']) .chat-message" not in source


def test_cortex_workspace_chat_light_mode_is_page_tokenized():
    source = (REPO_ROOT / "frontend/src/lib/features/cortex/components/CortexWorkspaceRoute.svelte").read_text()

    assert ":global(:root[data-color-scheme='light']) .cortex-page {" in source
    assert "--workspace-chat-expanded-background" in source
    assert "background: transparent" in source
    assert "data-color-scheme='light']) .workspace-chat" not in source
    assert "data-color-scheme='light']) .cortex-main::after" not in source


def test_thread_stage_sits_between_compact_and_foreground_chat_layers():
    page = (REPO_ROOT / "frontend/src/lib/features/cortex/components/CortexWorkspaceRoute.svelte").read_text()
    workspace_chat = (REPO_ROOT / "frontend/src/lib/features/cortex/components/chat/WorkspaceChatDock.css").read_text()
    thread_shell = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageShell.svelte").read_text()

    backdrop_start = page.index("<ConstellationWorkspaceBackdrop")
    backdrop_end = page.index("</ConstellationWorkspaceBackdrop>", backdrop_start)
    backdrop_markup = page[backdrop_start:backdrop_end]

    stage_start = page.index("{#if cortex.panelOpen && ThreadStageScreenComponent}")
    stage_end = page.index("{#if !cortex.panelOpen && activeWorkspaceApp", stage_start)
    stage_markup = page[stage_start:stage_end]

    assert "ThreadStageScreenComponent" not in backdrop_markup
    assert "ThreadStageScreenComponent" in stage_markup
    assert re.search(r"\.workspace-chat-dock\s*\{.*?z-index: 2;", workspace_chat, re.DOTALL)
    assert re.search(r"\.thread-stage-shell\s*\{.*?z-index: 25;", thread_shell, re.DOTALL)
    assert re.search(r"\.workspace-chat-dock\.is-foreground\s*\{.*?z-index: 120;", workspace_chat, re.DOTALL)


def test_thread_stage_open_motion_keeps_cortex_static():
    page = (REPO_ROOT / "frontend/src/lib/features/cortex/components/CortexWorkspaceRoute.svelte").read_text()
    thread_shell = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageShell.svelte").read_text()
    controller = (
        REPO_ROOT / "frontend/src/lib/features/cortex/controllers/workspaceThreadStageController.svelte.ts"
    ).read_text()
    shell_style = _last_style_block(thread_shell)
    panel_open_rule = re.search(r"\.panel-open \.cortex-main\s*\{(?P<body>.*?)\}", page, re.DOTALL)
    panel_overlay_rule = re.search(r"\.panel-open \.cortex-main::after\s*\{(?P<body>.*?)\}", page, re.DOTALL)

    assert "threadStagePrewarmQueued" in page
    assert "runWhenBrowserIdle(() => ensureThreadStageScreenLoaded()" in page
    assert "threadStage.syncPanelOpen(cortex.panelOpen && Boolean(ThreadStageScreenComponent))" in page
    assert "clip-path:" not in shell_style
    assert "thread-origin-bloom" not in thread_shell
    assert "thread-origin-ring" not in thread_shell
    assert "thread-shell-presence" in thread_shell
    assert "thread-shell-reveal" not in thread_shell
    assert panel_open_rule
    assert "opacity: 1;" in panel_open_rule.group("body")
    assert "transform: none;" in panel_open_rule.group("body")
    assert panel_overlay_rule
    assert "opacity: 0;" in panel_overlay_rule.group("body")
    assert "}, 32);" in controller
    assert "}, 460);" in controller


def test_thread_stage_expands_while_reading_column_stays_bounded():
    thread_shell = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageShell.svelte").read_text()
    stage_screen = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte").read_text()

    assert "--thread-stage-frame-width: clamp(1040px, 78vw, 1760px);" in thread_shell
    assert "width: min(100%, var(--thread-stage-frame-width));" in thread_shell
    assert "--thread-stage-thread-max: clamp(860px, 74vw, 1560px);" in stage_screen
    assert "--thread-stage-readable-max: 860px;" in stage_screen
    assert "width: min(100%, var(--thread-stage-thread-max));" in stage_screen
    assert "--thread-column-max: var(--thread-stage-readable-max);" in stage_screen
    assert "--thread-column-max: var(--thread-stage-thread-max);" not in stage_screen


def test_thread_stage_right_dock_does_not_auto_open_from_loaded_thread_state():
    source = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte").read_text()
    idea_change_body = source.split("const currentIdeaId = idea?.id ?? null;", 1)[1].split(
        "$effect(() => {\n    if (!idea) return;",
        1,
    )[0]

    assert "onBrowserOpenChange?.(false);" in idea_change_body
    assert "sidePanelTabs = createDefaultThreadSidePanelTabs();" in idea_change_body
    assert "lastAutoOpenedBrowserSessionId" not in source
    assert "lastAutoOpenedVaultPromptId" not in source
    assert "lastAutoOpenedVaultGrantPromptId" not in source
    assert "lastAutoOpenedCycleSignal" not in source
    assert "lastAutoOpenedCodeReviewSignature" not in source
    assert "lastAutoSelectedAppId" not in source


def test_thread_transcript_keeps_illo_mark_without_illo_message_meta_header():
    source = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadTranscript.svelte").read_text()
    message_block = source.split("{:else if item.kind === 'message'}", 1)[1].split(
        "{:else if item.kind === 'visual'}",
        1,
    )[0]

    assert "<ThreadAuthorMark" in message_block
    assert "{#if !isIllo}" in message_block
    assert "{#if !isIllo || hasSupplementalMeta}" not in message_block
    assert ".thread-message-illo .thread-message-content" in source


def test_thread_stage_dismiss_preserves_mounted_workspace_scene():
    source = (REPO_ROOT / "frontend/src/lib/features/cortex/components/CortexWorkspaceRoute.svelte").read_text()
    dismiss_body = source.split("function handleThreadStageDismiss()", 1)[1].split(
        "async function loadWorkspaceSceneSidecars()",
        1,
    )[0]

    assert "const shouldRefreshWorkspaceSceneSidecars = directThreadActive || !workspaceSceneSidecarsReady;" in dismiss_body
    assert "if (shouldRefreshWorkspaceSceneSidecars)" in dismiss_body
    assert "void loadWorkspaceSceneSidecars();" in dismiss_body


def test_cortex_list_view_light_mode_is_component_tokenized():
    source = (REPO_ROOT / "frontend/src/lib/features/cortex/components/ListView.svelte").read_text()
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
        REPO_ROOT / "frontend/src/lib/features/composer/components/project-context/projectContextPicker.css"
    ).read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [
        ":root[data-color-scheme='light'] .project-context-composer {",
        ":root[data-color-scheme='light'] .project-context-modal-backdrop {",
        ":root[data-color-scheme='light'] .project-access-menu {",
    ]
    assert "--project-context-popover-background" in source
    assert "--project-context-repo-option-hover-background" in source
    assert "--project-context-chip-invalid-border" in source
    assert "data-color-scheme='light'] .project-context-chip" not in source
    assert "data-color-scheme='light'] .github-repo-option" not in source


def test_cortex_thread_stage_screen_light_mode_is_boundary_tokenized():
    source = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte").read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [
        ":global(:root[data-color-scheme='light']) .thread-stage-panel {",
    ]
    assert "--thread-stage-panel-backdrop-filter" in source
    assert "--thread-stage-panel-before-filter" in source
    assert "--thread-stage-docked-header-height" in source
    assert "data-color-scheme='light']) .thread-stage-thread" not in source
    assert "data-color-scheme='light']) .thread-stage-dock" not in source


def test_cortex_panel_light_mode_is_boundary_tokenized():
    stage = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte").read_text()
    utility = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadUtilityContent.svelte").read_text()
    composer = (REPO_ROOT / "frontend/src/lib/features/composer/components/WorkspaceComposerAdapter.svelte").read_text()

    assert ":global(:root[data-color-scheme='light']) .thread-stage-panel {" in stage
    assert ":global(:root[data-color-scheme='light']) .composer-chip-group {" in composer
    assert "--thread-bridge-mention-dropdown-background" in stage
    assert "--panel-utility-card-background" in utility
    assert "data-color-scheme='light']) .audit-card" not in utility


def test_cortex_thread_stage_right_dock_uses_container_width_guard():
    source = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte").read_text()

    assert "container: thread-stage-panel / inline-size;" in source
    assert "@container thread-stage-panel (max-width: 1040px)" in source
    assert "calc(100% - var(--thread-stage-thread-min) - var(--thread-stage-gutter))" in source
    assert "grid-template-columns:\n        minmax(var(--thread-stage-thread-min), 1fr)" in source


def test_cortex_thread_stage_main_column_light_mode_is_boundary_tokenized():
    source = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadTranscript.svelte").read_text()
    light_lines = [
        line.strip()
        for line in source.splitlines()
        if "data-color-scheme='light'" in line
    ]

    assert light_lines == [
        ":global(:root[data-color-scheme='light']) .thread-transcript {",
    ]
    assert "--thread-run-border-running" in source
    assert "--thread-run-chevron-text" in source
    assert "--thread-reply-placeholder-background" in source
    assert "data-color-scheme='light']) .thread-transcript .run-" not in source
    assert "data-color-scheme='light']) .thread-transcript .thread-thinking" not in source


def test_cortex_thread_stage_surfaces_keep_light_mode_at_root_boundary():
    expected_boundaries = {
        "frontend/src/lib/features/threads/components/ThreadStageRightDock.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-thread-stage-right-dock {",
        ],
        "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte": [
            ":global(:root[data-color-scheme='light']) .thread-stage-panel {",
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

    right_dock = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageRightDock.svelte").read_text()
    stage_screen = (REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte").read_text()

    assert "--right-dock-add-menu-background" in right_dock
    assert "--thread-stage-panel-before-filter" in stage_screen
    assert "--thread-bridge-mention-dropdown-background" in stage_screen
    assert "data-color-scheme='light']) .right-dock-tab" not in right_dock
    assert "data-color-scheme='light']) .thread-stage-layout" not in stage_screen
    assert "data-color-scheme='light']) .mention-dropdown" not in stage_screen


def test_cortex_auxiliary_surfaces_keep_light_mode_at_root_boundary():
    expected_boundaries = {
        "frontend/src/lib/features/workspace-scene/renderers/WorkspacePin.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-workspace-pin {",
        ],
        "frontend/src/lib/features/cortex/components/menus/WorkspaceMenu.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-workspace-menu {",
        ],
        "frontend/src/lib/features/cortex/components/menus/WorkspacePinMenu.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-workspace-pin-menu {",
        ],
        "frontend/src/lib/features/cortex/components/menus/UserMenu.svelte": [
            ":global(:root[data-color-scheme='light']) .cortex-user-menu {",
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
