<script lang="ts">
  import { onMount, tick } from 'svelte';
  import {
    ConstellationComposerOrb,
    ConstellationIcon,
  } from '$lib/components/constellation';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import AiPromptComposer from '$lib/features/composer/components/AiPromptComposer.svelte';
  import MentionAutocomplete from '$lib/features/composer/components/MentionAutocomplete.svelte';
  import ProjectContextPicker from '$lib/features/composer/components/ProjectContextPicker.svelte';
  import WorkspaceComposerAdapter from './WorkspaceComposerAdapter.svelte';
  import WorkspaceVoiceRecording from './WorkspaceVoiceRecording.svelte';
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
  import { WorkspaceVoiceDictationController } from '$lib/features/composer/controllers/workspaceVoiceDictation.svelte.ts';
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
  import type { CortexCreateIdeaOptions } from '$lib/stores/cortex.svelte';
  import type { AgentRunOptions } from '$lib/types/cortex';

  type WorkspaceComposerAutoDraft = {
    id: string;
    text: string;
    origin?: string | null;
    originRef?: string | null;
    displayTitle?: string | null;
    runMetadata?: Record<string, any> | null;
    delayMs?: number;
    typeIntervalMs?: number;
    submitDelayMs?: number;
  };

  type AutoDraftCompletion = {
    id: string;
    status: 'submitted' | 'skipped' | 'cancelled';
    ideaId?: string;
  };

  type SubmitPromptOptions = {
    createOptions?: CortexCreateIdeaOptions;
    runOptions?: AgentRunOptions;
  };

  let {
    context = null,
    projectContextInitialOpen = false,
    projectContextInitialProfileId = 'none',
    projectContextLoadServerProfiles = true,
    autoDraft = null,
    onthreadintent,
    onAutoDraftComplete,
  }: {
    context?: CortexWorkspacePoint | null;
    projectContextInitialOpen?: boolean;
    projectContextInitialProfileId?: string;
    projectContextLoadServerProfiles?: boolean;
    autoDraft?: WorkspaceComposerAutoDraft | null;
    onthreadintent?: (origin: { x: number; y: number }) => void;
    onAutoDraftComplete?: (completion: AutoDraftCompletion) => void;
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
  let autoDraftPlaybackId: string | null = null;
  let autoDraftRunToken = 0;
  type CreationBehavior = 'open-thread' | 'keep-workspace';

  let viewportHeight = $state(800);
  const voiceDictation = new WorkspaceVoiceDictationController({
    getDraft: () => inputValue,
    setDraft: (next) => {
      inputValue = next;
      void syncComposerHeight();
    },
    submit: async () => {
      await syncComposerHeight();
      await submitPrompt();
    },
    onError: (message) => ui.toast(message, 'error'),
    onSettled: syncComposerHeight,
    focusDraft: () => requestAnimationFrame(() => textareaEl?.focus()),
  });

  const workspaceComposerMaxHeight = $derived(getWorkspaceComposerMaxHeight(viewportHeight));
  const isVoiceRecording = $derived(voiceDictation.isRecording);
  const isVoiceBusy = $derived(voiceDictation.isBusy);
  const voiceControlDisabled = $derived(sending || voiceDictation.controlDisabled);

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
    void voiceDictation.loadSettings();
    window.addEventListener('resize', updateViewportHeight);
    window.visualViewport?.addEventListener('resize', updateViewportHeight);

    return () => {
      voiceDictation.destroy();
      window.removeEventListener('resize', updateViewportHeight);
      window.visualViewport?.removeEventListener('resize', updateViewportHeight);
    };
  });

  function insertMention(name: string) {
    const cursor = textareaEl?.selectionStart ?? inputValue.length;
    const beforeCursor = inputValue.slice(0, cursor);
    const atMatch = beforeCursor.match(/(^|[\s([{])@([A-Za-z0-9._-]*)$/);
    const atPos = atMatch ? cursor - atMatch[2].length - 1 : inputValue.lastIndexOf('@');
    if (atPos < 0) return;
    inputValue = `${inputValue.slice(0, atPos)}@${name} ${inputValue.slice(cursor)}`;
    const nextCursor = atPos + name.length + 2;
    requestAnimationFrame(() => {
      textareaEl?.focus();
      textareaEl?.setSelectionRange(nextCursor, nextCursor);
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

  async function submitPrompt(
    behavior: CreationBehavior = 'open-thread',
    submitOptions: SubmitPromptOptions = {},
  ) {
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
      const runOptions = {
        ...cortex.runSettingsOptions(),
        ...(submitOptions.runOptions ?? {}),
      };
      const idea = worldOrigin
        ? await cortex.createIdeaAt(
            messageText,
            worldOrigin.x,
            worldOrigin.y,
            undefined,
            attachments,
            text,
            runOptions,
            submitOptions.createOptions,
          )
        : await cortex.createIdea(
            messageText,
            undefined,
            attachments,
            text,
            runOptions,
            submitOptions.createOptions,
          );
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
      return idea;
    } finally {
      sending = false;
      await syncComposerHeight();
    }
  }

  function sleep(ms: number) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  async function playAutoDraft(draft: WorkspaceComposerAutoDraft, token: number) {
    await tick();
    await sleep(draft.delayMs ?? 320);
    if (token !== autoDraftRunToken) return;
    if (inputValue.trim() || pendingAttachments.length > 0 || sending) {
      onAutoDraftComplete?.({ id: draft.id, status: 'skipped' });
      return;
    }

    textareaEl?.focus();
    inputValue = '';
    await syncComposerHeight();

    const intervalMs = draft.typeIntervalMs ?? 18;
    for (let index = 1; index <= draft.text.length; index += 1) {
      if (token !== autoDraftRunToken) return;
      inputValue = draft.text.slice(0, index);
      await syncComposerHeight();
      await sleep(intervalMs);
    }

    if (token !== autoDraftRunToken) return;
    await sleep(draft.submitDelayMs ?? 520);
    if (token !== autoDraftRunToken) return;

    const idea = await submitPrompt('open-thread', {
      createOptions: {
        origin: draft.origin,
        originRef: draft.originRef,
        displayTitle: draft.displayTitle,
      },
      runOptions: draft.runMetadata ? { metadata: draft.runMetadata } : undefined,
    });

    onAutoDraftComplete?.({
      id: draft.id,
      status: idea?.id ? 'submitted' : 'cancelled',
      ideaId: idea?.id,
    });
  }

  $effect(() => {
    const draft = autoDraft;
    if (!draft || autoDraftPlaybackId === draft.id) return;
    autoDraftPlaybackId = draft.id;
    const token = autoDraftRunToken + 1;
    autoDraftRunToken = token;
    void playAutoDraft(draft, token);

    return () => {
      if (autoDraftRunToken === token) autoDraftRunToken += 1;
    };
  });

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
  tone="spectral"
  kicker=""
  placeholder="Ask Illo anything..."
  actionState={sending ? 'working' : 'idle'}
  canSubmit={(isVoiceRecording || inputValue.trim().length > 0 || pendingAttachments.length > 0) && projectContextValid}
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
  onSubmit={() => (isVoiceRecording ? void voiceDictation.send() : void submitPrompt())}
  {onthreadintent}
>
  {#snippet extraLeadingControls()}
    <ProjectContextPicker
      mode="inline"
      disabled={sending || isVoiceBusy}
      initialOpen={projectContextInitialOpen}
      initialProfileId={projectContextInitialProfileId}
      loadServerProfiles={projectContextLoadServerProfiles}
      onStateChange={handleProjectContextState}
    />
    <ConstellationComposerOrb
      label={voiceDictation.controlLabel}
      title={voiceDictation.controlTitle}
      disabled={voiceControlDisabled}
      variant="bare"
      onclick={() => {
        if (!sending) voiceDictation.toggle();
      }}
    >
      <ConstellationIcon name={isVoiceRecording ? 'stop' : 'mic'} size={18} stroke={2} />
    </ConstellationComposerOrb>
  {/snippet}

  {#snippet editor()}
    <div class="workspace-input-wrap">
      {#if isVoiceRecording}
        <WorkspaceVoiceRecording elapsedMs={voiceDictation.elapsedMs} />
      {:else}
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
      {/if}
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
