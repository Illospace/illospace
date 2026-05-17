<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationIconButtonVariant = 'secondary' | 'quiet';
  export type ConstellationIconButtonSize = 'sm' | 'md';

  let {
    label,
    variant = 'quiet',
    size = 'sm',
    className = '',
    title,
    disabled = false,
    pressed,
    expanded,
    popup,
    type = 'button',
    onclick,
    children,
  }: {
    label: string;
    variant?: ConstellationIconButtonVariant;
    size?: ConstellationIconButtonSize;
    className?: string;
    title?: string;
    disabled?: boolean;
    pressed?: boolean;
    expanded?: boolean;
    popup?: 'menu' | 'listbox' | 'tree' | 'grid' | 'dialog' | true;
    type?: 'button' | 'submit' | 'reset';
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
  } = $props();

  const rootClass = $derived(
    ['constellation-icon-button', `constellation-icon-button-${variant}`, `constellation-icon-button-${size}`, className]
      .filter(Boolean)
      .join(' '),
  );

  const resolvedTitle = $derived(title ?? label);
</script>

<button
  {type}
  class={rootClass}
  aria-label={label}
  title={resolvedTitle}
  aria-pressed={pressed}
  aria-expanded={expanded}
  aria-haspopup={popup}
  {disabled}
  {onclick}
>
  <span class="constellation-icon-button-icon" aria-hidden="true">
    {@render children?.()}
  </span>
</button>

<style>
  .constellation-icon-button {
    --icon-button-background: var(--constellation-icon-button-quiet-background);
    --icon-button-background-hover: var(--constellation-icon-button-quiet-background-hover);
    --icon-button-border: var(--constellation-icon-button-quiet-border, var(--constellation-icon-button-border));
    --icon-button-border-hover: var(--constellation-icon-button-quiet-border-hover);
    --icon-button-shadow: var(--constellation-icon-button-quiet-shadow);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0;
    border-radius: 8px;
    border: 1px solid var(--icon-button-border);
    background: var(--icon-button-background);
    color: var(--constellation-icon-button-text);
    box-shadow: var(--icon-button-shadow);
    cursor: pointer;
    transition:
      transform var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-settle-duration) ease,
      border-color var(--constellation-motion-settle-duration) ease,
      color var(--constellation-motion-settle-duration) ease,
      box-shadow var(--constellation-motion-settle-duration) ease;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .constellation-icon-button:hover:not(:disabled) {
    transform: translateY(-1px);
    border-color: var(--icon-button-border-hover);
    background: var(--icon-button-background-hover);
    color: var(--constellation-color-text-primary);
  }

  .constellation-icon-button:active:not(:disabled) {
    transform: translateY(1px) scale(0.985);
  }

  .constellation-icon-button:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-icon-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .constellation-icon-button[aria-pressed='true'] {
    color: var(--constellation-icon-button-pressed-text);
    border-color: var(--constellation-icon-button-pressed-border);
    background: var(--constellation-icon-button-pressed-background);
  }

  .constellation-icon-button-quiet {
    --icon-button-background: var(--constellation-icon-button-quiet-background);
    --icon-button-background-hover: var(--constellation-icon-button-quiet-background-hover);
    --icon-button-border: var(--constellation-icon-button-quiet-border, var(--constellation-icon-button-border));
    --icon-button-border-hover: var(--constellation-icon-button-quiet-border-hover);
    --icon-button-shadow: var(--constellation-icon-button-quiet-shadow);
  }

  .constellation-icon-button-secondary {
    --icon-button-background: var(--constellation-icon-button-secondary-background);
    --icon-button-background-hover: var(--constellation-icon-button-secondary-background-hover);
    --icon-button-border: var(--constellation-icon-button-secondary-border, var(--constellation-icon-button-border));
    --icon-button-border-hover: var(--constellation-icon-button-secondary-border-hover);
    --icon-button-shadow: var(--constellation-icon-button-secondary-shadow);
  }

  :global(:root[data-color-scheme='light']) .constellation-icon-button {
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .constellation-icon-button-sm {
    width: 28px;
    height: 28px;
  }

  .constellation-icon-button-md {
    width: 34px;
    height: 34px;
  }

  .constellation-icon-button-icon {
    display: inline-flex;
    width: 14px;
    height: 14px;
    align-items: center;
    justify-content: center;
  }

  .constellation-icon-button-icon :global(svg) {
    width: 100%;
    height: 100%;
  }
</style>
