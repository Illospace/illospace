<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationEmptyStateAlign = 'start' | 'center';
  export type ConstellationEmptyStateSize = 'sm' | 'md';
  export type ConstellationEmptyStateSurface = 'framed' | 'plain';
  export type ConstellationEmptyStateTitleTag = 'h2' | 'h3' | 'h4' | 'p';

  type Props = {
    title: string;
    description?: string;
    eyebrow?: string;
    align?: ConstellationEmptyStateAlign;
    size?: ConstellationEmptyStateSize;
    surface?: ConstellationEmptyStateSurface;
    titleTag?: ConstellationEmptyStateTitleTag;
    className?: string;
    style?: string;
    icon?: Snippet;
    actions?: Snippet;
    children?: Snippet;
  };

  let {
    title,
    description = '',
    eyebrow = '',
    align = 'center',
    size = 'md',
    surface = 'framed',
    titleTag = 'h3',
    className = '',
    style = '',
    icon,
    actions,
    children,
  }: Props = $props();

  const hasActions = $derived(Boolean(actions));
  const rootClass = $derived(
    [
      'constellation-empty-state',
      `constellation-empty-state-${align}`,
      `constellation-empty-state-${size}`,
      `constellation-empty-state-${surface}`,
      hasActions ? 'has-actions' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<section class={rootClass} {style}>
  <div class="constellation-empty-state-mark" aria-hidden="true">
    {#if icon}
      {@render icon()}
    {:else}
      <span class="constellation-empty-state-orbit"></span>
      <span class="constellation-empty-state-core"></span>
    {/if}
  </div>

  <div class="constellation-empty-state-copy">
    {#if eyebrow}
      <p class="constellation-empty-state-eyebrow">{eyebrow}</p>
    {/if}

    <svelte:element this={titleTag} class="constellation-empty-state-title">
      {title}
    </svelte:element>

    {#if description}
      <p class="constellation-empty-state-description">{description}</p>
    {/if}
  </div>

  {#if children}
    <div class="constellation-empty-state-body">
      {@render children()}
    </div>
  {/if}

  {#if actions}
    <div class="constellation-empty-state-actions">
      {@render actions()}
    </div>
  {/if}
</section>

<style>
  .constellation-empty-state {
    --empty-align-items: center;
    --empty-text-align: center;
    --empty-padding: 28px;
    display: grid;
    justify-items: var(--empty-align-items);
    gap: 16px;
    min-height: 220px;
    padding: var(--empty-padding);
    border-radius: var(--constellation-radius-panel);
    border: 1px solid var(--constellation-surface-panel-border);
    background:
      radial-gradient(circle at 50% 0%, rgba(141, 183, 255, 0.1), transparent 34%),
      radial-gradient(circle at 74% 72%, color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 8%, transparent), transparent 28%),
      var(--constellation-surface-panel-background);
    text-align: var(--empty-text-align);
    box-shadow: var(--constellation-surface-nested-shadow);
  }

  .constellation-empty-state-plain {
    min-height: 0;
    padding-inline: 0;
    border-color: transparent;
    background: transparent;
    box-shadow: none;
  }

  .constellation-empty-state-mark {
    position: relative;
    display: grid;
    place-items: center;
    width: 58px;
    height: 58px;
    border-radius: 999px;
    border: 1px solid var(--constellation-surface-nested-border);
    background: var(--constellation-surface-nested-background);
    color: var(--constellation-color-text-primary);
    box-shadow:
      0 14px 28px rgba(0, 0, 0, 0.16),
      var(--constellation-surface-nested-shadow);
    overflow: hidden;
  }

  .constellation-empty-state-mark :global(svg) {
    width: 18px;
    height: 18px;
  }

  .constellation-empty-state-orbit,
  .constellation-empty-state-core {
    position: absolute;
    border-radius: 999px;
  }

  .constellation-empty-state-orbit {
    inset: 11px;
    border: 1px solid rgba(141, 183, 255, 0.3);
    box-shadow: 0 0 18px rgba(141, 183, 255, 0.16);
    transform: rotate(-18deg);
  }

  .constellation-empty-state-core {
    width: 10px;
    height: 10px;
    background: radial-gradient(circle, rgba(240, 240, 250, 0.98), rgba(141, 183, 255, 0.74));
    box-shadow: 0 0 14px rgba(141, 183, 255, 0.34);
  }

  .constellation-empty-state-copy {
    display: grid;
    gap: 8px;
    max-width: 520px;
  }

  .constellation-empty-state-eyebrow {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-empty-state-title {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-weight: 560;
    line-height: 1.28;
  }

  .constellation-empty-state-description {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.6;
  }

  .constellation-empty-state-body,
  .constellation-empty-state-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: center;
  }

  .constellation-empty-state-sm {
    --empty-padding: 22px;
    min-height: 180px;
  }

  .constellation-empty-state-sm .constellation-empty-state-title {
    font-size: 16px;
  }

  .constellation-empty-state-md .constellation-empty-state-title {
    font-size: clamp(18px, 1.8vw, 22px);
  }

  .constellation-empty-state-start {
    --empty-align-items: start;
    --empty-text-align: left;
  }

  .constellation-empty-state-start .constellation-empty-state-body,
  .constellation-empty-state-start .constellation-empty-state-actions {
    justify-content: flex-start;
  }

  @media (max-width: 640px) {
    .constellation-empty-state {
      min-height: 0;
    }
  }
</style>
