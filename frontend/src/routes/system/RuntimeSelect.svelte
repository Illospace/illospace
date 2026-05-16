<script lang="ts">
  import { tick } from 'svelte';

  import { ConstellationIcon } from '$lib/components/constellation';

  export interface RuntimeOption {
    key: string;
    label: string;
    description?: string | null;
    disabled?: boolean;
  }

  let {
    id,
    label,
    value,
    options,
    disabled = false,
    onValueChange,
  }: {
    id: string;
    label: string;
    value: string;
    options: RuntimeOption[];
    disabled?: boolean;
    onValueChange?: (value: string) => void;
  } = $props();

  let open = $state(false);
  let rootEl: HTMLDivElement | undefined = $state();
  let menuEl: HTMLDivElement | undefined = $state();
  let triggerEl: HTMLButtonElement | undefined = $state();
  let fixedMenuStyle = $state('');

  const selectedOption = $derived(options.find((option) => option.key === value) ?? options[0] ?? null);

  $effect(() => {
    if (!open) return;

    function handlePointerDown(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (!rootEl?.contains(target) && !menuEl?.contains(target)) {
        open = false;
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') open = false;
    }

    tick().then(updateFixedMenuPosition);
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    window.addEventListener('resize', updateFixedMenuPosition);
    window.addEventListener('scroll', updateFixedMenuPosition, true);

    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
      window.removeEventListener('resize', updateFixedMenuPosition);
      window.removeEventListener('scroll', updateFixedMenuPosition, true);
    };
  });

  function updateFixedMenuPosition() {
    if (!triggerEl || typeof window === 'undefined') return;
    const rect = triggerEl.getBoundingClientRect();
    const viewportPad = 12;
    const menuMinWidth = Math.max(220, rect.width);
    const verticalGap = 8;
    const spaceBelow = window.innerHeight - rect.bottom - viewportPad;
    const spaceAbove = rect.top - viewportPad;
    const openBelow = spaceBelow >= 260 || spaceBelow >= spaceAbove;
    const availableHeight = Math.max(120, openBelow ? spaceBelow - verticalGap : spaceAbove - verticalGap);
    const maxHeight = Math.min(420, availableHeight);
    const top = openBelow
      ? rect.bottom + verticalGap
      : Math.max(viewportPad, rect.top - verticalGap - maxHeight);
    const left = Math.min(
      Math.max(viewportPad, rect.left),
      Math.max(viewportPad, window.innerWidth - viewportPad - menuMinWidth),
    );
    fixedMenuStyle = [
      `top: ${Math.round(top)}px`,
      `left: ${Math.round(left)}px`,
      `min-width: ${Math.round(menuMinWidth)}px`,
      `max-height: ${Math.round(maxHeight)}px`,
    ].join('; ');
  }

  function toggleOpen() {
    if (disabled || !selectedOption) return;
    open = !open;
  }

  function handleSelect(option: RuntimeOption) {
    if (option.disabled) return;
    onValueChange?.(option.key);
    open = false;
  }

  function handleTriggerKeydown(event: KeyboardEvent) {
    if (event.key !== 'ArrowDown' && event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    if (!disabled) open = true;
  }

  function portalMenu(node: HTMLElement) {
    document.body.appendChild(node);
    return {
      destroy() {
        node.remove();
      },
    };
  }
</script>

<div bind:this={rootEl} class="runtime-field">
  <span id={`${id}-label`}>{label}</span>
  <button
    id={id}
    type="button"
    bind:this={triggerEl}
    class="runtime-select-trigger"
    class:is-open={open}
    disabled={disabled || !selectedOption}
    aria-haspopup="menu"
    aria-expanded={open}
    aria-labelledby={`${id}-label`}
    onclick={toggleOpen}
    onkeydown={handleTriggerKeydown}
  >
    <span>{selectedOption?.label ?? 'Unset'}</span>
    <ConstellationIcon name="chevron-down" size={14} stroke={1.9} />
  </button>

  {#if open}
    <div
      bind:this={menuEl}
      use:portalMenu
      class="runtime-select-menu"
      style={fixedMenuStyle}
      role="menu"
      aria-label={label}
    >
      {#each options as option}
        {@const isActive = option.key === selectedOption?.key}
        <button
          type="button"
          role="menuitemradio"
          aria-checked={isActive}
          class="runtime-select-option"
          class:is-active={isActive}
          disabled={option.disabled}
          onclick={() => handleSelect(option)}
        >
          <span class="runtime-select-option-copy">
            <span class="runtime-select-option-label">{option.label}</span>
            {#if option.description}
              <span class="runtime-select-option-description">{option.description}</span>
            {/if}
          </span>
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .runtime-field {
    display: grid;
    gap: 7px;
    min-width: 0;
    color: var(--constellation-text-muted);
  }

  .runtime-field span {
    min-width: 0;
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .runtime-select-trigger {
    display: inline-flex;
    width: 100%;
    min-width: 0;
    height: 42px;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    box-sizing: border-box;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 12px;
    background: color-mix(in srgb, var(--constellation-control-input-background) 88%, transparent);
    color: var(--constellation-text-primary);
    font: inherit;
    font-size: 13px;
    padding: 0 12px 0 14px;
    text-align: left;
    cursor: pointer;
    transition:
      border-color var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-hover-duration) ease,
      color var(--constellation-motion-hover-duration) ease;
  }

  .runtime-select-trigger span {
    min-width: 0;
    overflow: hidden;
    color: inherit;
    font-family: inherit;
    font-size: inherit;
    font-weight: inherit;
    letter-spacing: 0;
    text-overflow: ellipsis;
    text-transform: none;
    white-space: nowrap;
  }

  .runtime-select-trigger:hover:not(:disabled),
  .runtime-select-trigger.is-open {
    border-color: var(--constellation-control-button-secondary-border);
    background: color-mix(in srgb, var(--constellation-control-button-secondary-background) 84%, var(--constellation-control-input-background));
  }

  .runtime-select-trigger:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .runtime-select-trigger:disabled {
    opacity: 0.56;
    cursor: not-allowed;
  }

  .runtime-select-trigger :global(svg) {
    flex: 0 0 auto;
    transition: transform var(--constellation-motion-hover-duration) ease;
  }

  .runtime-select-trigger.is-open :global(svg) {
    transform: rotate(180deg);
  }

  .runtime-select-menu {
    position: fixed;
    z-index: var(--constellation-layer-popover, 10000);
    max-width: calc(100vw - 24px);
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-width: none;
    padding: 8px;
    border: 1px solid var(--constellation-select-chip-menu-border);
    border-radius: 16px;
    background: var(--constellation-select-chip-menu-background);
    box-shadow: var(--constellation-select-chip-menu-shadow);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
  }

  .runtime-select-menu::-webkit-scrollbar {
    display: none;
  }

  .runtime-select-option {
    display: flex;
    width: 100%;
    min-width: 0;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
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

  .runtime-select-option:hover:not(:disabled) {
    background: var(--constellation-select-chip-option-hover-background);
    color: var(--constellation-select-chip-option-hover-text);
    transform: translateX(1px);
  }

  .runtime-select-option.is-active {
    background: var(--constellation-select-chip-option-active-background);
    color: var(--constellation-select-chip-option-active-text);
  }

  .runtime-select-option:disabled {
    opacity: 0.56;
    cursor: not-allowed;
  }

  .runtime-select-option-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .runtime-select-option-label {
    color: inherit;
    font-family: var(--constellation-font-sans);
    font-size: 13px;
    line-height: 1.25;
  }

  .runtime-select-option-description {
    color: var(--constellation-select-chip-option-description);
    font-family: var(--constellation-font-sans);
    font-size: 11px;
    line-height: 1.35;
  }

</style>
