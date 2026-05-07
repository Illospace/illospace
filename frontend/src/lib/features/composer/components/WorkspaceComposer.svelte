<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import AiPromptComposer from '$lib/features/composer/components/AiPromptComposer.svelte';
  import MentionAutocomplete from '$lib/features/composer/components/MentionAutocomplete.svelte';
  import ProjectContextPicker from '$lib/features/composer/components/ProjectContextPicker.svelte';
  import WorkspaceComposerAdapter from './WorkspaceComposerAdapter.svelte';
  import { applyRunSetting, buildRunSettingsGroups } from '$lib/features/composer/domain/runSettings';
  import {
    attachIdeaProjectContext,
    uploadFile,
  } from '$lib/features/cortex/api/cortexApi';
  import { ATTACHMENT_INPUT_ACCEPT } from '$lib/utils/attachmentPreview';
  import {
    getWorkspaceComposerDropFiles,
    getWorkspaceComposerInputFiles,
    getWorkspaceComposerPasteFiles,
    getWorkspaceComposerUploadFailureMessage,
    preventWorkspaceComposerDefaultDrag,
    resetWorkspaceComposerFileInput,
    uploadWorkspaceComposerFiles,
  } from '$lib/features/composer/controllers/attachmentController';
  import {
    buildWorkspaceComposerAttachmentsWithProjectContext,
    getWorkspaceComposerProjectContextSaveErrorMessage,
    saveWorkspaceComposerProjectContext,
  } from '$lib/features/composer/controllers/projectContextComposerController';
  import {
    getWorkspaceComposerScreenOrigin,
    getWorkspaceComposerWorldOrigin,
  } from '$lib/features/composer/domain/workspaceComposerOrigin';
  import {
    WORKSPACE_COMPOSER_MIN_HEIGHT,
    applyWorkspaceComposerTextareaHeight,
    getWorkspaceComposerMaxHeight,
    getWorkspaceComposerViewportHeight,
  } from '$lib/features/composer/domain/workspaceComposerViewport';
  import {
    type ProjectContextSnapshotLike,
    type ProjectContextPickerState,
  } from '$lib/utils/projectContext';
  import type { SlashCommandToken } from '$lib/utils/slashCommand';
  import type { CortexWorkspacePoint } from '$lib/features/workspace-scene/domain/workspacePoint';

  let {
    context = null,
    projectContextInitialOpen = false,
    projectContextInitialProfileId = 'none',
    projectContextLoadServerProfiles = true,
    onthreadintent,
  }: {
    context?: CortexWorkspacePoint | null;
    projectContextInitialOpen?: boolean;
    projectContextInitialProfileId?: string;
    projectContextLoadServerProfiles?: boolean;
    onthreadintent?: (origin: { x: number; y: number }) => void;
  } = $props();

  let inputValue = $state('');
  let textareaEl: HTMLTextAreaElement | undefined = $state();
  let sending = $state(false);
  let activeSlashToken: SlashCommandToken | null = $state(null);
  let mentionRef: MentionAutocomplete | undefined = $state();
  let fileInputEl: HTMLInputElement | undefined = $state();
  let pendingAttachments = $state<any[]>([]);
  let dragOver = $state(false);
  let projectContextSnapshot = $state<ProjectContextSnapshotLike | null>(null);
  let projectContextValid = $state(true);
  let projectContextError = $state<string | null>(null);
  type CreationBehavior = 'open-thread' | 'keep-workspace';

  let viewportHeight = $state(800);

  const workspaceComposerMaxHeight = $derived(getWorkspaceComposerMaxHeight(viewportHeight));

  function updateViewportHeight() {
    viewportHeight = getWorkspaceComposerViewportHeight(window);
  }

  function autoGrowTextarea() {
    applyWorkspaceComposerTextareaHeight(textareaEl, workspaceComposerMaxHeight);
  }

  async function syncComposerHeight() {
    await tick();
    autoGrowTextarea();
  }

  $effect(() => {
    if (!textareaEl) return;
    textareaEl.setAttribute('autocorrect', 'off');
    textareaEl.setAttribute('autocapitalize', 'off');
  });

  $effect(() => {
    workspaceComposerMaxHeight;
    void syncComposerHeight();
  });

  onMount(() => {
    updateViewportHeight();
    window.addEventListener('resize', updateViewportHeight);
    window.visualViewport?.addEventListener('resize', updateViewportHeight);

    return () => {
      window.removeEventListener('resize', updateViewportHeight);
      window.visualViewport?.removeEventListener('resize', updateViewportHeight);
    };
  });

  function insertMention(name: string) {
    const atPos = inputValue.lastIndexOf('@');
    if (atPos < 0) return;
    inputValue = inputValue.slice(0, atPos) + '@' + name + ' ';
    requestAnimationFrame(() => {
      textareaEl?.focus();
      const pos = inputValue.length;
      textareaEl?.setSelectionRange(pos, pos);
      autoGrowTextarea();
    });
  }

  async function uploadFiles(files: File[]) {
    const { uploaded, failures } = await uploadWorkspaceComposerFiles(files, uploadFile);
    if (uploaded.length) {
      pendingAttachments = [...pendingAttachments, ...uploaded];
    }
    for (const failure of failures) {
      ui.toast(getWorkspaceComposerUploadFailureMessage(failure), 'error');
    }
  }

  async function handlePaste(event: ClipboardEvent) {
    const files = getWorkspaceComposerPasteFiles(event);
    if (files.length === 0) return;
    event.preventDefault();
    await uploadFiles(files);
  }

  async function handleFileSelect(event: Event) {
    const files = getWorkspaceComposerInputFiles(event);
    if (!files.length) return;
    await uploadFiles(files);
    resetWorkspaceComposerFileInput(event);
  }

  async function handleDrop(event: DragEvent) {
    preventWorkspaceComposerDefaultDrag(event);
    dragOver = false;
    const files = getWorkspaceComposerDropFiles(event);
    if (!files.length) return;
    await uploadFiles(files);
  }

  function handleDragOver(event: DragEvent) {
    preventWorkspaceComposerDefaultDrag(event);
    dragOver = true;
  }

  function handleDragLeave(event: DragEvent) {
    preventWorkspaceComposerDefaultDrag(event);
    dragOver = false;
  }

  function removeAttachment(idx: number) {
    pendingAttachments = pendingAttachments.filter((_, i) => i !== idx);
  }

  function composerOrigin() {
    return getWorkspaceComposerScreenOrigin(context, window);
  }

  function composerWorldOrigin() {
    return getWorkspaceComposerWorldOrigin(context);
  }

  /**
   * Workspace product contract:
   * any non-empty submission creates a thought. The composer intent decides
   * whether creation stays in orbit or immediately enters the thread stage.
   */
  async function openCreatedThread(ideaId: string) {
    await tick();
    await cortex.selectIdea(ideaId);

    if (cortex.selectedIdeaId === ideaId && cortex.panelOpen) return;

    await new Promise((resolve) => window.setTimeout(resolve, 120));
    await cortex.selectIdea(ideaId);
  }

  async function submitPrompt(behavior: CreationBehavior = 'open-thread') {
    const text = inputValue.trim();
    const baseAttachments = [...pendingAttachments];
    if ((!text && baseAttachments.length === 0) || sending) return;
    if (!projectContextValid) {
      ui.toast(projectContextError || 'Fix the project context before starting this thought.', 'error');
      return;
    }

    sending = true;
    try {
      const worldOrigin = composerWorldOrigin();
      const messageText = text || '(attachment)';
      const selectedProjectContext = projectContextSnapshot;
      const attachments = buildWorkspaceComposerAttachmentsWithProjectContext(baseAttachments, selectedProjectContext);
      const idea = worldOrigin
        ? await cortex.createIdeaAt(
            messageText,
            worldOrigin.x,
            worldOrigin.y,
            undefined,
            attachments,
            text,
            cortex.runSettingsOptions(),
          )
        : await cortex.createIdea(messageText, undefined, attachments, text, cortex.runSettingsOptions());
      if (!idea?.id) return;
      await saveWorkspaceComposerProjectContext(
        idea.id,
        selectedProjectContext,
        attachIdeaProjectContext,
        (err) => {
          ui.toast(getWorkspaceComposerProjectContextSaveErrorMessage(err), 'error');
        },
      );

      inputValue = '';
      pendingAttachments = [];
      await syncComposerHeight();

      if (behavior === 'open-thread') {
        onthreadintent?.(composerOrigin());
        await openCreatedThread(idea.id);
      }
    } finally {
      sending = false;
      await syncComposerHeight();
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (mentionRef?.handleKey(event)) return;
    if (event.key === 'Escape') {
      if (inputValue || pendingAttachments.length > 0) {
        event.preventDefault();
        inputValue = '';
        pendingAttachments = [];
        void syncComposerHeight();
      }
      return;
    }
  }

  function handleComposerSubmit(event: KeyboardEvent) {
    const requestedBehavior = event.metaKey || event.ctrlKey ? 'keep-workspace' : undefined;
    void submitPrompt(requestedBehavior);
  }

  function handleInput(_event: Event, token: SlashCommandToken | null = activeSlashToken) {
    mentionRef?.check(token ? '' : inputValue);
  }

  function handleCursorChange(_event: Event, token: SlashCommandToken | null = activeSlashToken) {
    mentionRef?.check(token ? '' : inputValue);
  }

  function handleProjectContextState(state: ProjectContextPickerState) {
    projectContextSnapshot = state.snapshot;
    projectContextValid = state.valid;
    projectContextError = state.error;
  }

</script>

<input
  type="file"
  accept={ATTACHMENT_INPUT_ACCEPT}
  multiple
  style="display:none"
  bind:this={fileInputEl}
  onchange={handleFileSelect}
/>

<WorkspaceComposerAdapter
  mode="workspace"
  tone="amber"
  kicker=""
  placeholder="Ask Illo anything..."
  actionState={sending ? 'working' : 'idle'}
  canSubmit={(inputValue.trim().length > 0 || pendingAttachments.length > 0) && projectContextValid}
  settingsGroups={buildRunSettingsGroups({
    mode: cortex.executionProfile,
    intelligence: cortex.intelligenceTier,
    effort: cortex.effortLevel,
  })}
  onSettingsChange={(key, nextValue) =>
    applyRunSetting(key, nextValue, {
      setExecutionProfile: (value) => cortex.setExecutionProfile(value),
      setIntelligenceTier: (value) => cortex.setIntelligenceTier(value),
      setEffortLevel: (value) => cortex.setEffortLevel(value),
    })}
  settingsAriaLabel="Mode, Intelligence, and Effort"
  attachments={pendingAttachments}
  context={context}
  disabled={sending}
  isDragOver={dragOver}
  onAttach={() => fileInputEl?.click()}
  onRemoveAttachment={removeAttachment}
  onDrop={handleDrop}
  onDragOver={handleDragOver}
  onDragLeave={handleDragLeave}
  onSubmit={() => void submitPrompt()}
  {onthreadintent}
>
  {#snippet extraLeadingControls()}
    <ProjectContextPicker
      mode="inline"
      disabled={sending}
      initialOpen={projectContextInitialOpen}
      initialProfileId={projectContextInitialProfileId}
      loadServerProfiles={projectContextLoadServerProfiles}
      onStateChange={handleProjectContextState}
    />
  {/snippet}

  {#snippet editor()}
    <div class="workspace-input-wrap">
      <MentionAutocomplete bind:this={mentionRef} bind:textarea={textareaEl} onselect={insertMention} />
      <AiPromptComposer
        bind:value={inputValue}
        bind:textarea={textareaEl}
        className="workspace-ai-prompt"
        rows={1}
        placeholder="Ask Illo anything..."
        ariaLabel="Workspace prompt"
        minHeight={WORKSPACE_COMPOSER_MIN_HEIGHT}
        maxHeight={workspaceComposerMaxHeight}
        submitOnEnter
        disabled={sending}
        onKeydown={handleKeydown}
        onInput={handleInput}
        onCursorChange={handleCursorChange}
        onSubmit={handleComposerSubmit}
        onPaste={handlePaste}
        onSlashTokenChange={(token) => (activeSlashToken = token)}
      />
    </div>
  {/snippet}
</WorkspaceComposerAdapter>

<style>
  .workspace-input-wrap {
    position: relative;
    min-height: 40px;
  }

  :global(.workspace-ai-prompt) {
    --skill-mention-padding: 2px 3px 0;
    --skill-mention-font-size: 15px;
    --skill-mention-line-height: 1.45;
    --ai-prompt-padding: 2px 3px 0;
    --ai-prompt-font-size: 15px;
    --ai-prompt-line-height: 1.45;
    --ai-prompt-text: var(--constellation-composer-textarea);
    --ai-prompt-placeholder: var(--constellation-composer-placeholder);
  }

  :global(.workspace-ai-prompt .ai-prompt-textarea) {
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  @media (max-width: 900px) {
    .workspace-input-wrap {
      min-height: 38px;
    }
  }
</style>
