<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    label,
    className = '',
    title,
    disabled = false,
    type = 'button',
    variant = 'glass',
    onclick,
    children,
  }: {
    label: string;
    className?: string;
    title?: string;
    disabled?: boolean;
    type?: 'button' | 'submit' | 'reset';
    variant?: 'glass' | 'bare';
    onclick?: (event: MouseEvent) => void;
    children?: Snippet;
  } = $props();

  const rootClass = $derived(
    ['constellation-composer-orb', variant === 'bare' ? 'is-bare' : '', className]
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
  {disabled}
  {onclick}
>
  <span class="constellation-composer-orb-icon" aria-hidden="true">
    {@render children?.()}
  </span>
</button>

<style>
  .constellation-composer-orb {
    display: inline-flex;
    position: relative;
    align-items: center;
    justify-content: center;
    width: var(--constellation-composer-orb-size);
    height: var(--constellation-composer-orb-size);
    padding: 0;
    border-radius: var(--constellation-radius-pill);
    border: 1px solid var(--constellation-composer-orb-border);
    background: var(--constellation-composer-orb-background);
    color: var(--constellation-composer-orb-text);
    cursor: pointer;
    transition:
      transform var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-hover-duration) ease,
      border-color var(--constellation-motion-hover-duration) ease,
      color var(--constellation-motion-hover-duration) ease,
      box-shadow var(--constellation-motion-hover-duration) ease;
  }

  .constellation-composer-orb:hover:not(:disabled) {
    transform: translateY(-1px);
    background: var(--constellation-composer-orb-hover-background);
    color: var(--constellation-composer-orb-hover-text);
  }

  .constellation-composer-orb:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-composer-orb:active:not(:disabled) {
    transform: translateY(1px) scale(0.98);
  }

  .constellation-composer-orb:disabled {
    opacity: var(--constellation-composer-orb-disabled-opacity);
    cursor: not-allowed;
    box-shadow: none;
  }

  .constellation-composer-orb.is-bare {
    width: 30px;
    height: 30px;
    border-color: transparent;
    background: transparent;
    color: var(--constellation-composer-orb-bare-text);
  }

  .constellation-composer-orb.is-bare:hover:not(:disabled) {
    background: transparent;
    color: var(--constellation-composer-orb-hover-text);
    box-shadow: none;
  }

  .constellation-composer-orb.is-bare:active:not(:disabled) {
    transform: translateY(0) scale(0.96);
  }

  .constellation-composer-orb-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 15px;
    height: 15px;
  }

  .constellation-composer-orb-icon :global(svg) {
    width: 100%;
    height: 100%;
    display: block;
  }
</style>
