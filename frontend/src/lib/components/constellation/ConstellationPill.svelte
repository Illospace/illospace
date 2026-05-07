<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationPillVariant =
    | 'default'
    | 'active'
    | 'status'
    | 'complete'
    | 'success'
    | 'positive'
    | 'model'
    | 'thinking'
    | 'warning'
    | 'danger'
    | 'error'
    | 'info'
    | 'muted';

  let {
    variant = 'default',
    leadingDot = false,
    className = '',
    children,
  }: {
    variant?: ConstellationPillVariant;
    leadingDot?: boolean;
    className?: string;
    children?: Snippet;
  } = $props();

  const resolvedVariant = $derived.by(() => {
    if (variant === 'positive') return 'success';
    if (variant === 'error') return 'danger';
    return variant;
  });

  const rootClass = $derived(
    ['constellation-pill', `constellation-pill-${resolvedVariant}`, className]
      .filter(Boolean)
      .join(' '),
  );
</script>

<span class={rootClass}>
  {#if leadingDot}
    <span class="constellation-pill-dot" aria-hidden="true"></span>
  {/if}
  {@render children?.()}
</span>

<style>
  .constellation-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    padding: 5px 10px;
    border-radius: var(--constellation-radius-pill);
    border: 1px solid var(--constellation-control-pill-border);
    background: var(--constellation-control-pill-background);
    color: var(--constellation-control-pill-text);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    line-height: 1;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    white-space: nowrap;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .constellation-pill-dot {
    width: 6px;
    height: 6px;
    border-radius: var(--constellation-radius-pill);
    background: currentColor;
    box-shadow: 0 0 8px color-mix(in srgb, currentColor 38%, transparent);
  }

  .constellation-pill-active {
    background: var(--constellation-control-pill-active-background);
    color: var(--constellation-control-pill-active-text);
  }

  .constellation-pill-status {
    background: var(--constellation-control-pill-status-background);
    border-color: var(--constellation-control-pill-status-border);
    color: var(--constellation-control-pill-status-text);
  }

  .constellation-pill-complete {
    background: var(--constellation-control-pill-complete-background);
    color: var(--constellation-control-pill-complete-text);
  }

  .constellation-pill-success {
    background: var(--constellation-control-pill-success-background);
    border-color: var(--constellation-control-pill-success-border);
    color: var(--constellation-control-pill-success-text);
  }

  .constellation-pill-model {
    background: var(--constellation-control-pill-model-background);
    color: var(--constellation-control-pill-model-text);
  }

  .constellation-pill-thinking {
    background: var(--constellation-control-pill-thinking-background);
    color: var(--constellation-control-pill-thinking-text);
  }

  .constellation-pill-warning {
    background: var(--constellation-control-pill-warning-background);
    border-color: var(--constellation-control-pill-warning-border);
    color: var(--constellation-control-pill-warning-text);
  }

  .constellation-pill-danger {
    background: var(--constellation-control-pill-danger-background);
    border-color: var(--constellation-control-pill-danger-border);
    color: var(--constellation-control-pill-danger-text);
  }

  .constellation-pill-info {
    background: var(--constellation-control-pill-info-background);
    border-color: var(--constellation-control-pill-info-border);
    color: var(--constellation-control-pill-info-text);
  }

  .constellation-pill-muted {
    background: var(--constellation-control-pill-muted-background);
    border-color: var(--constellation-control-pill-muted-border);
    color: var(--constellation-control-pill-muted-text);
  }
</style>
