<script lang="ts">
  import {
    ConstellationPill,
  } from '$lib/components/constellation';
  import WorkspaceComposerAdapter from '$lib/features/composer/components/WorkspaceComposerAdapter.svelte';

  import type { ChatComposerModel } from './chatTypes';

  let {
    tone = 'spectral',
    value,
    defaultValue = '',
    placeholder = 'Write a message...',
    hint = '',
    modeLabel = '',
    replyContextLabel = '',
    primaryActionLabel = 'Send',
    stopLabel = 'Stop',
    attachLabel = 'Attach',
    disabled = false,
    loading = false,
    canSubmit,
    attachments = [],
    typing = null,
    variant = 'room',
    onValueChange,
    onSubmit,
    onAttach,
    onStop,
    onPaste,
    onKeydown,
    onRemoveAttachment,
    className = '',
  }: ChatComposerModel & { className?: string } = $props();

  let localValue = $state('');
  let textareaEl: HTMLTextAreaElement | undefined = $state();

  const composerValue = $derived(value ?? localValue);
  const resolvedCanSubmit = $derived(
    (canSubmit ?? (composerValue.trim().length > 0 || attachments.length > 0)) && !disabled,
  );
  const actionState = $derived(loading ? 'working' : 'idle');
  const rootClass = $derived(
    ['chat-composer-shell', variant === 'thread' ? 'is-thread' : 'is-room', className]
      .filter(Boolean)
      .join(' '),
  );

  const typingLabel = $derived.by(() => {
    if (!typing?.participants?.length) return '';
    if (typing.label) return typing.label;
    if (typing.participants.length === 1) return `${typing.participants[0].label} is typing`;
    return `${typing.participants.length} people are typing`;
  });

  function resizeTextarea(target = textareaEl) {
    if (!target) return;
    const maxHeightValue = Number.parseFloat(
      getComputedStyle(target).getPropertyValue('--chat-composer-textarea-max-height'),
    );
    const maxHeight = Number.isFinite(maxHeightValue) && maxHeightValue > 0 ? maxHeightValue : 144;
    target.style.height = 'auto';
    target.style.height = `${Math.min(target.scrollHeight, maxHeight)}px`;
  }

  function updateValue(nextValue: string) {
    if (value === undefined) {
      localValue = nextValue;
    }

    onValueChange?.(nextValue);
  }

  function submitComposer() {
    if (loading && onStop) {
      onStop();
      return;
    }

    if (!resolvedCanSubmit) return;
    onSubmit?.(composerValue);
  }

  function handleInput(event: Event) {
    const nextValue = (event.currentTarget as HTMLTextAreaElement).value;
    updateValue(nextValue);
    resizeTextarea(event.currentTarget as HTMLTextAreaElement);
  }

  function handleTextareaPaste(event: ClipboardEvent) {
    onPaste?.(event);
  }

  function handleTextareaKeydown(event: KeyboardEvent) {
    onKeydown?.(event);
    if (event.defaultPrevented) return;

    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submitComposer();
      return;
    }

    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
      event.preventDefault();
      submitComposer();
    }
  }

  $effect(() => {
    const nextDefaultValue = defaultValue;
    if (value === undefined && nextDefaultValue !== undefined && localValue === '') {
      localValue = nextDefaultValue;
    }
  });

  $effect(() => {
    composerValue;
    resizeTextarea();
  });
</script>

<div class={rootClass}>
  <WorkspaceComposerAdapter
    mode="thread"
    {tone}
    {placeholder}
    value={composerValue}
    actionState={actionState}
    canSubmit={resolvedCanSubmit}
    attachLabel={attachLabel}
    sendLabel={primaryActionLabel}
    stopLabel={stopLabel}
    {attachments}
    {disabled}
    onStop={onStop}
    onAttach={onAttach}
    onRemoveAttachment={onRemoveAttachment}
    onSubmit={submitComposer}
    className="chat-composer-adapter"
  >
    {#snippet editor()}
      <div class="chat-composer-editor">
        {#if modeLabel || replyContextLabel}
          <div class="chat-composer-context">
            {#if modeLabel}
              <ConstellationPill variant="muted">{modeLabel}</ConstellationPill>
            {/if}

            {#if replyContextLabel}
              <ConstellationPill variant="status" leadingDot>{replyContextLabel}</ConstellationPill>
            {/if}
          </div>
        {/if}

        <textarea
          bind:this={textareaEl}
          class="chat-composer-textarea"
          rows="1"
          spellcheck="false"
          {placeholder}
          {disabled}
          value={composerValue}
          oninput={handleInput}
          onkeydown={handleTextareaKeydown}
          onpaste={handleTextareaPaste}
        ></textarea>
      </div>
    {/snippet}

    {#snippet supporting()}
      {#if typingLabel || hint}
        <div class="chat-composer-supporting">
          {#if hint}
            <span>{hint}</span>
          {/if}
          {#if typingLabel}
            <span>{typingLabel}</span>
          {/if}
        </div>
      {/if}
    {/snippet}
  </WorkspaceComposerAdapter>
</div>

<style>
  .chat-composer-shell {
    position: relative;
    left: auto;
    bottom: auto;
    width: 100%;
    max-width: 100%;
    transform: none;
    z-index: auto;
  }

  .chat-composer-shell :global(.chat-composer-adapter) {
    position: relative;
    left: auto;
    bottom: auto;
    width: 100%;
    max-width: 100%;
    transform: none;
    z-index: auto;
  }

  .chat-composer-shell :global(.chat-composer-adapter .cortex-workspace-composer) {
    gap: 10px;
    min-height: 0;
    padding: 14px 16px 12px;
    border-radius: 20px;
    background: var(--constellation-chat-composer-background);
    box-shadow: var(--constellation-chat-composer-shadow);
  }

  .chat-composer-shell.is-room :global(.chat-composer-adapter.thread-mode .cortex-workspace-composer) {
    min-height: 86px;
  }

  .chat-composer-shell.is-thread :global(.chat-composer-adapter .cortex-workspace-composer) {
    min-height: 96px;
    padding: 12px 14px 10px;
    border-radius: 18px;
    background: var(--constellation-chat-composer-thread-background);
    box-shadow: var(--constellation-chat-composer-thread-shadow);
  }

  .chat-composer-shell :global(.chat-composer-adapter .composer-editor) {
    min-height: 0;
  }

  .chat-composer-shell :global(.chat-composer-adapter .cortex-workspace-composer) {
    min-height: 0;
  }

  .chat-composer-shell.is-room :global(.chat-composer-adapter.thread-mode .composer-editor) {
    min-height: 28px;
  }

  .chat-composer-shell.is-thread :global(.chat-composer-adapter.thread-mode .composer-editor) {
    min-height: 42px;
  }

  .chat-composer-shell :global(.chat-composer-adapter .composer-footer) {
    gap: 8px;
  }

  .chat-composer-shell :global(.chat-composer-adapter .composer-footer-start) {
    flex: 0 0 auto;
  }

  .chat-composer-shell :global(.chat-composer-adapter .composer-footer-end) {
    margin-left: auto;
  }

  .chat-composer-shell :global(.chat-composer-adapter .composer-supporting) {
    min-height: 0;
  }

  .chat-composer-shell :global(.chat-composer-adapter .composer-attachments) {
    gap: 8px;
  }

  .chat-composer-shell :global(.chat-composer-adapter .composer-attachment) {
    max-width: min(100%, 240px);
  }

  .chat-composer-editor {
    display: grid;
    gap: 8px;
  }

  .chat-composer-context {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .chat-composer-textarea {
    display: block;
    width: 100%;
    min-height: 24px;
    max-height: 144px;
    padding: 2px 4px 0;
    border: 0;
    background: transparent;
    color: var(--constellation-composer-textarea);
    font: inherit;
    font-size: 14px;
    line-height: 1.5;
    resize: none;
    outline: none;
    box-sizing: border-box;
  }

  .chat-composer-shell.is-room .chat-composer-textarea {
    min-height: 28px;
  }

  .chat-composer-shell.is-thread .chat-composer-textarea {
    min-height: 44px;
  }

  .chat-composer-textarea::placeholder {
    color: var(--constellation-composer-placeholder);
  }

  .chat-composer-supporting {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 8px 16px;
    color: var(--constellation-chat-composer-supporting);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.12em;
    line-height: 1.3;
    text-transform: uppercase;
  }

  @media (max-width: 720px) {
    .chat-composer-shell :global(.chat-composer-adapter .cortex-workspace-composer) {
      padding: 12px 14px 10px;
      border-radius: 18px;
    }
  }
</style>
