<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationActionRowAs = 'article' | 'div' | 'li' | 'section';
  export type ConstellationActionRowTone = 'default' | 'info' | 'success' | 'warning' | 'danger';
  export type ConstellationActionRowDensity = 'comfortable' | 'compact';
  export type ConstellationActionRowTitleTag = 'h2' | 'h3' | 'h4' | 'p';
  export type ConstellationActionRowContent =
    | Snippet
    | string
    | number
    | boolean
    | null
    | undefined;

  type Props = {
    as?: ConstellationActionRowAs;
    title: string;
    description?: string;
    eyebrow?: string;
    tone?: ConstellationActionRowTone;
    density?: ConstellationActionRowDensity;
    dense?: boolean;
    interactive?: boolean;
    titleTag?: ConstellationActionRowTitleTag;
    className?: string;
    style?: string;
    ariaLabel?: string;
    leading?: Snippet;
    media?: Snippet;
    badge?: Snippet;
    meta?: ConstellationActionRowContent;
    actions?: Snippet;
    supporting?: Snippet;
    children?: Snippet;
  };

  let {
    as = 'article',
    title,
    description = '',
    eyebrow = '',
    tone = 'default',
    density = 'comfortable',
    dense = false,
    interactive = false,
    titleTag = 'h3',
    className = '',
    style = '',
    ariaLabel,
    leading,
    media,
    badge,
    meta,
    actions,
    supporting,
    children,
  }: Props = $props();

  function isSnippet(value: ConstellationActionRowContent): value is Snippet {
    return typeof value === 'function';
  }

  const resolvedDensity = $derived(dense ? 'compact' : density);
  const resolvedLeading = $derived(leading ?? media);
  const hasSupporting = $derived(Boolean(supporting || children));
  const rootClass = $derived(
    [
      'constellation-action-row',
      `constellation-action-row-tone-${tone}`,
      `constellation-action-row-${resolvedDensity}`,
      interactive ? 'is-interactive' : '',
      hasSupporting ? 'has-supporting' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<svelte:element this={as} class={rootClass} {style} aria-label={ariaLabel}>
  {#if resolvedLeading}
    <div class="constellation-action-row-leading" aria-hidden="true">
      {@render resolvedLeading()}
    </div>
  {/if}

  <div class="constellation-action-row-main">
    {#if eyebrow || meta != null}
      <div class="constellation-action-row-meta-row">
        {#if eyebrow}
          <p class="constellation-action-row-eyebrow">{eyebrow}</p>
        {/if}

        {#if meta != null}
          <div class="constellation-action-row-meta">
            {#if isSnippet(meta)}
              {@render meta()}
            {:else}
              {meta}
            {/if}
          </div>
        {/if}
      </div>
    {/if}

    <div class="constellation-action-row-title-row">
      <svelte:element this={titleTag} class="constellation-action-row-title">
        {title}
      </svelte:element>

      {#if badge}
        <div class="constellation-action-row-badge">
          {@render badge()}
        </div>
      {/if}
    </div>

    {#if description}
      <p class="constellation-action-row-description">{description}</p>
    {/if}

    {#if supporting}
      <div class="constellation-action-row-supporting">
        {@render supporting()}
      </div>
    {/if}

    {#if children}
      <div class="constellation-action-row-body">
        {@render children()}
      </div>
    {/if}
  </div>

  {#if actions}
    <div class="constellation-action-row-actions">
      {@render actions()}
    </div>
  {/if}
</svelte:element>

<style>
  .constellation-action-row {
    --action-row-accent: var(--constellation-action-row-accent, rgba(141, 183, 255, 0.18));
    --action-row-accent-soft: var(--constellation-action-row-accent-soft, rgba(141, 183, 255, 0.08));
    --action-row-padding-block: 16px;
    --action-row-padding-inline: 18px;
    position: relative;
    isolation: isolate;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: 14px;
    min-width: 0;
    overflow: hidden;
    padding: var(--action-row-padding-block) var(--action-row-padding-inline);
    border-radius: calc(var(--constellation-radius-panel) - 4px);
    border: 1px solid color-mix(in srgb, var(--action-row-accent) 42%, var(--constellation-surface-panel-border));
    background:
      radial-gradient(138% 176% at -24% 50%, var(--action-row-accent-soft), transparent 56%),
      var(--constellation-surface-panel-background);
    box-shadow: var(--constellation-surface-panel-shadow);
    backdrop-filter: blur(14px) saturate(1.03);
    -webkit-backdrop-filter: blur(14px) saturate(1.03);
    transition:
      transform var(--constellation-motion-hover-duration) var(--constellation-motion-ease-lift),
      border-color var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      box-shadow var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-action-row.is-interactive:hover {
    transform: translateY(-1px);
    border-color: color-mix(in srgb, var(--action-row-accent) 58%, var(--constellation-surface-panel-hover-border));
    box-shadow: var(--constellation-surface-panel-hover-shadow);
  }

  .constellation-action-row-leading,
  .constellation-action-row-main,
  .constellation-action-row-actions {
    position: relative;
    z-index: 1;
    min-width: 0;
  }

  .constellation-action-row-leading {
    display: inline-flex;
    width: 38px;
    height: 38px;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--action-row-accent) 52%, var(--constellation-surface-nested-border));
    background: color-mix(in srgb, var(--action-row-accent-soft) 78%, var(--constellation-surface-nested-background));
    color: var(--constellation-color-text-primary);
    box-shadow: var(--constellation-surface-nested-shadow);
    overflow: hidden;
  }

  .constellation-action-row-leading :global(img) {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .constellation-action-row-leading :global(svg) {
    width: 16px;
    height: 16px;
  }

  .constellation-action-row-main {
    display: grid;
    gap: 8px;
  }

  .constellation-action-row-meta-row,
  .constellation-action-row-title-row,
  .constellation-action-row-supporting,
  .constellation-action-row-actions,
  .constellation-action-row-body {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    min-width: 0;
  }

  .constellation-action-row-meta-row {
    align-items: center;
    justify-content: space-between;
  }

  .constellation-action-row-eyebrow {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-action-row-meta {
    color: var(--constellation-label-meta);
    font-size: 12px;
    line-height: 1.45;
  }

  .constellation-action-row-title-row {
    align-items: center;
  }

  .constellation-action-row-title {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 15px;
    font-weight: 560;
    line-height: 1.34;
    letter-spacing: 0;
    text-wrap: balance;
  }

  .constellation-action-row-badge {
    display: inline-flex;
    align-items: center;
  }

  .constellation-action-row-description {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.58;
  }

  .constellation-action-row-supporting,
  .constellation-action-row-body {
    align-items: center;
  }

  .constellation-action-row-actions {
    align-items: center;
    justify-content: flex-end;
    align-self: center;
  }

  .constellation-action-row-compact {
    --action-row-padding-block: 14px;
    --action-row-padding-inline: 16px;
  }

  .constellation-action-row-compact .constellation-action-row-leading {
    width: 34px;
    height: 34px;
  }

  .constellation-action-row-tone-default {
    --action-row-accent: var(--constellation-action-row-accent, rgba(141, 183, 255, 0.18));
    --action-row-accent-soft: var(--constellation-action-row-accent-soft, rgba(141, 183, 255, 0.08));
  }

  .constellation-action-row-tone-info {
    --action-row-accent: rgba(141, 183, 255, 0.22);
    --action-row-accent-soft: rgba(141, 183, 255, 0.1);
  }

  .constellation-action-row-tone-success {
    --action-row-accent: rgba(109, 245, 189, 0.22);
    --action-row-accent-soft: rgba(87, 207, 160, 0.1);
  }

  .constellation-action-row-tone-warning {
    --action-row-accent: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 22%, transparent);
    --action-row-accent-soft: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 12%, transparent);
  }

  .constellation-action-row-tone-danger {
    --action-row-accent: rgba(219, 110, 130, 0.24);
    --action-row-accent-soft: rgba(219, 110, 130, 0.12);
  }

  @media (max-width: 760px) {
    .constellation-action-row {
      grid-template-columns: auto minmax(0, 1fr);
    }

    .constellation-action-row-actions {
      grid-column: 1 / -1;
      justify-content: flex-start;
      padding-left: calc(38px + 14px);
    }

    .constellation-action-row-compact .constellation-action-row-actions {
      padding-left: calc(34px + 14px);
    }

    .constellation-action-row-meta-row {
      flex-direction: column;
      align-items: flex-start;
      justify-content: flex-start;
    }
  }

  @media (max-width: 560px) {
    .constellation-action-row {
      grid-template-columns: 1fr;
    }

    .constellation-action-row-actions {
      padding-left: 0;
    }
  }
</style>
