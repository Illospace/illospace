<script lang="ts">
  import type { Snippet } from 'svelte';
  import ConstellationGlyphIcon from './ConstellationGlyphIcon.svelte';
  import ConstellationPill from './ConstellationPill.svelte';
  import type { ConstellationPillVariant } from './ConstellationPill.svelte';

  export type ConstellationEntityListPill = {
    label: string;
    variant?: ConstellationPillVariant;
    className?: string;
    leadingDot?: boolean;
  };

  export type ConstellationEntityListRow = {
    id?: string;
    glyph?: string;
    marker?: Snippet;
    title: string;
    meta: string;
    summary: string;
    pills?: ConstellationEntityListPill[];
    actions?: Snippet;
  };

  let {
    rows = [],
    className = '',
  }: {
    rows: ConstellationEntityListRow[];
    className?: string;
  } = $props();

  const rootClass = $derived(['constellation-entity-list', className].filter(Boolean).join(' '));
</script>

<div class={rootClass}>
  {#each rows as row (row.id ?? `${row.title}-${row.meta}`)}
    <article class="constellation-entity-list-row">
      <span class="constellation-entity-list-glyph">
        {#if row.marker}
          {@render row.marker()}
        {:else if row.glyph}
          <ConstellationGlyphIcon label={row.glyph} />
        {/if}
      </span>

      <div class="constellation-entity-list-copy">
        <div class="constellation-entity-list-head">
          <span class="constellation-entity-list-title">{row.title}</span>
          <span class="constellation-entity-list-meta">{row.meta}</span>

          {#if row.pills?.length}
            {#each row.pills as pill (`${row.title}-${pill.label}`)}
              <ConstellationPill
                variant={pill.variant ?? 'default'}
                className={pill.className ?? ''}
                leadingDot={pill.leadingDot ?? false}
              >
                {pill.label}
              </ConstellationPill>
            {/each}
          {/if}
        </div>

        <p class="constellation-entity-list-summary">{row.summary}</p>
      </div>

      {#if row.actions}
        <div class="constellation-entity-list-actions">
          {@render row.actions()}
        </div>
      {/if}
    </article>
  {/each}
</div>

<style>
  .constellation-entity-list {
    display: grid;
    gap: 0;
  }

  .constellation-entity-list-row {
    display: grid;
    grid-template-columns: 22px minmax(0, 1fr) auto;
    gap: 14px;
    align-items: start;
    padding: 16px 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .constellation-entity-list-row:first-child {
    padding-top: 0;
    border-top: 0;
  }

  .constellation-entity-list-row:last-child {
    padding-bottom: 0;
  }

  .constellation-entity-list-glyph {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    min-height: 22px;
    color: var(--constellation-color-text-secondary);
  }

  .constellation-entity-list-copy {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  .constellation-entity-list-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .constellation-entity-list-title {
    color: var(--constellation-color-text-primary);
    font-size: 14px;
    font-weight: 600;
  }

  .constellation-entity-list-meta {
    color: var(--constellation-color-text-tertiary);
    font-family: var(--font-mono-direction);
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .constellation-entity-list-summary {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.55;
  }

  .constellation-entity-list-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
  }

  @media (max-width: 760px) {
    .constellation-entity-list-row {
      grid-template-columns: 1fr;
    }

    .constellation-entity-list-glyph {
      display: none;
    }

    .constellation-entity-list-actions {
      justify-content: flex-start;
    }
  }
</style>
