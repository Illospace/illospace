<script lang="ts">
  import type { Snippet } from 'svelte';

  export type ConstellationKeyValueTone = 'default' | 'info' | 'success' | 'warning' | 'danger';
  export type ConstellationKeyValueSize = 'sm' | 'md';
  export type ConstellationKeyValueLayout = 'stack' | 'grid';
  export type ConstellationKeyValueEmphasis = 'default' | 'strong';
  export type ConstellationKeyValueContent =
    | Snippet
    | string
    | number
    | boolean
    | null
    | undefined;

  export type ConstellationKeyValueItem = {
    id?: string | number;
    label: string;
    value: ConstellationKeyValueContent;
    description?: ConstellationKeyValueContent;
    tone?: ConstellationKeyValueTone;
    mono?: boolean;
    meta?: ConstellationKeyValueContent;
    actions?: Snippet;
    emphasis?: ConstellationKeyValueEmphasis;
    className?: string;
  };

  type Props = {
    items?: ReadonlyArray<ConstellationKeyValueItem>;
    compact?: boolean;
    size?: ConstellationKeyValueSize;
    layout?: ConstellationKeyValueLayout;
    columns?: 1 | 2 | 3;
    className?: string;
    style?: string;
  };

  let {
    items = [],
    compact = false,
    size = 'md',
    layout = 'stack',
    columns = 1,
    className = '',
    style = '',
  }: Props = $props();

  function isSnippet(value: ConstellationKeyValueContent): value is Snippet {
    return typeof value === 'function';
  }

  const resolvedSize = $derived(compact ? 'sm' : size);
  const rootClass = $derived(
    [
      'constellation-key-value-list',
      `constellation-key-value-list-${layout}`,
      `constellation-key-value-list-${resolvedSize}`,
      `constellation-key-value-list-columns-${columns}`,
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<dl class={rootClass} {style}>
  {#each items as item, index (item.id ?? `${item.label}-${index + 1}`)}
    <div
      class={`constellation-key-value-row constellation-key-value-tone-${item.tone ?? 'default'} constellation-key-value-emphasis-${item.emphasis ?? 'default'} ${item.className ?? ''}`.trim()}
    >
      <div class="constellation-key-value-label-wrap">
        <dt class="constellation-key-value-label">{item.label}</dt>

        {#if item.meta != null}
          <div class="constellation-key-value-meta">
            {#if isSnippet(item.meta)}
              {@render item.meta()}
            {:else}
              {item.meta}
            {/if}
          </div>
        {/if}
      </div>

      <div class="constellation-key-value-value-wrap">
        <dd class={`constellation-key-value-value ${item.mono ? 'is-mono' : ''}`.trim()}>
          {#if isSnippet(item.value)}
            {@render item.value()}
          {:else if item.value != null}
            {item.value}
          {:else}
            <span class="constellation-key-value-empty">--</span>
          {/if}
        </dd>

        {#if item.description != null}
          <div class="constellation-key-value-description">
            {#if isSnippet(item.description)}
              {@render item.description()}
            {:else}
              {item.description}
            {/if}
          </div>
        {/if}

        {#if item.actions}
          <div class="constellation-key-value-actions">
            {@render item.actions()}
          </div>
        {/if}
      </div>
    </div>
  {/each}
</dl>

<style>
  .constellation-key-value-list {
    display: grid;
    gap: 0;
    min-width: 0;
  }

  .constellation-key-value-list-grid {
    gap: 12px;
  }

  .constellation-key-value-list-columns-2.constellation-key-value-list-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .constellation-key-value-list-columns-3.constellation-key-value-list-grid {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .constellation-key-value-row {
    --key-value-accent: rgba(141, 183, 255, 0.08);
    position: relative;
    min-width: 0;
  }

  .constellation-key-value-list-stack .constellation-key-value-row {
    display: grid;
    grid-template-columns: minmax(0, 0.86fr) minmax(0, 1.14fr);
    gap: 18px;
    align-items: start;
    padding: 14px 0;
    border-bottom: 1px solid var(--constellation-section-divider);
  }

  .constellation-key-value-list-stack .constellation-key-value-row:first-child {
    padding-top: 0;
  }

  .constellation-key-value-list-stack .constellation-key-value-row:last-child {
    padding-bottom: 0;
    border-bottom: 0;
  }

  .constellation-key-value-list-grid .constellation-key-value-row {
    display: grid;
    gap: 12px;
    padding: 16px;
    border-radius: calc(var(--constellation-radius-panel) - 4px);
    border: 1px solid color-mix(in srgb, var(--key-value-accent) 72%, var(--constellation-surface-panel-border));
    background:
      var(--constellation-surface-panel-background),
      linear-gradient(90deg, var(--key-value-accent), transparent 48%);
    box-shadow:
      0 12px 28px rgba(0, 0, 0, 0.14),
      var(--constellation-surface-nested-shadow);
  }

  .constellation-key-value-label-wrap {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
  }

  .constellation-key-value-label {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    line-height: 1.4;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-key-value-meta,
  .constellation-key-value-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
  }

  .constellation-key-value-meta {
    justify-content: flex-end;
    color: var(--constellation-label-meta);
    font-size: 11px;
    line-height: 1.45;
  }

  .constellation-key-value-value-wrap {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .constellation-key-value-value {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 14px;
    font-weight: 520;
    line-height: 1.45;
    letter-spacing: 0;
    text-wrap: balance;
  }

  .constellation-key-value-value.is-mono {
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .constellation-key-value-description {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.55;
  }

  .constellation-key-value-empty {
    color: var(--constellation-color-text-muted);
  }

  .constellation-key-value-emphasis-strong .constellation-key-value-value {
    font-size: clamp(15px, 1.4vw, 18px);
    font-weight: 560;
    line-height: 1.34;
  }

  .constellation-key-value-list-sm .constellation-key-value-row {
    gap: 12px;
  }

  .constellation-key-value-list-sm .constellation-key-value-value {
    font-size: 13px;
  }

  .constellation-key-value-list-sm .constellation-key-value-emphasis-strong .constellation-key-value-value {
    font-size: 15px;
  }

  .constellation-key-value-tone-default {
    --key-value-accent: rgba(141, 183, 255, 0.08);
  }

  .constellation-key-value-tone-info {
    --key-value-accent: rgba(141, 183, 255, 0.12);
  }

  .constellation-key-value-tone-success {
    --key-value-accent: rgba(109, 245, 189, 0.12);
  }

  .constellation-key-value-tone-warning {
    --key-value-accent: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 14%, transparent);
  }

  .constellation-key-value-tone-danger {
    --key-value-accent: rgba(219, 110, 130, 0.14);
  }

  .constellation-key-value-tone-info .constellation-key-value-value {
    color: rgba(204, 226, 255, 0.94);
  }

  .constellation-key-value-tone-success .constellation-key-value-value {
    color: rgba(184, 248, 219, 0.94);
  }

  .constellation-key-value-tone-warning .constellation-key-value-value {
    color: rgba(255, 230, 184, 0.94);
  }

  .constellation-key-value-tone-danger .constellation-key-value-value {
    color: rgba(255, 186, 198, 0.94);
  }

  @media (max-width: 880px) {
    .constellation-key-value-list-columns-2.constellation-key-value-list-grid,
    .constellation-key-value-list-columns-3.constellation-key-value-list-grid {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 720px) {
    .constellation-key-value-list-stack .constellation-key-value-row {
      grid-template-columns: 1fr;
      gap: 8px;
    }

    .constellation-key-value-label-wrap {
      flex-direction: column;
      align-items: flex-start;
    }

    .constellation-key-value-meta {
      justify-content: flex-start;
    }
  }
</style>
