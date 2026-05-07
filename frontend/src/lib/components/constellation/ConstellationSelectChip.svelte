<script lang="ts">
  import { tick } from 'svelte';
  import ConstellationIcon, { type ConstellationIconName } from './ConstellationIcon.svelte';

  export type ConstellationSelectChipOption = {
    value: string;
    label: string;
    description?: string;
    shortcut?: string;
    icon?: ConstellationIconName;
  };

  type Props = {
    options: ReadonlyArray<ConstellationSelectChipOption>;
    defaultValue?: string;
    value?: string;
    onValueChange?: (value: string) => void;
    placement?: 'top' | 'bottom';
    align?: 'start' | 'end';
    positioning?: 'absolute' | 'fixed';
    className?: string;
    variant?: 'glass' | 'bare';
    disabled?: boolean;
    ariaLabel?: string;
  };

  let {
    options,
    defaultValue,
    value,
    onValueChange,
    placement = 'bottom',
    align = 'start',
    positioning = 'absolute',
    className = '',
    variant = 'glass',
    disabled = false,
    ariaLabel,
  }: Props = $props();

  let open = $state(false);
  let rootEl: HTMLDivElement | undefined = $state();
  let triggerEl: HTMLButtonElement | undefined = $state();
  let fixedMenuStyle = $state('');
  let localValue = $state('');

  const fallbackValue = $derived(defaultValue ?? options[0]?.value ?? '');
  const selectedValue = $derived(value ?? localValue);
  const selectedOption = $derived(options.find((option) => option.value === selectedValue) ?? options[0] ?? null);
  const rootClass = $derived(
    [
      'constellation-select-chip',
      open ? 'is-open' : '',
      variant === 'bare' ? 'is-bare' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
  const menuClass = $derived(
    [
      'constellation-select-chip-menu',
      placement === 'top' ? 'is-top' : 'is-bottom',
      align === 'end' ? 'is-end' : 'is-start',
      positioning === 'fixed' ? 'is-fixed' : '',
    ].join(' '),
  );

  $effect(() => {
    if (value !== undefined) return;

    const nextValue = fallbackValue;
    const hasLocalMatch = options.some((option) => option.value === localValue);
    if (!hasLocalMatch || localValue === '') {
      localValue = nextValue;
    }
  });

  $effect(() => {
    if (!open) return;

    function handlePointerDown(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (!rootEl?.contains(target)) {
        open = false;
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        open = false;
      }
    }

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  });

  $effect(() => {
    if (!open || positioning !== 'fixed') return;

    tick().then(updateFixedMenuPosition);
    window.addEventListener('resize', updateFixedMenuPosition);
    window.addEventListener('scroll', updateFixedMenuPosition, true);

    return () => {
      window.removeEventListener('resize', updateFixedMenuPosition);
      window.removeEventListener('scroll', updateFixedMenuPosition, true);
    };
  });

  function updateFixedMenuPosition() {
    if (!triggerEl || typeof window === 'undefined') return;

    const rect = triggerEl.getBoundingClientRect();
    const offset = 8;
    const viewportPad = 12;
    const menuMinWidth = Math.max(220, rect.width);
    const horizontalProperty = align === 'end' ? 'right' : 'left';
    const rawHorizontalValue = align === 'end' ? window.innerWidth - rect.right : rect.left;
    const maxHorizontalValue = Math.max(viewportPad, window.innerWidth - viewportPad - menuMinWidth);
    const horizontalValue = clamp(rawHorizontalValue, viewportPad, maxHorizontalValue);
    const verticalProperty = placement === 'top' ? 'bottom' : 'top';
    const verticalValue = placement === 'top' ? window.innerHeight - rect.top + offset : rect.bottom + offset;

    fixedMenuStyle = [
      `${horizontalProperty}: ${Math.round(horizontalValue)}px`,
      `${verticalProperty}: ${Math.round(verticalValue)}px`,
      `min-width: ${Math.round(menuMinWidth)}px`,
    ].join('; ');
  }

  function clamp(value: number, min: number, max: number) {
    if (max < min) return min;
    return Math.min(max, Math.max(min, value));
  }

  function toggleOpen() {
    if (disabled || !selectedOption) return;
    open = !open;
    if (!open) fixedMenuStyle = '';
  }

  function handleSelect(nextValue: string) {
    if (value === undefined) {
      localValue = nextValue;
    }

    onValueChange?.(nextValue);
    open = false;
  }
</script>

{#if selectedOption}
  <div bind:this={rootEl} class={rootClass}>
    <button
      type="button"
      bind:this={triggerEl}
      class="constellation-select-chip-trigger"
      aria-label={ariaLabel ?? selectedOption.label}
      aria-expanded={open}
      aria-haspopup="menu"
      disabled={disabled}
      onclick={toggleOpen}
    >
      {#if selectedOption.icon}
        <ConstellationIcon
          name={selectedOption.icon}
          size={14}
          stroke={1.9}
          className="constellation-select-chip-trigger-icon"
        />
      {/if}
      <span class="constellation-select-chip-trigger-label">{selectedOption.label}</span>
      <ConstellationIcon name="chevron-down" size={12} stroke={1.9} className="constellation-select-chip-chevron" />
    </button>

    {#if open}
      <div role="menu" class={menuClass} style={positioning === 'fixed' ? fixedMenuStyle : undefined}>
        {#each options as option}
          {@const isActive = option.value === selectedOption.value}
          <button
            type="button"
            role="menuitemradio"
            aria-checked={isActive}
            class={`constellation-select-chip-option ${isActive ? 'is-active' : ''}`}
            onclick={() => handleSelect(option.value)}
          >
            {#if option.icon}
              <ConstellationIcon
                name={option.icon}
                size={14}
                stroke={1.9}
                className="constellation-select-chip-option-icon"
              />
            {/if}
            <span class="constellation-select-chip-option-text">
              <span class="constellation-select-chip-option-label">{option.label}</span>
              {#if option.description}
                <span class="constellation-select-chip-option-description">{option.description}</span>
              {/if}
            </span>
            <span class="constellation-select-chip-option-end">
              {#if option.shortcut}
                <span class="constellation-select-chip-option-shortcut" aria-hidden="true">{option.shortcut}</span>
              {/if}
              <span class="constellation-select-chip-option-indicator" aria-hidden="true"></span>
            </span>
          </button>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .constellation-select-chip {
    position: relative;
    display: inline-flex;
  }

  .constellation-select-chip-trigger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    height: 34px;
    padding: 0 12px;
    border: 1px solid var(--constellation-select-chip-trigger-border);
    border-radius: 999px;
    background: var(--constellation-select-chip-trigger-background);
    color: var(--constellation-select-chip-trigger-text);
    font-family: var(--constellation-font-sans);
    font-size: 12px;
    line-height: 1;
    white-space: nowrap;
    cursor: pointer;
    transition:
      transform var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-hover-duration) ease,
      border-color var(--constellation-motion-hover-duration) ease,
      color var(--constellation-motion-hover-duration) ease,
      box-shadow var(--constellation-motion-hover-duration) ease;
  }

  .constellation-select-chip-trigger:hover:not(:disabled) {
    transform: translateY(-1px);
    background: var(--constellation-select-chip-trigger-hover-background);
    color: var(--constellation-select-chip-trigger-hover-text);
  }

  .constellation-select-chip-trigger:active:not(:disabled) {
    transform: translateY(1px) scale(0.98);
  }

  .constellation-select-chip-trigger:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-select-chip-trigger:disabled {
    opacity: 0.42;
    cursor: default;
    transform: none;
  }

  .constellation-select-chip.is-open .constellation-select-chip-trigger {
    border-color: var(--constellation-select-chip-trigger-open-border);
    background: var(--constellation-select-chip-trigger-open-background);
    color: var(--constellation-select-chip-trigger-open-text);
    box-shadow: var(--constellation-select-chip-trigger-open-shadow);
  }

  .constellation-select-chip.is-bare .constellation-select-chip-trigger {
    height: 30px;
    gap: 5px;
    padding: 0 4px;
    border-color: transparent;
    background: transparent;
    color: var(--constellation-select-chip-trigger-bare-text);
    box-shadow: none;
  }

  .constellation-select-chip.is-bare .constellation-select-chip-trigger:hover:not(:disabled),
  .constellation-select-chip.is-bare.is-open .constellation-select-chip-trigger {
    border-color: transparent;
    background: transparent;
    color: var(--constellation-select-chip-trigger-hover-text);
    box-shadow: none;
  }

  .constellation-select-chip.is-bare .constellation-select-chip-trigger:active:not(:disabled) {
    transform: translateY(0) scale(0.98);
  }

  .constellation-select-chip-chevron {
    flex-shrink: 0;
    transition: transform var(--constellation-motion-hover-duration) ease;
  }

  .constellation-select-chip-trigger-icon,
  .constellation-select-chip-option-icon {
    flex-shrink: 0;
  }

  .constellation-select-chip.is-open .constellation-select-chip-chevron {
    transform: rotate(180deg);
  }

  .constellation-select-chip-menu {
    position: absolute;
    z-index: 30;
    min-width: 220px;
    padding: 8px;
    border: 1px solid var(--constellation-select-chip-menu-border);
    border-radius: 16px;
    background: var(--constellation-select-chip-menu-background);
    box-shadow: var(--constellation-select-chip-menu-shadow);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
  }

  .constellation-select-chip-menu.is-fixed {
    position: fixed;
    z-index: var(--constellation-layer-popover, 1000);
    max-width: calc(100vw - 24px);
  }

  .constellation-select-chip-menu.is-bottom {
    top: calc(100% + 8px);
  }

  .constellation-select-chip-menu.is-top {
    bottom: calc(100% + 8px);
  }

  .constellation-select-chip-menu.is-start {
    left: 0;
  }

  .constellation-select-chip-menu.is-end {
    right: 0;
  }

  .constellation-select-chip-menu.is-fixed.is-bottom,
  .constellation-select-chip-menu.is-fixed.is-top {
    top: auto;
    bottom: auto;
  }

  .constellation-select-chip-menu.is-fixed.is-start,
  .constellation-select-chip-menu.is-fixed.is-end {
    right: auto;
    left: auto;
  }

  .constellation-select-chip-option {
    display: flex;
    width: 100%;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding: 10px 12px;
    border: 0;
    border-radius: 12px;
    background: transparent;
    color: var(--constellation-select-chip-option-text);
    text-align: left;
    cursor: pointer;
    transition:
      background-color var(--constellation-motion-hover-duration) ease,
      color var(--constellation-motion-hover-duration) ease,
      transform var(--constellation-motion-hover-duration) ease;
  }

  .constellation-select-chip-option:hover {
    background: var(--constellation-select-chip-option-hover-background);
    color: var(--constellation-select-chip-option-hover-text);
    transform: translateX(1px);
  }

  .constellation-select-chip-option.is-active {
    background: var(--constellation-select-chip-option-active-background);
    color: var(--constellation-select-chip-option-active-text);
  }

  .constellation-select-chip-option-text {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .constellation-select-chip-option-end {
    display: inline-flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 10px;
  }

  .constellation-select-chip-option-shortcut {
    display: inline-flex;
    align-items: center;
    min-height: 18px;
    padding: 0 6px;
    border-radius: 6px;
    background: var(--constellation-select-chip-shortcut-background);
    color: var(--constellation-select-chip-shortcut-text);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    line-height: 1;
    letter-spacing: 0;
  }

  .constellation-select-chip-option-label {
    font-family: var(--constellation-font-sans);
    font-size: 13px;
    line-height: 1.25;
  }

  .constellation-select-chip-option-description {
    color: var(--constellation-select-chip-option-description);
    font-family: var(--constellation-font-sans);
    font-size: 11px;
    line-height: 1.35;
  }

  .constellation-select-chip-option-indicator {
    width: 8px;
    height: 8px;
    flex-shrink: 0;
    border-radius: 999px;
    border: 1px solid var(--constellation-select-chip-indicator-border);
    background: var(--constellation-select-chip-indicator-background);
    transition:
      border-color var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-hover-duration) ease,
      box-shadow var(--constellation-motion-hover-duration) ease;
  }

  .constellation-select-chip-option.is-active .constellation-select-chip-option-indicator {
    border-color: var(--constellation-select-chip-indicator-active-border);
    background: var(--constellation-select-chip-indicator-active-background);
    box-shadow: var(--constellation-select-chip-indicator-active-shadow);
  }
</style>
