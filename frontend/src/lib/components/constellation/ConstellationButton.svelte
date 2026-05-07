<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLButtonAttributes } from 'svelte/elements';

  export type ConstellationButtonVariant =
    | 'primary'
    | 'secondary'
    | 'quiet'
    | 'destructive'
    | 'danger';
  export type ConstellationButtonSize = 'sm' | 'md';

  type Props = Omit<HTMLButtonAttributes, 'children' | 'class'> & {
    className?: string;
    variant?: ConstellationButtonVariant;
    size?: ConstellationButtonSize;
    leadingVisual?: Snippet;
    trailingVisual?: Snippet;
    loading?: boolean;
    loadingLabel?: string;
    fullWidth?: boolean;
    pressed?: boolean;
    children?: Snippet;
  };

  let {
    className = '',
    variant = 'primary',
    size = 'md',
    leadingVisual,
    trailingVisual,
    loading = false,
    loadingLabel,
    fullWidth = false,
    pressed,
    children,
    type = 'button',
    disabled = false,
    ...rest
  }: Props = $props();

  const resolvedVariant = $derived(variant === 'danger' ? 'destructive' : variant);

  const rootClass = $derived(
    [
      'constellation-button',
      `constellation-button-${resolvedVariant}`,
      `constellation-button-${size}`,
      loading ? 'is-loading' : '',
      fullWidth ? 'is-full-width' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );

  const resolvedDisabled = $derived(disabled || loading);
</script>

<button
  {type}
  class={rootClass}
  disabled={resolvedDisabled}
  aria-busy={loading}
  aria-pressed={pressed}
  {...rest}
>
  {#if loading}
    <span class="constellation-button-spinner" aria-hidden="true"></span>
  {:else if leadingVisual}
    <span class="constellation-button-icon" aria-hidden="true">
      {@render leadingVisual()}
    </span>
  {/if}

  <span class="constellation-button-label">
    {#if loading && loadingLabel}
      {loadingLabel}
    {:else}
      {@render children?.()}
    {/if}
  </span>

  {#if !loading && trailingVisual}
    <span class="constellation-button-icon" aria-hidden="true">
      {@render trailingVisual()}
    </span>
  {/if}
</button>

<style>
  .constellation-button {
    --button-background: var(--constellation-button-primary-background);
    --button-background-hover: var(--constellation-button-primary-background-hover);
    --button-border: var(--constellation-button-primary-border);
    --button-border-hover: var(--constellation-button-primary-border-hover);
    --button-text: var(--constellation-button-primary-text);
    --button-shadow: var(--constellation-button-primary-shadow);
    display: inline-flex;
    position: relative;
    align-items: center;
    justify-content: center;
    gap: 8px;
    overflow: hidden;
    border-radius: var(--constellation-radius-pill);
    border: 1px solid var(--button-border);
    background: var(--button-background);
    color: var(--button-text);
    box-shadow: var(--button-shadow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    white-space: nowrap;
    cursor: pointer;
    transition:
      transform var(--constellation-motion-hover-duration) ease,
      box-shadow var(--constellation-motion-settle-duration) ease,
      background-color var(--constellation-motion-settle-duration) ease,
      border-color var(--constellation-motion-settle-duration) ease,
      color var(--constellation-motion-settle-duration) ease,
      opacity var(--constellation-motion-settle-duration) ease;
  }

  .constellation-button::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.18), transparent 62%);
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--constellation-motion-hover-duration) ease;
  }

  .constellation-button:hover:not(:disabled) {
    transform: translateY(-1px);
    background: var(--button-background-hover);
    border-color: var(--button-border-hover);
  }

  .constellation-button:hover:not(:disabled)::after {
    opacity: 1;
  }

  .constellation-button:active:not(:disabled) {
    transform: translateY(1px) scale(0.985);
  }

  .constellation-button:active:not(:disabled)::after {
    opacity: 0.42;
  }

  .constellation-button:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    box-shadow: none;
  }

  .constellation-button[aria-pressed='true'] {
    background: var(--constellation-button-pressed-background);
    border-color: var(--constellation-button-pressed-border);
    color: var(--constellation-button-pressed-text);
    box-shadow: var(--constellation-button-pressed-shadow);
  }

  .constellation-button-primary {
    --button-background: var(--constellation-button-primary-background);
    --button-background-hover: var(--constellation-button-primary-background-hover);
    --button-border: var(--constellation-button-primary-border);
    --button-border-hover: var(--constellation-button-primary-border-hover);
    --button-text: var(--constellation-button-primary-text);
    --button-shadow: var(--constellation-button-primary-shadow);
  }

  .constellation-button-secondary {
    --button-background: var(--constellation-button-secondary-background);
    --button-background-hover: var(--constellation-button-secondary-background-hover);
    --button-border: var(--constellation-control-button-secondary-border);
    --button-border-hover: var(--constellation-button-secondary-border-hover);
    --button-text: var(--constellation-control-button-secondary-text);
    --button-shadow: var(--constellation-button-secondary-shadow);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .constellation-button-quiet {
    --button-background: var(--constellation-button-quiet-background);
    --button-background-hover: var(--constellation-button-quiet-background-hover);
    --button-border: var(--constellation-button-quiet-border);
    --button-border-hover: var(--constellation-button-quiet-border-hover);
    --button-text: var(--constellation-button-quiet-text);
    --button-shadow: var(--constellation-button-quiet-shadow);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .constellation-button-destructive {
    --button-background: var(--constellation-button-destructive-background);
    --button-background-hover: var(--constellation-button-destructive-background-hover);
    --button-border: var(--constellation-button-destructive-border);
    --button-border-hover: var(--constellation-button-destructive-border-hover);
    --button-text: var(--constellation-button-destructive-text);
    --button-shadow: var(--constellation-button-destructive-shadow);
  }

  .constellation-button-destructive[aria-pressed='true'] {
    background: var(--constellation-button-destructive-pressed-background);
    border-color: var(--constellation-button-destructive-pressed-border);
    color: var(--constellation-button-destructive-pressed-text);
    box-shadow: var(--constellation-button-destructive-pressed-shadow);
  }

  .constellation-button-sm {
    min-height: 32px;
    padding: 0 12px;
  }

  .constellation-button-md {
    min-height: 40px;
    padding: 0 18px;
  }

  .constellation-button.is-loading {
    cursor: progress;
  }

  .constellation-button.is-full-width {
    width: 100%;
  }

  .constellation-button-label,
  .constellation-button-icon,
  .constellation-button-spinner {
    position: relative;
    z-index: 1;
  }

  .constellation-button-label {
    display: inline-flex;
    align-items: center;
  }

  .constellation-button-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 14px;
    height: 14px;
    flex: 0 0 auto;
  }

  .constellation-button-icon :global(svg) {
    width: 100%;
    height: 100%;
  }

  .constellation-button-spinner {
    width: 12px;
    height: 12px;
    border-radius: 999px;
    border: 1.5px solid currentColor;
    border-right-color: transparent;
    animation: constellation-button-spin 0.7s linear infinite;
  }

  @keyframes constellation-button-spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
