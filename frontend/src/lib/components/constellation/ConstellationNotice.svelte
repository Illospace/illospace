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
    --notice-accent: rgba(141, 183, 255, 0.24);
    --notice-accent-strong: rgba(141, 183, 255, 0.22);
    --notice-accent-soft: rgba(141, 183, 255, 0.12);
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
    background:
      linear-gradient(90deg, var(--notice-accent-soft), transparent 24%),
      var(--constellation-surface-panel-background);
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
    --notice-accent: rgba(255, 255, 255, 0.16);
    --notice-accent-strong: rgba(240, 240, 250, 0.2);
    --notice-accent-soft: rgba(255, 255, 255, 0.06);
  }

  .constellation-notice-tone-info {
    --notice-accent: rgba(141, 183, 255, 0.28);
    --notice-accent-strong: rgba(141, 183, 255, 0.24);
    --notice-accent-soft: rgba(141, 183, 255, 0.12);
  }

  .constellation-notice-tone-success {
    --notice-accent: rgba(109, 245, 189, 0.28);
    --notice-accent-strong: rgba(87, 207, 160, 0.24);
    --notice-accent-soft: rgba(87, 207, 160, 0.1);
  }

  .constellation-notice-tone-warning {
    --notice-accent: rgba(250, 231, 188, 0.26);
    --notice-accent-strong: rgba(213, 161, 77, 0.26);
    --notice-accent-soft: rgba(213, 161, 77, 0.12);
  }

  .constellation-notice-tone-danger {
    --notice-accent: rgba(219, 110, 130, 0.28);
    --notice-accent-strong: rgba(219, 110, 130, 0.24);
    --notice-accent-soft: rgba(219, 110, 130, 0.12);
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
