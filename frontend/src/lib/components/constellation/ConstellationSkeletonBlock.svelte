<script lang="ts">
  export type ConstellationSkeletonVariant = 'metric' | 'panel' | 'text';

  type Props = {
    variant?: ConstellationSkeletonVariant;
    width?: string;
    height?: string;
    lineCount?: number;
    animated?: boolean;
    className?: string;
    style?: string;
  };

  let {
    variant = 'panel',
    width = '100%',
    height,
    lineCount,
    animated = true,
    className = '',
    style = '',
  }: Props = $props();

  const resolvedHeight = $derived.by(() => {
    if (height) return height;
    if (variant === 'metric') return '132px';
    if (variant === 'panel') return '184px';
    return 'auto';
  });

  const resolvedLineCount = $derived.by(() => {
    if (lineCount != null) return Math.max(1, lineCount);
    if (variant === 'metric') return 2;
    if (variant === 'panel') return 4;
    return 3;
  });

  const lines = $derived(Array.from({ length: resolvedLineCount }, (_, index) => index));
  const rootClass = $derived(
    [
      'constellation-skeleton-block',
      `constellation-skeleton-block-${variant}`,
      animated ? 'is-animated' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
  const resolvedStyle = $derived(
    [`width: ${width}`, variant === 'text' ? '' : `min-height: ${resolvedHeight}`, style].filter(Boolean).join('; '),
  );

  function lineWidth(index: number) {
    const widths = variant === 'text' ? ['100%', '86%', '64%', '74%'] : ['100%', '90%', '78%', '68%', '56%'];
    return widths[index % widths.length];
  }
</script>

<div class={rootClass} style={resolvedStyle} aria-hidden="true">
  {#if variant === 'metric'}
    <span class="constellation-skeleton-chip"></span>
    <span class="constellation-skeleton-value"></span>
    <div class="constellation-skeleton-lines">
      {#each lines as line}
        <span class="constellation-skeleton-line" style={`width: ${lineWidth(line)}`}></span>
      {/each}
    </div>
  {:else if variant === 'panel'}
    <span class="constellation-skeleton-kicker"></span>
    <span class="constellation-skeleton-title"></span>
    <div class="constellation-skeleton-lines">
      {#each lines as line}
        <span class="constellation-skeleton-line" style={`width: ${lineWidth(line)}`}></span>
      {/each}
    </div>
  {:else}
    <div class="constellation-skeleton-lines">
      {#each lines as line}
        <span class="constellation-skeleton-line" style={`width: ${lineWidth(line)}`}></span>
      {/each}
    </div>
  {/if}
</div>

<style>
  .constellation-skeleton-block {
    --skeleton-fill: var(--constellation-skeleton-fill);
    --skeleton-fill-soft: var(--constellation-skeleton-fill-soft);
    --skeleton-shimmer: var(--constellation-skeleton-shimmer);
    position: relative;
    overflow: hidden;
    display: grid;
    gap: 12px;
    min-width: 0;
  }

  .constellation-skeleton-block.is-animated::after {
    content: '';
    position: absolute;
    inset: 0;
    background: linear-gradient(110deg, transparent 20%, var(--skeleton-shimmer) 48%, transparent 76%);
    transform: translateX(-100%);
    animation: constellation-skeleton-shimmer 1.7s linear infinite;
  }

  .constellation-skeleton-block-panel,
  .constellation-skeleton-block-metric {
    padding: 18px;
    border-radius: var(--constellation-radius-panel);
    border: 1px solid var(--constellation-skeleton-panel-border);
    background: var(--constellation-skeleton-panel-background);
    box-shadow: var(--constellation-skeleton-panel-shadow);
  }

  .constellation-skeleton-block-text {
    gap: 10px;
  }

  .constellation-skeleton-kicker,
  .constellation-skeleton-title,
  .constellation-skeleton-chip,
  .constellation-skeleton-value,
  .constellation-skeleton-line {
    position: relative;
    z-index: 1;
    display: block;
    border-radius: 999px;
    background: linear-gradient(90deg, var(--skeleton-fill), var(--skeleton-fill-soft));
  }

  .constellation-skeleton-kicker {
    width: 84px;
    height: 10px;
  }

  .constellation-skeleton-title {
    width: 52%;
    height: 18px;
  }

  .constellation-skeleton-chip {
    width: 78px;
    height: 10px;
  }

  .constellation-skeleton-value {
    width: 44%;
    height: 34px;
    border-radius: 14px;
  }

  .constellation-skeleton-lines {
    display: grid;
    gap: 10px;
  }

  .constellation-skeleton-line {
    height: 10px;
  }

  .constellation-skeleton-block-text .constellation-skeleton-line {
    height: 9px;
  }

  .constellation-skeleton-block-metric .constellation-skeleton-lines {
    gap: 8px;
    align-self: end;
  }

  @keyframes constellation-skeleton-shimmer {
    to {
      transform: translateX(100%);
    }
  }
</style>
