<script lang="ts">
  import { tick } from 'svelte';
  import ConstellationIcon from './ConstellationIcon.svelte';

  export type ConstellationSelectOption = {
    value: string;
    label: string;
    description?: string;
    disabled?: boolean;
  };

  type Props = {
    id?: string;
    label?: string;
    labelHidden?: boolean;
    options: ReadonlyArray<ConstellationSelectOption>;
    value?: string;
    placeholder?: string;
    disabled?: boolean;
    className?: string;
    ariaLabel?: string;
    ariaDescribedby?: string;
    ariaInvalid?: boolean | 'true' | 'false' | 'grammar' | 'spelling';
    size?: 'sm' | 'md';
    onValueChange?: (value: string) => void;
  };

  let {
    id,
    label,
    labelHidden = false,
    options,
    value = $bindable(''),
    placeholder = 'Select...',
    disabled = false,
    className = '',
    ariaLabel,
    ariaDescribedby,
    ariaInvalid,
    size = 'sm',
    onValueChange,
  }: Props = $props();

  let open = $state(false);
  let rootEl: HTMLDivElement | undefined = $state();
  let menuEl: HTMLDivElement | undefined = $state();
  let triggerEl: HTMLButtonElement | undefined = $state();
  let fixedMenuStyle = $state('');

  const selectedOption = $derived(options.find((option) => option.value === value) ?? null);
  const triggerLabel = $derived(selectedOption?.label ?? placeholder);
  const rootClass = $derived(
    [
      'constellation-select',
      `is-${size}`,
      open ? 'is-open' : '',
      labelHidden ? 'has-hidden-label' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
  const labelId = $derived(id ? `${id}-label` : undefined);
  const accessibleLabel = $derived(ariaLabel ?? label ?? triggerLabel);

  $effect(() => {
    if (!open) return;

    function handlePointerDown(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (!rootEl?.contains(target) && !menuEl?.contains(target)) open = false;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        open = false;
        triggerEl?.focus();
      }
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
    if (disabled || options.length === 0) return;
    if (open) {
      open = false;
      triggerEl?.focus();
      return;
    }
    openMenu();
  }

  function enabledOptionIndexes(): number[] {
    return options.flatMap((option, index) => option.disabled ? [] : [index]);
  }

  function initialOptionIndex(): number {
    const selectedIndex = options.findIndex((option) => option.value === value && !option.disabled);
    return selectedIndex >= 0 ? selectedIndex : (enabledOptionIndexes()[0] ?? -1);
  }

  function optionElement(index: number): HTMLButtonElement | null {
    return menuEl?.querySelector<HTMLButtonElement>(`[data-option-index="${index}"]`) ?? null;
  }

  function focusOption(index: number): void {
    optionElement(index)?.focus();
  }

  function openMenu(): void {
    open = true;
    const index = initialOptionIndex();
    tick().then(() => {
      updateFixedMenuPosition();
      if (index >= 0) focusOption(index);
    });
  }

  function moveOptionFocus(currentIndex: number, direction: -1 | 1): void {
    const indexes = enabledOptionIndexes();
    if (!indexes.length) return;
    const position = indexes.indexOf(currentIndex);
    const nextPosition = position < 0
      ? 0
      : (position + direction + indexes.length) % indexes.length;
    focusOption(indexes[nextPosition]);
  }

  function focusOutsideMenu(direction: -1 | 1): void {
    if (!triggerEl) return;
    const focusableSelector = [
      'a[href]',
      'button:not([disabled])',
      'input:not([disabled])',
      'select:not([disabled])',
      'textarea:not([disabled])',
      '[tabindex]:not([tabindex="-1"])',
    ].join(',');
    const focusable = Array.from(document.querySelectorAll<HTMLElement>(focusableSelector))
      .filter((element) => !menuEl?.contains(element) && element.getClientRects().length > 0);
    const triggerIndex = focusable.indexOf(triggerEl);
    const next = focusable[triggerIndex + direction];
    open = false;
    tick().then(() => (next ?? triggerEl).focus());
  }

  function selectValue(nextValue: string) {
    const option = options.find((item) => item.value === nextValue);
    if (!option || option.disabled) return;

    value = option.value;
    onValueChange?.(option.value);
    open = false;
    triggerEl?.focus();
  }

  function handleTriggerKeydown(event: KeyboardEvent) {
    if (disabled || options.length === 0) return;

    if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown') {
      event.preventDefault();
      openMenu();
      return;
    }

    if (event.key === 'ArrowUp') {
      event.preventDefault();
      openMenu();
    }
  }

  function handleOptionKeydown(event: KeyboardEvent, index: number): void {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveOptionFocus(index, event.key === 'ArrowDown' ? 1 : -1);
      return;
    }

    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      const indexes = enabledOptionIndexes();
      const nextIndex = event.key === 'Home' ? indexes[0] : indexes[indexes.length - 1];
      if (nextIndex !== undefined) focusOption(nextIndex);
      return;
    }

    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      open = false;
      triggerEl?.focus();
      return;
    }

    if (event.key === 'Tab') {
      event.preventDefault();
      focusOutsideMenu(event.shiftKey ? -1 : 1);
    }
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

<div bind:this={rootEl} class={rootClass}>
  {#if label}
    <span id={labelId} class="constellation-select-label" class:is-hidden={labelHidden}>{label}</span>
  {/if}

  <button
    id={id}
    type="button"
    role="combobox"
    bind:this={triggerEl}
    class="constellation-select-trigger"
    aria-label={label ? undefined : accessibleLabel}
    aria-labelledby={label ? labelId : undefined}
    aria-describedby={ariaDescribedby}
    aria-invalid={ariaInvalid}
    aria-expanded={open}
    aria-haspopup="listbox"
    aria-controls={id ? `${id}-menu` : undefined}
    {disabled}
    onclick={toggleOpen}
    onkeydown={handleTriggerKeydown}
  >
    <span class="constellation-select-trigger-label" class:is-placeholder={!selectedOption}>{triggerLabel}</span>
    <ConstellationIcon name="chevron-down" size={14} stroke={1.9} className="constellation-select-chevron" />
  </button>

  {#if open}
    <div
      id={id ? `${id}-menu` : undefined}
      bind:this={menuEl}
      use:portalMenu
      class="constellation-select-menu"
      style={fixedMenuStyle}
      role="listbox"
      aria-label={accessibleLabel}
    >
      {#each options as option, index (option.value)}
        {@const isActive = option.value === value}
        <button
          type="button"
          role="option"
          aria-selected={isActive}
          tabindex="-1"
          data-option-index={index}
          class={`constellation-select-option ${isActive ? 'is-active' : ''}`}
          disabled={option.disabled}
          onclick={() => selectValue(option.value)}
          onkeydown={(event) => handleOptionKeydown(event, index)}
        >
          <span class="constellation-select-option-text">
            <span class="constellation-select-option-label">{option.label}</span>
            {#if option.description}
              <span class="constellation-select-option-description">{option.description}</span>
            {/if}
          </span>
          <span class="constellation-select-option-indicator" aria-hidden="true"></span>
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .constellation-select {
    display: grid;
    gap: 7px;
    min-width: 0;
  }

  .constellation-select.has-hidden-label {
    gap: 0;
  }

  .constellation-select-label {
    min-width: 0;
    color: var(--constellation-text-muted);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .constellation-select-label.is-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    clip-path: inset(50%);
  }

  .constellation-select-trigger {
    appearance: none;
    -webkit-appearance: none;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 8px;
    min-width: 0;
    width: 100%;
    border: 1px solid var(--constellation-control-input-border);
    background: color-mix(in srgb, var(--constellation-control-input-background) 88%, transparent);
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 13px;
    line-height: 1.2;
    letter-spacing: 0;
    text-align: left;
    cursor: pointer;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    transition:
      border-color var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-hover-duration) ease,
      color var(--constellation-motion-hover-duration) ease,
      box-shadow var(--constellation-motion-hover-duration) ease;
  }

  .constellation-select.is-sm .constellation-select-trigger {
    min-height: 32px;
    padding: 0 10px;
    border-radius: 8px;
  }

  .constellation-select.is-md .constellation-select-trigger {
    min-height: 42px;
    padding: 0 12px 0 14px;
    border-radius: 12px;
  }

  .constellation-select-trigger:hover:not(:disabled),
  .constellation-select.is-open .constellation-select-trigger {
    border-color: var(--constellation-control-button-secondary-border);
    background: color-mix(in srgb, var(--constellation-control-button-secondary-background) 84%, var(--constellation-control-input-background));
  }

  .constellation-select-trigger:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-select-trigger:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .constellation-select-trigger-label {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .constellation-select-trigger-label.is-placeholder,
  .constellation-select-chevron {
    color: var(--constellation-control-field-placeholder);
  }

  .constellation-select.is-open :global(.constellation-select-chevron) {
    transform: rotate(180deg);
  }

  .constellation-select-menu {
    position: fixed;
    z-index: var(--constellation-layer-popover, 10000);
    display: grid;
    gap: 3px;
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

  .constellation-select-menu::-webkit-scrollbar {
    display: none;
  }

  .constellation-select-option {
    appearance: none;
    -webkit-appearance: none;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 8px;
    min-width: 0;
    padding: 9px 10px;
    border: 0;
    border-radius: 12px;
    background: transparent;
    color: var(--constellation-select-chip-option-text);
    font-family: var(--constellation-font-sans);
    font-size: 13px;
    line-height: 1.25;
    letter-spacing: 0;
    text-align: left;
    cursor: pointer;
    transition:
      background-color var(--constellation-motion-hover-duration) ease,
      color var(--constellation-motion-hover-duration) ease,
      transform var(--constellation-motion-hover-duration) ease;
  }

  .constellation-select-option:hover:not(:disabled) {
    background: var(--constellation-select-chip-option-hover-background);
    color: var(--constellation-select-chip-option-hover-text);
    transform: translateX(1px);
  }

  .constellation-select-option.is-active {
    background: var(--constellation-select-chip-option-active-background);
    color: var(--constellation-select-chip-option-active-text);
  }

  .constellation-select-option:disabled {
    cursor: not-allowed;
    opacity: 0.5;
  }

  .constellation-select-option-text {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .constellation-select-option-label,
  .constellation-select-option-description {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .constellation-select-option-description {
    color: var(--constellation-select-chip-option-description);
    font-size: 12px;
  }

  .constellation-select-option-indicator {
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: transparent;
  }

  .constellation-select-option.is-active .constellation-select-option-indicator {
    background: currentColor;
  }
</style>
