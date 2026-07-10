from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
STAGE_PATH = REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadStageScreen.svelte"
REGISTRY_PATH = REPO_ROOT / "frontend/src/lib/features/threads/controllers/threadLazyModuleRegistry.ts"
COMPOSER_ADAPTER_PATH = REPO_ROOT / "frontend/src/lib/features/composer/components/WorkspaceComposerAdapter.svelte"
SETTINGS_MENU_PATH = REPO_ROOT / "frontend/src/lib/features/composer/components/WorkspaceComposerSettingsMenu.svelte"
TRANSCRIPT_PATH = REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadTranscript.svelte"
DISCUSSION_PATH = REPO_ROOT / "frontend/src/lib/features/threads/components/ThreadDiscussionPane.svelte"
LAZY_REFERENCE_PREVIEW_PATH = (
    REPO_ROOT / "frontend/src/lib/features/threads/components/LazyObjectReferencePreviewList.svelte"
)
LAZY_VISUAL_BLOCK_PATH = (
    REPO_ROOT / "frontend/src/lib/features/threads/components/LazyStreamVisualBlock.svelte"
)

RARE_THREAD_PANES = {
    "browser": (
        "BrowserThoughtPanel",
        "$lib/features/browser-sessions/components/BrowserThoughtPanel.svelte",
    ),
    "cycles": (
        "ThreadCyclesPane",
        "$lib/features/cycles/components/ThreadCyclesPane.svelte",
    ),
    "project": (
        "ProjectDraftStatePanel",
        "$lib/features/threads/components/ProjectDraftStatePanel.svelte",
    ),
    "preview": (
        "ThreadAttachmentPreviewPane",
        "$lib/features/threads/components/ThreadAttachmentPreviewPane.svelte",
    ),
    "code-review": (
        "ThreadCodeReviewPane",
        "$lib/features/threads/components/ThreadCodeReviewPane.svelte",
    ),
    "file-preview": (
        "ThreadProjectFilePreviewPane",
        "$lib/features/threads/components/ThreadProjectFilePreviewPane.svelte",
    ),
    "app": (
        "ThreadAppsPane",
        "$lib/features/workspace-apps/components/ThreadAppsPane.svelte",
    ),
    "vault": (
        "VaultPage",
        "../../../../routes/vault/+page.svelte",
    ),
}


def _stage_source() -> str:
    return STAGE_PATH.read_text()


def _registry_source() -> str:
    return REGISTRY_PATH.read_text()


def test_rare_thread_panes_are_dynamic_imports_only():
    source = _stage_source()
    registry = _registry_source()
    script = source.split("</script>", 1)[0]

    for kind, (component, module_path) in RARE_THREAD_PANES.items():
        assert f"import {component} from '{module_path}';" not in script
        assert f"{kind!r}: async () =>" in script
        assert f"import('{module_path}')" in registry
        assert f"{component}Component" in script


def test_only_the_selected_rare_pane_triggers_loading():
    source = _stage_source()

    effect = source.split("const activeKind = activeSidePanelTab?.kind;", 1)[1].split(
        "});",
        1,
    )[0]
    assert "isLazyThreadPaneKind(activeKind)" in effect
    assert "ensureThreadPaneLoaded(activeKind);" in effect
    assert source.count("ensureThreadPaneLoaded(") == 3

    for kind, (component, _) in RARE_THREAD_PANES.items():
        assert f"{{#if {component}Component}}" in source
        assert f"{{@render lazyPaneStatus('{kind}')}}" in source


def test_default_thread_panes_remain_eager_and_lazy_failures_are_retryable():
    source = _stage_source()

    assert "import ThreadDiscussionPane from" in source
    assert "import ThreadUtilityContent from" in source
    assert "threadModuleLoadErrors[kind]" in source
    assert 'onclick={() => ensureThreadPaneLoaded(kind)}' in source
    assert ">Retry</button>" in source


def test_project_context_picker_loads_on_interaction_without_losing_pending_context():
    source = _stage_source()
    registry = _registry_source()
    module_path = "$lib/features/composer/components/ProjectContextPicker.svelte"

    assert f"import ProjectContextPicker from '{module_path}';" not in source
    assert "'project-context': async () =>" in source
    assert f"import('{module_path}')" in registry
    assert "onclick={openLazyProjectContextPicker}" in source
    assert "initialOpen={projectContextPickerRequestedOpen}" in source

    pending_state_effect = source.split("if (ProjectContextPickerComponent) return;", 1)[1].split(
        "});",
        1,
    )[0]
    assert "validateProjectContextResources" in pending_state_effect
    assert "snapshot," in pending_state_effect
    assert "resourceCount:" in pending_state_effect


def test_voice_dictation_controller_and_recording_ui_load_on_first_use():
    source = _stage_source()
    registry = _registry_source()
    controller_path = "$lib/features/composer/controllers/workspaceVoiceDictation.svelte.ts"
    recording_path = "$lib/features/composer/components/WorkspaceVoiceRecording.svelte"

    assert f"import {{ WorkspaceVoiceDictationController }} from '{controller_path}';" not in source
    assert f"import WorkspaceVoiceRecording from '{recording_path}';" not in source
    assert "'voice-dictation': async () =>" in source
    assert f"import('{controller_path}')" in registry
    assert f"import('{recording_path}')" in registry
    assert "await ensureThreadModuleLoaded('voice-dictation');" in source
    assert "if (voiceDictation?.isReady) voiceDictation.toggle();" in source
    assert "WorkspaceVoiceRecordingComponent" in source


def test_slash_and_skill_overlays_load_only_when_composer_text_needs_them():
    source = _stage_source()
    registry = _registry_source()
    slash_path = "$lib/features/composer/components/SlashAutocomplete.svelte"
    skill_path = "$lib/features/composer/components/SkillMentionOverlay.svelte"

    assert f"import SlashAutocomplete from '{slash_path}';" not in source
    assert f"import SkillMentionOverlay from '{skill_path}';" not in source
    assert "'composer-text-tools': async () =>" in source
    assert f"import('{slash_path}')" in registry
    assert f"import('{skill_path}')" in registry
    assert "if (slashToken || hasSkillMention(inputValue))" in source
    assert "SlashAutocompleteComponent" in source
    assert "SkillMentionOverlayComponent" in source


def test_run_settings_menu_and_data_load_on_settings_interaction():
    stage = _stage_source()
    registry = _registry_source()
    adapter = COMPOSER_ADAPTER_PATH.read_text()
    menu = SETTINGS_MENU_PATH.read_text()

    assert "import('$lib/features/composer/domain/runSettings')" in registry
    assert "onSettingsOpen={() => ensureThreadModuleLoaded('run-settings')}" in stage
    assert "import('./WorkspaceComposerSettingsMenu.svelte')" in adapter
    assert "await onSettingsOpen?.();" in adapter
    assert "WorkspaceComposerSettingsMenuComponent" in adapter
    assert 'role="menu" class="composer-settings-menu"' in menu


def test_reference_previews_load_only_for_messages_that_contain_references():
    transcript = TRANSCRIPT_PATH.read_text()
    discussion = DISCUSSION_PATH.read_text()
    lazy_preview = LAZY_REFERENCE_PREVIEW_PATH.read_text()
    registry = _registry_source()
    module_path = "$lib/features/threads/components/ObjectReferencePreviewList.svelte"

    assert f"import ObjectReferencePreviewList from '{module_path}';" not in transcript
    assert f"import ObjectReferencePreviewList from '{module_path}';" not in discussion
    assert "import LazyObjectReferencePreviewList from" in transcript
    assert "import LazyObjectReferencePreviewList from" in discussion
    assert "<LazyObjectReferencePreviewList" in transcript
    assert "<LazyObjectReferencePreviewList" in discussion
    assert f"import('{module_path}')" in registry
    assert "const hasReferences = $derived" in lazy_preview
    assert "if (hasReferences) void ensureLoaded();" in lazy_preview
    assert "import('$lib/features/threads/controllers/threadLazyModuleRegistry')" in lazy_preview
    assert "registry.loadObjectReferencePreviewList()" in lazy_preview
    assert "ObjectReferencePreviewListComponent" in lazy_preview
    assert "Loading reference previews" in lazy_preview
    assert "Reference previews could not load." in lazy_preview
    assert "function ensureLoaded()" in lazy_preview


def test_visual_block_renderer_loads_only_when_visual_content_is_present():
    transcript = TRANSCRIPT_PATH.read_text()
    lazy_visual = LAZY_VISUAL_BLOCK_PATH.read_text()
    registry = _registry_source()
    module_path = "$lib/features/threads/components/StreamVisualBlock.svelte"

    assert f"import StreamVisualBlock from '{module_path}';" not in transcript
    assert "import LazyStreamVisualBlock from" in transcript
    assert transcript.count("<LazyStreamVisualBlock") == 2
    assert f"import('{module_path}')" in registry
    assert "registry.loadStreamVisualBlock()" in lazy_visual
    assert "StreamVisualBlockComponent" in lazy_visual
    assert "Loading visual content" in lazy_visual
    assert "Visual content could not load." in lazy_visual
    assert "function ensureLoaded()" in lazy_visual
