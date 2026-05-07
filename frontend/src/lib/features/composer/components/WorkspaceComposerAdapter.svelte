<script lang="ts">
  import {
    ConstellationComposerActionOrb,
    ConstellationIcon,
    ConstellationComposerOrb,
    ConstellationSelectChip,
  } from '$lib/components/constellation';
  import {
    getWorkspaceComposerActionLabel,
    getWorkspaceComposerOrigin,
    type WorkspaceComposerAdapterProps,
  } from '../domain/composerAdapter';

  let {
    mode = 'workspace',
    tone = 'amber',
    actionStyle,
    kicker = '',
    placeholder,
    hint,
    value,
    defaultValue,
    onValueChange,
    actionState = 'idle',
    canSubmit,
    allowSubmitWhileWorking = false,
    onSubmit,
    onStop,
    attachLabel = 'Attach file',
    sendLabel = 'Send',
    stopLabel = 'Stop generation',
    intentOptions,
    intentValue,
    onIntentChange,
    intentAriaLabel = 'Mode',
    secondaryIntentOptions,
    secondaryIntentValue,
    onSecondaryIntentChange,
    secondaryIntentAriaLabel = 'Mode',
    settingsGroups,
    onSettingsChange,
    settingsAriaLabel = 'Mode, Intelligence, and Effort',
    attachments = [],
    onAttach,
    onRemoveAttachment,
    onPaste,
    onDrop,
    onDragOver,
    onDragLeave,
    onKeydown,
    onthreadintent,
    context = null,
    disabled = false,
    isDragOver,
    className,
    editor,
    attachmentsSlot,
    leadingControls,
    extraLeadingControls,
    trailingControls,
    supporting,
  }: WorkspaceComposerAdapterProps = $props();

  let draftValue = $state('');
  let internalDragOver = $state(false);
  let textareaEl: HTMLTextAreaElement | undefined = $state();
  let settingsRootEl: HTMLDivElement | undefined = $state();
  let selectedMode = $state<string>('');
  let settingsOpen = $state(false);
  let activeSettingsGroupKey = $state<string | null>(null);
  let settingsCloseTimer: ReturnType<typeof setTimeout> | null = null;

  const isWorking = $derived(actionState === 'working');
  const composerValue = $derived(value ?? draftValue);
  const resolvedCanSubmit = $derived(canSubmit ?? composerValue.trim().length > 0);
  const shouldSubmitWhileWorking = $derived(Boolean(isWorking && allowSubmitWhileWorking && resolvedCanSubmit));
  const effectiveActionState = $derived(shouldSubmitWhileWorking ? 'idle' : actionState);
  const actionLabel = $derived(getWorkspaceComposerActionLabel(effectiveActionState, sendLabel, stopLabel));
  const resolvedDragOver = $derived(isDragOver ?? internalDragOver);
  const threadOrigin = $derived(getWorkspaceComposerOrigin(context));
  const resolvedDefaultValue = $derived(defaultValue);
  const controlVariant = 'bare';
  const showIntentPicker = $derived(Boolean(intentOptions?.length));
  const selectModeOptions = $derived(intentOptions ?? []);
  const showSecondaryIntentPicker = $derived(Boolean(secondaryIntentOptions?.length));
  const showSettingsPicker = $derived(Boolean(settingsGroups?.length));
  const selectedIntentValue = $derived(intentValue ?? selectedMode);
  const selectedSecondaryIntentValue = $derived(secondaryIntentValue ?? secondaryIntentOptions?.[0]?.value ?? '');
  const effectiveIntentValue = $derived(
    selectModeOptions.some((option) => option.value === selectedIntentValue)
      ? selectedIntentValue
      : (selectModeOptions[0]?.value ?? ''),
  );
  const effectiveSecondaryIntentValue = $derived(
    secondaryIntentOptions?.some((option) => option.value === selectedSecondaryIntentValue)
      ? selectedSecondaryIntentValue
      : (secondaryIntentOptions?.[0]?.value ?? ''),
  );
  const settingsSummary = $derived(
    (settingsGroups ?? [])
      .map((group) => selectedSettingsOption(group)?.label ?? group.value ?? '')
      .filter(Boolean)
      .join(' · '),
  );
  const shellClass = $derived(
    [
      'cortex-workspace-composer-shell',
      resolvedDragOver ? 'drag-over' : '',
      mode === 'thread' ? 'thread-mode' : '',
      className ?? '',
    ]
      .filter(Boolean)
      .join(' '),
  );

  $effect(() => {
    const nextDefaultValue = resolvedDefaultValue;
    if (nextDefaultValue !== undefined && draftValue === '' && value === undefined) {
      draftValue = nextDefaultValue;
    }
  });

  $effect(() => {
    if (intentValue !== undefined) return;
    if (selectModeOptions.length === 0) return;

    const nextModeValue = selectModeOptions[0]?.value ?? '';
    if (!selectModeOptions.some((option) => option.value === selectedMode)) {
      selectedMode = nextModeValue;
    }
  });

  $effect(() => {
    if (!settingsGroups?.length) {
      activeSettingsGroupKey = null;
      return;
    }

    const hasActiveGroup = settingsGroups.some((group) => group.key === activeSettingsGroupKey);
    if (!activeSettingsGroupKey || !hasActiveGroup) activeSettingsGroupKey = settingsGroups[0]?.key ?? null;
  });

  $effect(() => {
    if (!settingsOpen) return;

    function handlePointerDown(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (!settingsRootEl?.contains(target)) {
        closeSettingsMenu();
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') closeSettingsMenu();
    }

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  });

  function growTextarea(target: HTMLTextAreaElement) {
    target.style.height = 'auto';
    target.style.height = `${Math.min(target.scrollHeight, targetMaxHeight(target))}px`;
  }

  function targetMaxHeight(target: HTMLTextAreaElement) {
    const rawMaxHeight = getComputedStyle(target).maxHeight;
    const parsedMaxHeight = Number.parseFloat(rawMaxHeight);
    return Number.isFinite(parsedMaxHeight) ? parsedMaxHeight : 140;
  }

  $effect(() => {
    composerValue;
    if (textareaEl) {
      growTextarea(textareaEl);
    }
  });

  function handleValueChange(nextValue: string) {
    if (value === undefined) {
      draftValue = nextValue;
    }
    onValueChange?.(nextValue);
  }

  function handleTextareaInput(event: Event) {
    const target = event.currentTarget as HTMLTextAreaElement | null;
    if (!target) return;
    handleValueChange(target.value);
    growTextarea(target);
  }

  function handleTextareaKeydown(event: KeyboardEvent) {
    onKeydown?.(event);
    if (event.defaultPrevented) return;

    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (shouldSubmitWhileWorking) {
        onSubmit?.(composerValue);
        return;
      }
      if (isWorking) {
        onStop?.();
        return;
      }
      if (!resolvedCanSubmit) return;
      onSubmit?.(composerValue);
    }
  }

  function handleSubmitAction() {
    if (shouldSubmitWhileWorking) {
      onSubmit?.(composerValue);
      return;
    }
    if (isWorking) {
      onStop?.();
      return;
    }

    if (!resolvedCanSubmit) return;
    onSubmit?.(composerValue);
  }

  function handleDrop(event: DragEvent) {
    onDrop?.(event);
    if (!event.defaultPrevented) {
      internalDragOver = false;
    }
  }

  function handleDragOver(event: DragEvent) {
    onDragOver?.(event);
    if (!event.defaultPrevented) {
      internalDragOver = true;
    }
  }

  function handleDragLeave(event: DragEvent) {
    onDragLeave?.(event);
    if (!event.defaultPrevented) {
      internalDragOver = false;
    }
  }

  function handleAttach() {
    onAttach?.();
  }

  function handleRemoveAttachment(index: number) {
    onRemoveAttachment?.(index);
  }

  function handleIntentChange(nextValue: string) {
    if (intentValue === undefined) {
      selectedMode = nextValue;
    }

    onIntentChange?.(nextValue);
  }

  function handleSecondaryIntentChange(nextValue: string) {
    onSecondaryIntentChange?.(nextValue);
  }

  function selectedSettingsOption(group: { options: readonly { value: string; label: string }[]; value?: string }) {
    return group.options.find((option) => option.value === group.value) ?? group.options[0] ?? null;
  }

  function selectedSettingsLabel(group: { label: string; options: readonly { value: string; label: string }[]; value?: string }) {
    const option = selectedSettingsOption(group);
    return option ? `${group.label}: ${option.label}` : group.label;
  }

  function cancelSettingsClose() {
    if (!settingsCloseTimer) return;
    clearTimeout(settingsCloseTimer);
    settingsCloseTimer = null;
  }

  function openSettingsMenu(groupKey?: string) {
    if (disabled || !showSettingsPicker) return;
    cancelSettingsClose();
    activeSettingsGroupKey = groupKey ?? activeSettingsGroupKey ?? settingsGroups?.[0]?.key ?? null;
    settingsOpen = true;
  }

  function closeSettingsMenu() {
    cancelSettingsClose();
    settingsOpen = false;
  }

  function queueSettingsMenuClose() {
    cancelSettingsClose();
    settingsCloseTimer = setTimeout(() => {
      settingsOpen = false;
      settingsCloseTimer = null;
    }, 120);
  }

  function toggleSettingsOpen() {
    if (disabled || !showSettingsPicker) return;
    if (settingsOpen) {
      closeSettingsMenu();
      return;
    }
    openSettingsMenu();
  }

  function setActiveSettingsGroup(key: string) {
    activeSettingsGroupKey = key;
  }

  function handleSettingsChange(key: string, nextValue: string) {
    onSettingsChange?.(key, nextValue);
    closeSettingsMenu();
  }

  function attachmentLabel(attachment: any) {
    return attachment?.label ?? attachment?.filename ?? attachment?.name ?? 'file';
  }

  function attachmentUrl(attachment: any) {
    return typeof attachment?.url === 'string' ? attachment.url : '';
  }

  function attachmentType(attachment: any) {
    const type = attachment?.content_type ?? attachment?.type ?? attachment?.contentType ?? attachment?.mime_type;
    return typeof type === 'string' ? type : '';
  }

  function isImageAttachment(attachment: any) {
    const type = attachmentType(attachment);
    if (type.startsWith('image/')) return true;
    return /\.(avif|gif|jpe?g|png|svg|webp)$/i.test(attachmentLabel(attachment));
  }

  function shouldFocusTextareaFromSurfaceClick(root: HTMLElement, target: EventTarget | null) {
    if (!(target instanceof Element)) return target === root;
    if (!root.contains(target)) return false;
    if (target.closest('textarea, input, button, select, a[href], [role="button"], [role="menu"], [role="menuitem"], [role="listbox"], [role="option"], [contenteditable="true"]')) {
      return false;
    }

    return (
      target === root
      || target.classList.contains('composer-editor')
      || target.classList.contains('ai-prompt-composer')
      || target.classList.contains('workspace-input-wrap')
    );
  }

  function focusTextareaFromSurfaceClick(root: HTMLElement, event: MouseEvent) {
    if (!shouldFocusTextareaFromSurfaceClick(root, event.target)) return;
    root.querySelector<HTMLTextAreaElement>('textarea:not(:disabled)')?.focus();
  }

  function composerSurfaceFocus(root: HTMLElement) {
    const handleClick = (event: MouseEvent) => focusTextareaFromSurfaceClick(root, event);
    root.addEventListener('click', handleClick);

    return {
      destroy() {
        root.removeEventListener('click', handleClick);
      },
    };
  }
</script>

<div
  class={shellClass}
  data-design-component="ConstellationComposer"
  data-cortex-surface="workspace-composer"
  data-composer-mode={mode}
  data-composer-tone={tone}
  data-thread-origin-x={threadOrigin?.x}
  data-thread-origin-y={threadOrigin?.y}
  data-thread-intent={onthreadintent ? 'enabled' : 'disabled'}
  role="presentation"
  ondrop={handleDrop}
  ondragover={handleDragOver}
  ondragleave={handleDragLeave}
>
  <div class="cortex-workspace-composer" data-tone={tone} use:composerSurfaceFocus>
    {#if kicker}
      <div class="composer-kicker">{kicker}</div>
    {/if}

    <div class="composer-editor">
      {#if editor}
        {@render editor()}
      {:else}
        <textarea
          class="composer-textarea"
          bind:this={textareaEl}
          placeholder={placeholder}
          value={composerValue}
          rows="1"
          spellcheck="false"
          disabled={disabled}
          oninput={handleTextareaInput}
          onkeydown={handleTextareaKeydown}
          onpaste={onPaste}
        ></textarea>
      {/if}
    </div>

    {#if attachmentsSlot}
      <div class="composer-attachments-slot">
        {@render attachmentsSlot()}
      </div>
    {:else if attachments.length > 0}
      <div class="composer-attachments" aria-label="Pending attachments">
        {#each attachments as attachment, index (attachment.id ?? attachment.url ?? index)}
          <span class="composer-attachment">
            {#if isImageAttachment(attachment) && attachmentUrl(attachment)}
              <img src={attachmentUrl(attachment)} alt={attachment.alt ?? attachmentLabel(attachment)} />
            {:else}
              <ConstellationIcon name="document" size={14} stroke={1.8} />
            {/if}

            <span class="composer-attachment-label">{attachmentLabel(attachment)}</span>

            {#if onRemoveAttachment}
              <button
                type="button"
                class="composer-attachment-remove"
                aria-label={`Remove ${attachmentLabel(attachment)}`}
                onclick={() => handleRemoveAttachment(index)}
              >
                &times;
              </button>
            {/if}
          </span>
        {/each}
      </div>
    {/if}

    <div class="composer-footer">
      <div class="composer-footer-start">
        {#if leadingControls}
          {@render leadingControls()}
        {:else}
          <div class="composer-default-leading">
            <ConstellationComposerOrb
              label={attachLabel}
              title={attachLabel}
              disabled={disabled}
              variant={controlVariant}
              onclick={handleAttach}
            >
              <ConstellationIcon name="attach" size={18} stroke={2} />
            </ConstellationComposerOrb>

            <div class="composer-chip-group" aria-label="Composer settings">
              {#if extraLeadingControls}
                {@render extraLeadingControls()}
              {/if}

              {#if showIntentPicker}
                <ConstellationSelectChip
                  options={selectModeOptions}
                  value={effectiveIntentValue}
                  onValueChange={handleIntentChange}
                  placement="top"
                  variant={controlVariant}
                  className="composer-menu-chip"
                  disabled={disabled}
                  ariaLabel={intentAriaLabel}
                />
              {/if}
              {#if showSettingsPicker && settingsGroups}
                <div
                  bind:this={settingsRootEl}
                  class:composer-settings-chip={true}
                  class:is-open={settingsOpen}
                  role="group"
                  aria-label="Run settings picker"
                  onpointerenter={() => openSettingsMenu()}
                  onpointerleave={queueSettingsMenuClose}
                  onfocusin={() => openSettingsMenu()}
                >
                  <button
                    type="button"
                    class="composer-settings-trigger"
                    aria-label={settingsAriaLabel}
                    aria-expanded={settingsOpen}
                    aria-haspopup="menu"
                    disabled={disabled}
                    onclick={toggleSettingsOpen}
                  >
                    <ConstellationIcon name="settings" size={14} stroke={1.9} />
                    <span class="composer-settings-trigger-label">{settingsSummary}</span>
                    <ConstellationIcon name="chevron-down" size={12} stroke={1.9} className="composer-settings-chevron" />
                  </button>

                  {#if settingsOpen}
                    <div role="menu" class="composer-settings-menu" aria-label={settingsAriaLabel}>
                      <div class="composer-settings-primary" role="group" aria-label="Run setting categories">
                        {#each settingsGroups as group (group.key)}
                          <button
                            type="button"
                            class:composer-settings-group-trigger={true}
                            class:is-active={activeSettingsGroupKey === group.key}
                            role="menuitem"
                            aria-haspopup="menu"
                            aria-expanded={activeSettingsGroupKey === group.key}
                            onpointerenter={() => setActiveSettingsGroup(group.key)}
                            onfocus={() => setActiveSettingsGroup(group.key)}
                            onclick={() => setActiveSettingsGroup(group.key)}
                          >
                            <span class="composer-settings-group-label">{group.label}</span>
                            <span class="composer-settings-group-value">{selectedSettingsOption(group)?.label ?? ''}</span>
                          </button>
                        {/each}
                      </div>

                      <div class="composer-settings-secondary">
                        {#each settingsGroups as group (group.key)}
                          {@const isActiveGroup = activeSettingsGroupKey === group.key}
                          <div
                            class:composer-settings-group-panel={true}
                            class:is-active={isActiveGroup}
                            aria-label={group.ariaLabel ?? group.label}
                            aria-hidden={isActiveGroup ? undefined : 'true'}
                          >
                            <div class="composer-settings-heading">{selectedSettingsLabel(group)}</div>
                            <div class="composer-settings-options">
                              {#each group.options as option}
                                {@const isActive = option.value === (selectedSettingsOption(group)?.value ?? '')}
                                <button
                                  type="button"
                                  role="menuitemradio"
                                  aria-checked={isActive}
                                  title={option.description ?? option.label}
                                  class:composer-settings-option={true}
                                  class:is-active={isActive}
                                  tabindex={isActiveGroup ? 0 : -1}
                                  onclick={() => handleSettingsChange(group.key, option.value)}
                                >
                                  <span class="composer-settings-option-main">
                                    {#if option.icon}
                                      <ConstellationIcon name={option.icon} size={14} stroke={1.9} />
                                    {/if}
                                    <span class="composer-settings-option-copy">
                                      <span class="composer-settings-option-label">{option.label}</span>
                                      {#if option.description}
                                        <span class="composer-settings-option-description">{option.description}</span>
                                      {/if}
                                    </span>
                                  </span>
                                  <span class="composer-settings-option-indicator" aria-hidden="true"></span>
                                </button>
                              {/each}
                            </div>
                          </div>
                        {/each}
                      </div>
                    </div>
                  {/if}
                </div>
              {/if}
              {#if showSecondaryIntentPicker && secondaryIntentOptions}
                <ConstellationSelectChip
                  options={secondaryIntentOptions}
                  value={effectiveSecondaryIntentValue}
                  onValueChange={handleSecondaryIntentChange}
                  placement="top"
                  variant={controlVariant}
                  className="composer-menu-chip"
                  disabled={disabled}
                  ariaLabel={secondaryIntentAriaLabel}
                />
              {/if}
            </div>
          </div>
        {/if}
      </div>

      <div class="composer-footer-end" style={actionStyle}>
        {#if trailingControls}
          {@render trailingControls()}
        {:else}
          <ConstellationComposerActionOrb
            actionState={effectiveActionState}
            label={actionLabel}
            disabled={disabled || (!isWorking && !resolvedCanSubmit)}
            onclick={handleSubmitAction}
          />
        {/if}
      </div>
    </div>

    {#if supporting}
      <div class="composer-supporting">
        {@render supporting()}
      </div>
    {:else if hint}
      <div class="composer-supporting">{hint}</div>
    {/if}
  </div>
</div>

<style>
  .cortex-workspace-composer-shell {
    --workspace-composer-bottom-margin: clamp(14px, 2.4vh, 26px);

    position: absolute;
    left: 50%;
    bottom: var(--workspace-composer-bottom-margin);
    transform: translateX(-50%);
    width: min(640px, calc(100vw - 112px));
    max-height: calc(100svh - var(--workspace-composer-bottom-margin));
    z-index: 28;
    pointer-events: auto;
  }

  :global(.constellation-workspace-backdrop-composer-slot) .cortex-workspace-composer-shell:not(.thread-mode) {
    position: relative;
    left: auto;
    bottom: auto;
    width: 100%;
    max-width: 100%;
    transform: none;
  }

  .cortex-workspace-composer-shell.thread-mode {
    position: relative;
    left: auto;
    bottom: auto;
    transform: none;
    width: 100%;
    max-width: 100%;
    max-height: none;
    z-index: 1;
  }

  .cortex-workspace-composer {
    --constellation-composer-action-accent: rgba(244, 246, 252, 0.78);
    --constellation-composer-action-core: rgba(20, 24, 33, 0.96);
    --constellation-composer-action-owner: rgba(255, 255, 255, 0.94);

    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
    min-height: 0;
    max-height: inherit;
    width: 100%;
    box-sizing: border-box;
    padding: 12px 14px 10px;
    border-radius: 22px;
    border: 1px solid var(--constellation-control-surface-border, rgba(255, 255, 255, 0.08));
    background: var(--constellation-composer-shell-background);
    box-shadow: var(--constellation-composer-shell-shadow);
    backdrop-filter: blur(18px) saturate(1.04);
    -webkit-backdrop-filter: blur(18px) saturate(1.04);
    cursor: text;
    overflow: visible;
  }

  .composer-footer,
  .composer-attachments,
  .composer-attachments-slot,
  .composer-kicker {
    cursor: default;
  }

  .cortex-workspace-composer[data-tone='spectral'] {
    --constellation-composer-action-accent: rgba(244, 246, 252, 0.78);
    --constellation-composer-action-core: rgba(20, 24, 33, 0.96);
    --constellation-composer-action-owner: rgba(255, 255, 255, 0.94);
  }

  .cortex-workspace-composer-shell.drag-over .cortex-workspace-composer {
    border-color: var(--constellation-composer-shell-drag-border);
    box-shadow: var(--constellation-composer-shell-drag-shadow);
    transform: translateY(-1px);
  }

  .composer-kicker {
    margin-bottom: -4px;
    color: var(--constellation-composer-kicker);
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .composer-editor {
    flex: 0 1 auto;
    min-height: 0;
  }

  .composer-textarea {
    display: block;
    width: 100%;
    min-height: 24px;
    max-height: max(40px, calc(100svh - var(--workspace-composer-bottom-margin) - 96px));
    padding: 1px 3px 0;
    border: 0;
    background: transparent;
    color: var(--constellation-composer-textarea);
    font-family: inherit;
    font-size: 15px;
    line-height: 1.45;
    resize: none;
    outline: none;
    box-sizing: border-box;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .composer-textarea::placeholder {
    color: var(--constellation-composer-placeholder);
  }

  .cortex-workspace-composer-shell:not(.thread-mode) .cortex-workspace-composer {
    gap: 8px;
    padding: 12px 14px 10px;
    border-radius: var(--constellation-radius-panel);
  }

  .cortex-workspace-composer-shell:not(.thread-mode) .composer-editor {
    min-height: 0;
  }

  .cortex-workspace-composer-shell:not(.thread-mode) .composer-textarea {
    min-height: 24px;
    max-height: max(40px, calc(100svh - var(--workspace-composer-bottom-margin) - 96px));
    padding: 1px 3px 0;
    font-size: 15px;
    line-height: 1.45;
  }

  .cortex-workspace-composer-shell:not(.thread-mode) .composer-footer {
    min-height: var(--composer-action-slot-size);
    padding-block: 2px;
  }

  .composer-attachments,
  .composer-attachments-slot {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .composer-attachment {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: 28px;
    max-width: min(100%, 220px);
    padding: 5px 9px;
    border-radius: 999px;
    background: var(--constellation-composer-attachment-background);
    border: 1px solid var(--constellation-composer-attachment-border);
    color: var(--constellation-composer-attachment-text);
  }

  .composer-attachment img {
    width: 20px;
    height: 20px;
    border-radius: 999px;
    object-fit: cover;
  }

  .composer-attachment :global(svg) {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }

  .composer-attachment-label {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12px;
  }

  .composer-attachment-remove {
    border: 0;
    background: transparent;
    color: var(--constellation-composer-attachment-remove);
    cursor: pointer;
    font-size: 18px;
    line-height: 1;
    padding: 0;
  }

  .composer-footer {
    --composer-action-slot-size: var(
      --constellation-composer-action-button-size,
      var(--constellation-composer-orb-size, 32px)
    );

    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    margin-top: auto;
    min-height: var(--composer-action-slot-size);
    padding-block: 2px;
  }

  .composer-footer-start,
  .composer-footer-end {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .composer-footer-start {
    flex: 1 1 auto;
  }

  .composer-footer-end {
    flex: 0 0 auto;
    min-width: var(--composer-action-slot-size);
    min-height: var(--composer-action-slot-size);
    align-self: center;
    box-sizing: border-box;
  }

  .composer-default-leading,
  .composer-chip-group {
    display: flex;
    align-items: center;
    min-width: 0;
  }

  .composer-default-leading {
    flex: 1 1 auto;
    gap: 8px;
  }

  .composer-chip-group {
    --composer-menu-border: rgba(240, 240, 250, 0.09);
    --composer-menu-background: rgba(9, 12, 18, 0.98);
    --composer-menu-shadow:
      0 18px 42px rgba(0, 0, 0, 0.32),
      0 0 0 1px rgba(255, 255, 255, 0.02) inset;
    --composer-menu-option-hover-background: rgba(240, 240, 250, 0.06);
    --composer-menu-option-active-background: rgba(240, 240, 250, 0.075);
    --constellation-select-chip-indicator-active-border: rgba(240, 240, 250, 0.34);
    --constellation-select-chip-indicator-active-background: rgba(240, 240, 250, 0.86);
    --constellation-select-chip-indicator-active-shadow: none;

    container: composer-controls / inline-size;
    flex: 1 1 auto;
    flex-wrap: nowrap;
    gap: 7px;
  }

  :global(:root[data-color-scheme='light']) .composer-chip-group {
    --composer-menu-border: rgba(49, 63, 76, 0.12);
    --composer-menu-background: rgba(255, 253, 247, 0.98);
    --composer-menu-shadow: 0 18px 38px rgba(54, 70, 82, 0.13);
    --composer-menu-option-hover-background: rgba(49, 63, 76, 0.055);
    --composer-menu-option-active-background: rgba(49, 63, 76, 0.075);
    --constellation-select-chip-indicator-active-border: rgba(49, 63, 76, 0.34);
    --constellation-select-chip-indicator-active-background: rgba(49, 63, 76, 0.82);
    --constellation-select-chip-indicator-active-shadow: none;
  }

  .composer-chip-group :global(.constellation-select-chip),
  .composer-chip-group :global(.project-context-composer) {
    min-width: 0;
  }

  .composer-chip-group :global(.constellation-select-chip-trigger),
  .composer-chip-group :global(.project-context-chip) {
    max-width: 100%;
  }

  .composer-chip-group :global(.constellation-select-chip-trigger-label),
  .composer-chip-group :global(.project-context-chip-label) {
    min-width: 0;
  }

  .composer-chip-group :global(.composer-menu-chip .constellation-select-chip-menu) {
    padding: 6px;
    border-color: var(--composer-menu-border);
    border-radius: 12px;
    background: var(--composer-menu-background);
    box-shadow: var(--composer-menu-shadow);
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .composer-chip-group :global(.composer-menu-chip .constellation-select-chip-option) {
    gap: 10px;
    min-height: 34px;
    padding: 6px 8px;
    border-radius: 9px;
  }

  .composer-chip-group :global(.composer-menu-chip .constellation-select-chip-option:hover) {
    background: var(--composer-menu-option-hover-background);
    transform: none;
  }

  .composer-chip-group :global(.composer-menu-chip .constellation-select-chip-option.is-active) {
    background: var(--composer-menu-option-active-background);
  }

  .composer-chip-group :global(.composer-menu-chip .constellation-select-chip-option-label) {
    font-size: 12px;
  }

  .composer-chip-group :global(.composer-menu-chip .constellation-select-chip-option-description) {
    font-size: 10.5px;
    line-height: 1.25;
  }

  .composer-chip-group :global(.composer-menu-chip .constellation-select-chip-option-end) {
    gap: 8px;
  }


  .composer-settings-chip {
    position: relative;
    display: inline-flex;
    min-width: 0;
  }

  .composer-settings-chip.is-open::before {
    content: '';
    position: absolute;
    left: -10px;
    right: -220px;
    bottom: 100%;
    height: 16px;
    pointer-events: auto;
  }

  .composer-settings-trigger {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 30px;
    max-width: 100%;
    padding: 0 4px;
    border: 0;
    background: transparent;
    color: var(--constellation-select-chip-trigger-bare-text);
    font: inherit;
    font-size: 12px;
    cursor: pointer;
  }

  .composer-settings-trigger:hover:not(:disabled),
  .composer-settings-chip.is-open .composer-settings-trigger {
    color: var(--constellation-select-chip-trigger-hover-text);
  }

  .composer-settings-trigger:disabled {
    opacity: 0.42;
    cursor: default;
  }

  .composer-settings-trigger:focus-visible,
  .composer-settings-group-trigger:focus-visible,
  .composer-settings-option:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .composer-settings-trigger:focus-visible {
    border-radius: 999px;
  }

  .composer-settings-trigger-label {
    min-width: 0;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .composer-settings-chevron {
    flex-shrink: 0;
    transition: transform var(--constellation-motion-hover-duration) ease;
  }

  .composer-settings-trigger[aria-expanded='true'] :global(.composer-settings-chevron) {
    transform: rotate(180deg);
  }

  .composer-settings-menu {
    position: absolute;
    left: 0;
    bottom: calc(100% + 8px);
    z-index: 32;
    display: grid;
    grid-template-columns: 128px minmax(220px, 1fr);
    gap: 6px;
    width: min(386px, calc(100vw - 32px));
    max-width: min(520px, calc(100vw - 32px));
    padding: 7px;
    border: 1px solid var(--composer-menu-border);
    border-radius: 14px;
    background: var(--composer-menu-background);
    box-shadow: var(--composer-menu-shadow);
  }

  .composer-settings-primary,
  .composer-settings-secondary {
    min-width: 0;
  }

  .composer-settings-primary {
    display: grid;
    align-content: start;
    gap: 3px;
    padding-right: 5px;
    border-right: 1px solid color-mix(in oklab, var(--composer-menu-border), transparent 38%);
  }

  .composer-settings-secondary {
    position: relative;
    display: grid;
    align-content: start;
    min-height: 174px;
  }

  .composer-settings-group-trigger {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    column-gap: 12px;
    min-height: 32px;
    padding: 6px 7px;
    border: 0;
    border-radius: 9px;
    background: transparent;
    color: var(--constellation-select-chip-option-text);
    font: inherit;
    cursor: pointer;
    text-align: left;
  }

  .composer-settings-group-trigger:hover,
  .composer-settings-group-trigger.is-active {
    background: var(--composer-menu-option-hover-background);
  }

  .composer-settings-group-trigger.is-active {
    color: var(--constellation-select-chip-trigger-hover-text);
  }

  .composer-settings-group-label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12px;
    font-weight: 650;
  }

  .composer-settings-group-value {
    max-width: 78px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--constellation-composer-kicker);
    font-size: 10.5px;
  }

  .composer-settings-group-panel {
    display: grid;
    gap: 5px;
    grid-area: 1 / 1;
    min-width: 0;
    opacity: 0;
    pointer-events: none;
    visibility: hidden;
  }

  .composer-settings-group-panel.is-active {
    opacity: 1;
    pointer-events: auto;
    visibility: visible;
  }

  .composer-settings-heading {
    padding: 0 4px;
    color: var(--constellation-composer-kicker);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .composer-settings-options {
    display: grid;
    gap: 4px;
  }

  .composer-settings-option {
    display: inline-flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 34px;
    padding: 6px 7px;
    border: 0;
    border-radius: 9px;
    background: transparent;
    color: var(--constellation-select-chip-option-text);
    font: inherit;
    font-size: 12px;
    cursor: pointer;
    text-align: left;
  }

  .composer-settings-option:hover {
    background: var(--composer-menu-option-hover-background);
  }

  .composer-settings-option.is-active {
    background: var(--composer-menu-option-active-background);
  }

  .composer-settings-option-main {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .composer-settings-option-copy {
    display: grid;
    gap: 1px;
    min-width: 0;
  }

  .composer-settings-option-label,
  .composer-settings-option-description {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .composer-settings-option-label {
    font-weight: 650;
  }

  .composer-settings-option-description {
    color: var(--constellation-composer-kicker);
    font-size: 10.5px;
    line-height: 1.2;
  }

  .composer-settings-option-indicator {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: transparent;
    flex-shrink: 0;
  }

  .composer-settings-option.is-active .composer-settings-option-indicator {
    background: var(--constellation-select-chip-indicator-active-background);
  }

  @container composer-controls (max-width: 230px) {
    .composer-chip-group :global(.constellation-select-chip-trigger),
    .composer-chip-group :global(.project-context-chip),
    .composer-settings-trigger {
      width: 30px;
      padding-inline: 0;
    }

    .composer-settings-chip.is-open::before {
      left: -220px;
      right: -10px;
    }

    .composer-settings-menu {
      left: auto;
      right: 0;
    }

    .composer-chip-group :global(.constellation-select-chip-trigger-label),
    .composer-chip-group :global(.project-context-chip-label),
    .composer-chip-group :global(.constellation-select-chip-chevron),
    .composer-chip-group :global(.project-context-chip-chevron),
    .composer-settings-trigger-label,
    .composer-settings-chevron {
      display: none;
    }
  }

  .thread-mode .cortex-workspace-composer {
    min-height: 102px;
    max-height: min(220px, calc(100svh - 164px));
    padding: 16px 18px 14px;
    border-color: var(--constellation-thread-composer-border);
    background: var(--constellation-thread-composer-background);
    box-shadow: var(--constellation-thread-composer-shadow);
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .thread-mode .composer-editor {
    min-height: 34px;
    max-height: 120px;
    overflow: hidden;
  }

  .thread-mode .composer-textarea {
    min-height: 34px;
    max-height: 120px;
    font-size: 14px;
  }

  @media (max-width: 900px) {
    .cortex-workspace-composer-shell {
      width: min(100vw - 24px, 560px);
      bottom: 14px;
    }

    .cortex-workspace-composer {
      border-radius: var(--constellation-radius-panel);
      min-height: 0;
      padding: 11px 12px 10px;
    }

    .composer-footer {
      gap: 8px;
    }
  }

  @media (max-width: 560px) {
    .composer-default-leading {
      flex-wrap: nowrap;
      row-gap: 8px;
    }

    .composer-footer {
      align-items: center;
    }
  }

  @media (max-height: 680px) {
    .cortex-workspace-composer-shell {
      bottom: 10px;
    }

    .cortex-workspace-composer {
      padding-block: 10px 8px;
    }

    .composer-textarea {
      max-height: 72px;
    }
  }
</style>
