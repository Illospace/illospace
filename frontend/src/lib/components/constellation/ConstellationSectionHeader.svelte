<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationSectionHeaderAlign = 'start' | 'center';
  export type ConstellationSectionHeaderSize = 'sm' | 'md';
  export type ConstellationSectionHeaderTitleTag = 'h1' | 'h2' | 'h3' | 'h4' | 'p';

  type Props = {
    title: string;
    description?: string;
    eyebrow?: string;
    align?: ConstellationSectionHeaderAlign;
    size?: ConstellationSectionHeaderSize;
    titleTag?: ConstellationSectionHeaderTitleTag;
    divider?: boolean;
    className?: string;
    meta?: Snippet;
    actions?: Snippet;
    children?: Snippet;
  };

  let {
    title,
    description = '',
    eyebrow = '',
    align = 'start',
    size = 'md',
    titleTag = 'h2',
    divider = false,
    className = '',
    meta,
    actions,
    children,
  }: Props = $props();

  const hasMeta = $derived(Boolean(meta));
  const rootClass = $derived(
    [
      'constellation-section-header',
      `constellation-section-header-${align}`,
      `constellation-section-header-${size}`,
      divider ? 'has-divider' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<header class={rootClass}>
  <div class="constellation-section-header-main">
    <div class="constellation-section-header-copy">
      {#if eyebrow || hasMeta}
        <div class="constellation-section-header-meta-row">
          {#if eyebrow}
            <p class="constellation-section-header-eyebrow">{eyebrow}</p>
          {/if}

          {#if meta}
            <div class="constellation-section-header-meta">
              {@render meta()}
            </div>
          {/if}
        </div>
      {/if}

      <svelte:element this={titleTag} class="constellation-section-header-title">
        {title}
      </svelte:element>

      {#if description}
        <p class="constellation-section-header-description">{description}</p>
      {/if}
    </div>

    {#if actions}
      <div class="constellation-section-header-actions">
        {@render actions()}
      </div>
    {/if}
  </div>

  {#if children}
    <div class="constellation-section-header-supporting">
      {@render children()}
    </div>
  {/if}
</header>

<style>
  .constellation-section-header {
    display: grid;
    gap: 12px;
    min-width: 0;
  }

  .constellation-section-header-main {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
  }

  .constellation-section-header-copy {
    display: grid;
    gap: 8px;
    min-width: 0;
    max-width: 760px;
  }

  .constellation-section-header-meta-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  .constellation-section-header-eyebrow {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-section-header-meta,
  .constellation-section-header-actions,
  .constellation-section-header-supporting {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }

  .constellation-section-header-actions {
    align-items: center;
    justify-content: flex-end;
    flex: 0 0 auto;
  }

  .constellation-section-header-title {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-weight: 560;
    line-height: 1.22;
    letter-spacing: 0;
    text-wrap: balance;
  }

  .constellation-section-header-description {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.55;
    max-width: 64ch;
  }

  .constellation-section-header-supporting {
    align-items: center;
  }

  .constellation-section-header-sm .constellation-section-header-title {
    font-size: 16px;
  }

  .constellation-section-header-md .constellation-section-header-title {
    font-size: clamp(18px, 1.7vw, 22px);
  }

  .constellation-section-header-start.has-divider {
    padding-bottom: 14px;
    border-bottom: 1px solid var(--constellation-section-divider);
  }

  .constellation-section-header-center {
    text-align: center;
  }

  .constellation-section-header-center .constellation-section-header-main,
  .constellation-section-header-center .constellation-section-header-meta-row {
    flex-direction: column;
    align-items: center;
    justify-content: center;
  }

  .constellation-section-header-center .constellation-section-header-copy {
    justify-items: center;
    margin-inline: auto;
  }

  .constellation-section-header-center .constellation-section-header-actions,
  .constellation-section-header-center .constellation-section-header-supporting {
    justify-content: center;
  }

  @media (max-width: 760px) {
    .constellation-section-header-main {
      flex-direction: column;
    }

    .constellation-section-header-actions {
      justify-content: flex-start;
    }
  }
</style>
