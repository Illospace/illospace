<script lang="ts">
  export type ConstellationPageTabOption = {
    key: string;
    label: string;
    meta?: string | number | null;
    disabled?: boolean;
  };

  type Props = {
    options?: ReadonlyArray<ConstellationPageTabOption>;
    activeKey?: string;
    defaultActiveKey?: string;
    onActiveKeyChange?: (key: string) => void;
    className?: string;
    ariaLabel?: string;
  };

  let {
    options = [],
    activeKey,
    defaultActiveKey,
    onActiveKeyChange,
    className = '',
    ariaLabel = 'Page sections',
  }: Props = $props();

  let localActiveKey = $state('');
  let rootEl: HTMLDivElement | undefined = $state();

  function isTabKey(key: string | undefined, candidates: ReadonlyArray<ConstellationPageTabOption>) {
    return key !== undefined && candidates.some((option) => option.key === key && !option.disabled);
  }

  const fallbackActiveKey = $derived(
    (isTabKey(defaultActiveKey, options) ? defaultActiveKey : undefined) ??
      options.find((option) => !option.disabled)?.key ??
      '',
  );
  const resolvedActiveKey = $derived(
    (isTabKey(activeKey, options) ? activeKey : undefined) ??
      (isTabKey(localActiveKey, options) ? localActiveKey : undefined) ??
      fallbackActiveKey,
  );
  const rootClass = $derived(['constellation-page-tabs', className].filter(Boolean).join(' '));

  $effect(() => {
    if (activeKey !== undefined) return;
    if (!isTabKey(localActiveKey, options)) {
      localActiveKey = fallbackActiveKey;
    }
  });

  function selectTab(nextKey: string) {
    const option = options.find((item) => item.key === nextKey);
    if (!option || option.disabled || option.key === resolvedActiveKey) return;
    if (activeKey === undefined) {
      localActiveKey = nextKey;
    }
    onActiveKeyChange?.(nextKey);
  }

  function focusTab(index: number) {
    if (!rootEl) return;
    const buttons = Array.from(rootEl.querySelectorAll<HTMLButtonElement>('.constellation-page-tabs-option'));
    const nextButton = buttons[index];
    if (!nextButton) return;
    if (typeof window === 'undefined') {
      nextButton.focus();
      return;
    }
    window.requestAnimationFrame(() => nextButton.focus());
  }

  function handleKeyDown(event: KeyboardEvent, index: number) {
    const enabledOptions = options
      .map((option, optionIndex) => ({ option, optionIndex }))
      .filter((entry) => !entry.option.disabled);
    const enabledIndex = enabledOptions.findIndex((entry) => entry.optionIndex === index);
    if (enabledIndex === -1) return;

    let nextEnabledIndex = enabledIndex;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      nextEnabledIndex = (enabledIndex + 1) % enabledOptions.length;
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      nextEnabledIndex = (enabledIndex - 1 + enabledOptions.length) % enabledOptions.length;
    } else if (event.key === 'Home') {
      nextEnabledIndex = 0;
    } else if (event.key === 'End') {
      nextEnabledIndex = enabledOptions.length - 1;
    } else {
      return;
    }

    const next = enabledOptions[nextEnabledIndex];
    event.preventDefault();
    selectTab(next.option.key);
    focusTab(next.optionIndex);
  }
</script>

{#if options.length > 0}
  <div bind:this={rootEl} class={rootClass} role="tablist" aria-label={ariaLabel}>
    {#each options as option, index (option.key)}
      {@const isActive = option.key === resolvedActiveKey}
      <button
        type="button"
        role="tab"
        aria-selected={isActive}
        aria-controls={`${option.key}-panel`}
        id={`${option.key}-tab`}
        tabindex={isActive ? 0 : -1}
        class={`constellation-page-tabs-option ${isActive ? 'is-active' : ''}`}
        disabled={option.disabled}
        onclick={() => selectTab(option.key)}
        onkeydown={(event) => handleKeyDown(event, index)}
      >
        <span class="constellation-page-tabs-label">{option.label}</span>
        {#if option.meta !== null && option.meta !== undefined && option.meta !== ''}
          <span class="constellation-page-tabs-meta">{option.meta}</span>
        {/if}
      </button>
    {/each}
  </div>
{/if}

<style>
  .constellation-page-tabs {
    display: flex;
    align-items: flex-end;
    gap: 22px;
    min-width: 0;
    overflow-x: auto;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    scrollbar-width: none;
  }

  .constellation-page-tabs::-webkit-scrollbar {
    display: none;
  }

  .constellation-page-tabs-option {
    appearance: none;
    -webkit-appearance: none;
    display: inline-flex;
    position: relative;
    align-items: center;
    gap: 8px;
    min-height: 42px;
    min-width: max-content;
    padding: 0 0 12px;
    border: 0;
    border-bottom: 2px solid transparent;
    background: transparent;
    color: var(--constellation-color-text-secondary);
    cursor: pointer;
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    letter-spacing: 0.14em;
    line-height: 1;
    text-transform: uppercase;
    transition:
      border-color var(--constellation-motion-settle-duration) ease,
      color var(--constellation-motion-settle-duration) ease,
      opacity var(--constellation-motion-settle-duration) ease;
  }

  .constellation-page-tabs-option:hover:not(:disabled),
  .constellation-page-tabs-option.is-active {
    color: var(--constellation-color-text-primary);
  }

  .constellation-page-tabs-option.is-active {
    border-bottom-color: var(--constellation-control-focus-ring);
  }

  .constellation-page-tabs-option:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 4px;
  }

  .constellation-page-tabs-option:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .constellation-page-tabs-meta {
    display: inline-flex;
    align-items: center;
    min-height: 20px;
    padding: 2px 7px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-control-button-secondary-background);
    color: var(--constellation-color-text-secondary);
    font-size: 10px;
    letter-spacing: 0.08em;
  }

  .constellation-page-tabs-option.is-active .constellation-page-tabs-meta {
    border-color: var(--constellation-button-pressed-border);
    background: var(--constellation-button-pressed-background);
    color: var(--constellation-button-pressed-text);
  }
</style>
