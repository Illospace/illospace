<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationMetricTileTone = 'default' | 'info' | 'success' | 'warning' | 'danger';
  export type ConstellationMetricTileTrend = 'up' | 'down' | 'flat';
  export type ConstellationMetricTileSize = 'sm' | 'md';

  type Props = {
    label: string;
    value: string | number;
    detail?: string;
    delta?: string;
    trend?: ConstellationMetricTileTrend;
    tone?: ConstellationMetricTileTone;
    size?: ConstellationMetricTileSize;
    className?: string;
    style?: string;
    icon?: Snippet;
    badge?: Snippet;
    children?: Snippet;
  };

  let {
    label,
    value,
    detail = '',
    delta = '',
    trend = 'flat',
    tone = 'default',
    size = 'md',
    className = '',
    style = '',
    icon,
    badge,
    children,
  }: Props = $props();

  const hasFooter = $derived(Boolean(detail || delta || children));
  const rootClass = $derived(
    [
      'constellation-metric-tile',
      `constellation-metric-tile-tone-${tone}`,
      `constellation-metric-tile-${size}`,
      delta ? `constellation-metric-tile-trend-${trend}` : '',
      hasFooter ? 'has-footer' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<article class={rootClass} {style}>
  <div class="constellation-metric-tile-head">
    <div class="constellation-metric-tile-label-group">
      <span class="constellation-metric-tile-label">{label}</span>

      {#if badge}
        <div class="constellation-metric-tile-badge">
          {@render badge()}
        </div>
      {/if}
    </div>

    {#if icon}
      <div class="constellation-metric-tile-icon" aria-hidden="true">
        {@render icon()}
      </div>
    {/if}
  </div>

  <div class="constellation-metric-tile-value-wrap">
    <strong class="constellation-metric-tile-value">{value}</strong>
  </div>

  {#if hasFooter}
    <div class="constellation-metric-tile-footer">
      {#if detail}
        <p class="constellation-metric-tile-detail">{detail}</p>
      {/if}

      {#if delta}
        <span class="constellation-metric-tile-delta">{delta}</span>
      {/if}

      {#if children}
        <div class="constellation-metric-tile-supporting">
          {@render children()}
        </div>
      {/if}
    </div>
  {/if}
</article>

<style>
  .constellation-metric-tile {
    --metric-accent: rgba(141, 183, 255, 0.26);
    --metric-accent-soft: rgba(141, 183, 255, 0.1);
    --metric-value-color: var(--constellation-section-title);
    --metric-detail-color: var(--constellation-color-text-secondary);
    --metric-delta-background: var(--constellation-surface-nested-strong-background);
    --metric-delta-border: var(--constellation-surface-nested-border);
    --metric-delta-color: var(--constellation-color-text-secondary);
    position: relative;
    isolation: isolate;
    display: grid;
    gap: 16px;
    min-width: 0;
    padding: 18px;
    border-radius: var(--constellation-radius-panel);
    border: 1px solid color-mix(in srgb, var(--metric-accent) 34%, var(--constellation-surface-panel-border));
    background:
      radial-gradient(circle at 14% 0%, var(--metric-accent-soft), transparent 38%),
      var(--constellation-surface-panel-background);
    box-shadow: var(--constellation-surface-panel-shadow);
    overflow: hidden;
  }

  .constellation-metric-tile::before {
    content: '';
    position: absolute;
    inset: 0;
    background: var(--constellation-surface-panel-highlight);
    pointer-events: none;
  }

  .constellation-metric-tile-head,
  .constellation-metric-tile-value-wrap,
  .constellation-metric-tile-footer {
    position: relative;
    z-index: 1;
  }

  .constellation-metric-tile-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .constellation-metric-tile-label-group,
  .constellation-metric-tile-badge,
  .constellation-metric-tile-supporting {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .constellation-metric-tile-label {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-metric-tile-icon {
    display: inline-flex;
    width: 32px;
    height: 32px;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--metric-accent) 52%, var(--constellation-surface-nested-border));
    background: color-mix(in srgb, var(--metric-accent-soft) 76%, var(--constellation-surface-nested-background));
    color: var(--metric-value-color);
    box-shadow: var(--constellation-surface-nested-shadow);
  }

  .constellation-metric-tile-icon :global(svg) {
    width: 14px;
    height: 14px;
  }

  .constellation-metric-tile-value-wrap {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 0;
  }

  .constellation-metric-tile-value {
    min-width: 0;
    color: var(--metric-value-color);
    font-family: var(--constellation-font-sans);
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0;
    text-wrap: balance;
  }

  .constellation-metric-tile-footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 12px;
  }

  .constellation-metric-tile-detail {
    margin: 0;
    color: var(--metric-detail-color);
    font-size: 12px;
    line-height: 1.5;
  }

  .constellation-metric-tile-delta {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 24px;
    padding: 0 9px;
    border-radius: 999px;
    border: 1px solid var(--metric-delta-border);
    background: var(--metric-delta-background);
    color: var(--metric-delta-color);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .constellation-metric-tile-sm {
    gap: 12px;
    padding: 16px;
  }

  .constellation-metric-tile-sm .constellation-metric-tile-value {
    font-size: clamp(24px, 2.6vw, 30px);
  }

  .constellation-metric-tile-md .constellation-metric-tile-value {
    font-size: clamp(30px, 3vw, 38px);
  }

  .constellation-metric-tile-tone-default {
    --metric-accent: rgba(141, 183, 255, 0.26);
    --metric-accent-soft: rgba(141, 183, 255, 0.1);
  }

  .constellation-metric-tile-tone-info {
    --metric-accent: rgba(141, 183, 255, 0.3);
    --metric-accent-soft: rgba(141, 183, 255, 0.12);
  }

  .constellation-metric-tile-tone-success {
    --metric-accent: rgba(109, 245, 189, 0.28);
    --metric-accent-soft: rgba(87, 207, 160, 0.1);
  }

  .constellation-metric-tile-tone-warning {
    --metric-accent: rgba(250, 231, 188, 0.28);
    --metric-accent-soft: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 12%, transparent);
  }

  .constellation-metric-tile-tone-danger {
    --metric-accent: rgba(219, 110, 130, 0.3);
    --metric-accent-soft: rgba(219, 110, 130, 0.12);
  }

  .constellation-metric-tile-trend-up {
    --metric-delta-background: rgba(87, 207, 160, 0.14);
    --metric-delta-border: rgba(87, 207, 160, 0.22);
    --metric-delta-color: rgba(223, 255, 243, 0.94);
  }

  .constellation-metric-tile-trend-down {
    --metric-delta-background: rgba(219, 110, 130, 0.14);
    --metric-delta-border: rgba(219, 110, 130, 0.22);
    --metric-delta-color: rgba(255, 225, 232, 0.94);
  }

  .constellation-metric-tile-trend-flat {
    --metric-delta-background: var(--constellation-surface-nested-strong-background);
    --metric-delta-border: var(--constellation-surface-nested-border);
    --metric-delta-color: var(--constellation-color-text-secondary);
  }
</style>
