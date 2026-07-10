import json
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTE_PATH = REPO_ROOT / "frontend/src/lib/features/cortex/components/CortexWorkspaceRoute.svelte"
APP_TEMPLATE_PATH = REPO_ROOT / "frontend/src/app.html"


def _route_source() -> str:
    return ROUTE_PATH.read_text()


def _preloaded_paths(url: str) -> list[str]:
    app_template = APP_TEMPLATE_PATH.read_text()
    preload_script = next(
        script
        for script in re.findall(r"<script>(.*?)</script>", app_template, flags=re.DOTALL)
        if "/api/cortex/bootstrap?" in script
    )
    harness = f"""
const paths = [];
globalThis.location = new URL({json.dumps(url)});
globalThis.window = {{}};
globalThis.fetch = (path) => {{
  paths.push(path);
  return new Promise(() => {{}});
}};
{preload_script}
console.log(JSON.stringify(paths));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", harness],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_cold_workspace_page_modal_defers_cortex_startup():
    source = _route_source()
    startup = source.split("function startCortexWorkspace()", 1)[1].split(
        "async function closeWorkspacePageModal()",
        1,
    )[0]
    mount = source.split("onMount(() => {", 1)[1].split("onDestroy(() => {", 1)[0]

    guard = "if (!browser || !workspaceRouteMounted || workspaceStartupStarted) return;"
    assert guard in startup
    assert startup.index(guard) < startup.index("workspaceStartupStarted = true;")
    assert "cortex.setupWs();" in startup
    assert "loadWorkspaceSceneSidecars()" in startup
    assert "cortex.setupWs();" not in mount
    assert "if (!activeWorkspacePageModalId || requestedThreadIdeaIdFromPage()) startCortexWorkspace();" in mount

    unguarded_effects = [
        effect
        for effect in source.split("$effect(() => {")[1:]
        if not effect.lstrip().startswith("if (!workspaceStartupStarted) return;")
    ]
    assert len(unguarded_effects) == 1
    assert unguarded_effects[0].lstrip().startswith("const modalId = activeWorkspacePageModalId;")


def test_closing_workspace_page_modal_starts_workspace_exactly_once():
    source = _route_source()
    startup = source.split("function startCortexWorkspace()", 1)[1].split(
        "async function closeWorkspacePageModal()",
        1,
    )[0]
    close = source.split("async function closeWorkspacePageModal()", 1)[1].split("$effect(() => {", 1)[0]
    modal_effect = source.split("const modalId = activeWorkspacePageModalId;", 1)[1].split(
        "async function handleThreadOpen",
        1,
    )[0]

    assert startup.count("cortex.setupWs();") == 1
    assert "workspaceStartupStarted" in startup
    assert close.index("await goto(") < close.index("startCortexWorkspace();")
    assert "if (workspaceRouteMounted) startCortexWorkspace();" in modal_effect


def test_cold_modal_does_not_mount_hidden_workspace_surface():
    source = _route_source()
    markup = source.split("<div class=\"cortex-workspace\" bind:this={workspaceEl}>", 1)[1].split(
        "<style>",
        1,
    )[0]

    workspace_gate = markup.index("{#if workspaceStartupStarted}")
    workspace_surface = markup.index("<ConstellationWorkspaceBackdrop")
    workspace_gate_end = markup.index("{/if}", markup.index("<CortexWorkspacePinMenuComponent"))
    modal = markup.index("{#if activeWorkspacePageModal}")

    assert workspace_gate < workspace_surface < workspace_gate_end < modal


@pytest.mark.parametrize("modal_id", ["cycles", "skills", "team", "vault", "system"])
def test_valid_modal_first_url_skips_cortex_bootstrap_preload(modal_id: str):
    paths = _preloaded_paths(f"https://illo.test/cortex?modal={modal_id}")

    assert not any(path.startswith("/api/cortex/bootstrap?") for path in paths)


def test_normal_cortex_url_keeps_workspace_bootstrap_preload():
    paths = _preloaded_paths("https://illo.test/cortex")

    assert "/api/cortex/bootstrap?include=core%2Cworkspace" in paths


def test_invalid_modal_keeps_workspace_bootstrap_preload():
    paths = _preloaded_paths("https://illo.test/cortex?modal=unknown")

    assert "/api/cortex/bootstrap?include=core%2Cworkspace" in paths


def test_direct_thread_keeps_direct_bootstrap_even_with_valid_modal():
    paths = _preloaded_paths("https://illo.test/cortex?modal=cycles&idea=idea-1")

    assert "/api/cortex/bootstrap?include=selected_idea%2Cdirect_thread&idea_id=idea-1" in paths
