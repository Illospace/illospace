<script lang="ts">
  import ConstellationIcon, { type ConstellationIconName } from './ConstellationIcon.svelte';

  export type ConstellationSegmentedToggleOption = {
    key: string;
    label: string;
    icon?: ConstellationIconName;
  };

  type Props = {
    options?: ReadonlyArray<ConstellationSegmentedToggleOption>;
    activeKey?: string;
    defaultActiveKey?: string;
    onActiveKeyChange?: (key: string) => void;
    className?: string;
    disabled?: boolean;
    ariaLabel?: string;
  };

  const defaultOptions: ConstellationSegmentedToggleOption[] = [
    { key: 'canvas', label: 'Canvas' },
    { key: 'list', label: 'List' },
  ];

  let {
    options = defaultOptions,
    activeKey,
    defaultActiveKey,
    onActiveKeyChange,
    className = '',
    disabled = false,
    ariaLabel = 'View mode',
  }: Props = $props();

  let localActiveKey = $state('');
  let rootEl: HTMLDivElement | undefined = $state();

  function isViewKey(key: string | undefined, candidates: ReadonlyArray<ConstellationSegmentedToggleOption>) {
    return key !== undefined && candidates.some((option) => option.key === key);
  }

  const fallbackActiveKey = $derived(
    (isViewKey(defaultActiveKey, options) ? defaultActiveKey : undefined) ?? options[0]?.key ?? '',
  );

  const resolvedActiveKey = $derived(
    (isViewKey(activeKey, options) ? activeKey : undefined) ??
      (isViewKey(localActiveKey, options) ? localActiveKey : undefined) ??
      fallbackActiveKey,
  );

  const activeIndex = $derived(Math.max(options.findIndex((option) => option.key === resolvedActiveKey), 0));

  const rootClass = $derived(
    ['constellation-segmented-toggle', disabled ? 'is-disabled' : '', className]
      .filter(Boolean)
      .join(' '),
  );

  const rootStyle = $derived(
    `--constellation-segment-count:${options.length};--constellation-segment-index:${activeIndex};`,
  );

  $effect(() => {
    if (activeKey !== undefined) {
      return;
    }

    if (!isViewKey(localActiveKey, options)) {
      localActiveKey = fallbackActiveKey;
    }
  });

  function handleSelect(nextKey: string) {
    if (disabled || nextKey === resolvedActiveKey) {
      return;
    }

    if (activeKey === undefined) {
      localActiveKey = nextKey;
    }

    onActiveKeyChange?.(nextKey);
  }

  function focusOption(index: number) {
    if (!rootEl) {
      return;
    }

    const buttons = Array.from(
      rootEl.querySelectorAll<HTMLButtonElement>('.constellation-segmented-toggle-option'),
    );
    const nextButton = buttons[index];

    if (!nextButton) {
      return;
    }

    if (typeof window === 'undefined') {
      nextButton.focus();
      return;
    }

    window.requestAnimationFrame(() => nextButton.focus());
  }

  function handleKeyDown(event: KeyboardEvent, index: number) {
    if (disabled) {
      return;
    }

    let nextIndex = index;

    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      nextIndex = (index + 1) % options.length;
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      nextIndex = (index - 1 + options.length) % options.length;
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = options.length - 1;
    } else {
      return;
    }

    event.preventDefault();
    handleSelect(options[nextIndex].key);
    focusOption(nextIndex);
  }
</script>

{#if options.length > 0}
  <div
    bind:this={rootEl}
    class={rootClass}
    role="radiogroup"
    aria-label={ariaLabel}
    aria-disabled={disabled || undefined}
    style={rootStyle}
  >
    <span class="constellation-segmented-toggle-thumb" aria-hidden="true"></span>

    {#each options as option, index (option.key)}
      {@const isActive = option.key === resolvedActiveKey}
      <button
        type="button"
        role="radio"
        aria-checked={isActive}
        aria-label={option.icon ? option.label : undefined}
        tabindex={isActive ? 0 : -1}
        class={`constellation-segmented-toggle-option ${option.icon ? 'has-icon' : ''} ${isActive ? 'is-active' : ''}`}
        {disabled}
        onclick={() => handleSelect(option.key)}
        onkeydown={(event) => handleKeyDown(event, index)}
      >
        {#if option.icon}
          <ConstellationIcon
            name={option.icon}
            size={16}
            stroke={1.9}
            className="constellation-segmented-toggle-icon"
          />
          <span class="constellation-segmented-toggle-label is-visually-hidden">{option.label}</span>
        {:else}
          <span class="constellation-segmented-toggle-label">{option.label}</span>
        {/if}
      </button>
    {/each}
  </div>
{/if}

<style>
  .constellation-segmented-toggle {
    --constellation-segment-count: 2;
    --constellation-segment-index: 0;
    position: relative;
    display: inline-grid;
    grid-template-columns: repeat(var(--constellation-segment-count), minmax(0, 1fr));
    min-width: max-content;
    padding: 2px;
    overflow: hidden;
    border: 1px solid var(--constellation-control-surface-border);
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-control-button-secondary-background);
    isolation: isolate;
  }

  .constellation-segmented-toggle-thumb {
    position: absolute;
    top: 2px;
    bottom: 2px;
    left: 2px;
    width: calc((100% - 4px) / var(--constellation-segment-count));
    border-radius: inherit;
    background: var(--constellation-control-toggle-active-background);
    transform: translateX(calc(var(--constellation-segment-index) * 100%));
    transition:
      transform var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      background-color var(--constellation-motion-settle-duration) ease;
    pointer-events: none;
  }

  .constellation-segmented-toggle-option {
    position: relative;
    z-index: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 0;
    padding: 7px 10px;
    border: 0;
    background: transparent;
    color: var(--constellation-control-button-secondary-text);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    line-height: 1;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    white-space: nowrap;
    cursor: pointer;
    transition:
      color var(--constellation-motion-hover-duration) ease,
      transform var(--constellation-motion-hover-duration) ease,
      opacity var(--constellation-motion-hover-duration) ease;
  }

  .constellation-segmented-toggle-option.has-icon {
    min-width: 38px;
    min-height: 34px;
    padding: 8px 10px;
  }

  .constellation-segmented-toggle-option:hover:not(:disabled) {
    color: var(--constellation-color-text-primary);
  }

  .constellation-segmented-toggle-option:active:not(:disabled) {
    transform: translateY(1px);
  }

  .constellation-segmented-toggle-option:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-segmented-toggle-label {
    position: relative;
    z-index: 1;
  }

  .constellation-segmented-toggle-icon {
    position: relative;
    z-index: 1;
    flex: 0 0 auto;
  }

  .constellation-segmented-toggle-label.is-visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  .constellation-segmented-toggle-option.is-active {
    color: var(--constellation-control-toggle-active-text);
  }

  .constellation-segmented-toggle.is-disabled {
    opacity: 0.52;
  }

  .constellation-segmented-toggle.is-disabled .constellation-segmented-toggle-option {
    cursor: default;
  }

  @media (prefers-reduced-motion: reduce) {
    .constellation-segmented-toggle-thumb,
    .constellation-segmented-toggle-option {
      transition: none;
    }
  }
</style>
