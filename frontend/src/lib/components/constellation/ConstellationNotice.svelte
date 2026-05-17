<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationNoticeTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

  type Props = {
    title: string;
    description?: string;
    tone?: ConstellationNoticeTone;
    compact?: boolean;
    className?: string;
    style?: string;
    icon?: Snippet;
    actions?: Snippet;
    children?: Snippet;
  };

  let {
    title,
    description = '',
    tone = 'neutral',
    compact = false,
    className = '',
    style = '',
    icon,
    actions,
    children,
  }: Props = $props();

  const hasActions = $derived(Boolean(actions));
  const hasBody = $derived(Boolean(description || children));
  const rootClass = $derived(
    [
      'constellation-notice',
      `constellation-notice-tone-${tone}`,
      compact ? 'is-compact' : '',
      hasActions ? 'has-actions' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<article class={rootClass} {style}>
  <div class="constellation-notice-icon" aria-hidden="true">
    {#if icon}
      {@render icon()}
    {:else}
      <span class="constellation-notice-dot"></span>
    {/if}
  </div>

  <div class="constellation-notice-copy">
    <p class="constellation-notice-title">{title}</p>

    {#if hasBody}
      <div class="constellation-notice-body">
        {#if description}
          <p class="constellation-notice-description">{description}</p>
        {/if}

        {#if children}
          <div class="constellation-notice-supporting">
            {@render children()}
          </div>
        {/if}
      </div>
    {/if}
  </div>

  {#if actions}
    <div class="constellation-notice-actions">
      {@render actions()}
    </div>
  {/if}
</article>

<style>
  .constellation-notice {
    --notice-accent: var(--constellation-notice-accent, var(--constellation-tone-neutral-accent));
    --notice-accent-strong: var(--constellation-notice-accent-strong, var(--constellation-tone-neutral-accent-strong));
    --notice-accent-soft: var(--constellation-notice-accent-soft, var(--constellation-tone-neutral-accent-soft));
    --notice-text: var(--constellation-color-text-primary);
    --notice-body: var(--constellation-color-text-secondary);
    position: relative;
    isolation: isolate;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: flex-start;
    gap: 14px;
    padding: 16px 18px;
    border-radius: var(--constellation-radius-panel);
    border: 1px solid color-mix(in srgb, var(--notice-accent) 38%, var(--constellation-surface-panel-border));
    background: var(--constellation-surface-panel-background);
    box-shadow: var(--constellation-surface-panel-shadow);
    backdrop-filter: blur(14px) saturate(1.04);
    -webkit-backdrop-filter: blur(14px) saturate(1.04);
  }

  .constellation-notice-icon,
  .constellation-notice-copy,
  .constellation-notice-actions {
    position: relative;
    z-index: 1;
  }

  .constellation-notice-icon {
    display: inline-flex;
    width: 30px;
    height: 30px;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--notice-accent) 52%, var(--constellation-surface-nested-border));
    background: color-mix(in srgb, var(--notice-accent-soft) 72%, var(--constellation-surface-nested-background));
    color: var(--notice-text);
    box-shadow: var(--constellation-surface-nested-shadow);
  }

  .constellation-notice-icon :global(svg) {
    width: 14px;
    height: 14px;
  }

  .constellation-notice-dot {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--notice-accent-strong) 64%, white 36%);
    box-shadow: 0 0 12px var(--notice-accent-strong);
  }

  .constellation-notice-copy {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  .constellation-notice-title {
    margin: 0;
    color: var(--notice-text);
    font-family: var(--constellation-font-sans);
    font-size: 14px;
    font-weight: 560;
    line-height: 1.4;
    letter-spacing: 0;
  }

  .constellation-notice-body {
    display: grid;
    gap: 10px;
  }

  .constellation-notice-description {
    margin: 0;
    color: var(--notice-body);
    font-size: 12px;
    line-height: 1.55;
  }

  .constellation-notice-supporting,
  .constellation-notice-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }

  .constellation-notice-actions {
    align-items: center;
    justify-content: flex-end;
  }

  .constellation-notice.is-compact {
    padding: 14px 16px;
    gap: 12px;
  }

  .constellation-notice.is-compact .constellation-notice-icon {
    width: 26px;
    height: 26px;
  }

  .constellation-notice-tone-neutral {
    --notice-accent: var(--constellation-tone-neutral-accent);
    --notice-accent-strong: var(--constellation-tone-neutral-accent-strong);
    --notice-accent-soft: var(--constellation-tone-neutral-accent-soft);
  }

  .constellation-notice-tone-info {
    --notice-accent: var(--constellation-tone-info-accent);
    --notice-accent-strong: var(--constellation-tone-info-accent-strong);
    --notice-accent-soft: var(--constellation-tone-info-accent-soft);
  }

  .constellation-notice-tone-success {
    --notice-accent: var(--constellation-tone-success-accent);
    --notice-accent-strong: var(--constellation-tone-success-accent-strong);
    --notice-accent-soft: var(--constellation-tone-success-accent-soft);
  }

  .constellation-notice-tone-warning {
    --notice-accent: var(--constellation-control-pill-warning-border);
    --notice-accent-strong: var(--constellation-color-warning);
    --notice-accent-soft: var(--constellation-control-pill-warning-background);
  }

  .constellation-notice-tone-danger {
    --notice-accent: var(--constellation-tone-danger-accent);
    --notice-accent-strong: var(--constellation-tone-danger-accent-strong);
    --notice-accent-soft: var(--constellation-tone-danger-accent-soft);
  }

  @media (max-width: 720px) {
    .constellation-notice {
      grid-template-columns: auto minmax(0, 1fr);
    }

    .constellation-notice-actions {
      grid-column: 1 / -1;
      justify-content: flex-start;
      padding-left: 44px;
    }

    .constellation-notice.is-compact .constellation-notice-actions {
      padding-left: 38px;
    }
  }
</style>
