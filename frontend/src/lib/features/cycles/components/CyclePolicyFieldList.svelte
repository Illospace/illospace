<script lang="ts">
  import type { CyclePolicyJsonValue } from '$lib/api/client';
  import {
    policyFieldLabel,
    policyValueLabel,
  } from '$lib/features/cycles/domain/effectivePolicy';

  type PolicyFieldSource = {
    title?: string;
    label: string;
    changedAt: string | null;
    changedLabel: string;
  };

  type PolicyFieldEntry = {
    key: string;
    value: CyclePolicyJsonValue;
    source?: PolicyFieldSource;
  };

  let {
    entries,
    appearance,
    compact = false,
  }: {
    entries: readonly PolicyFieldEntry[];
    appearance: 'active' | 'snapshot';
    compact?: boolean;
  } = $props();
</script>

<dl class="policy-field-list" class:active={appearance === 'active'} class:snapshot={appearance === 'snapshot'} class:compact>
  {#each entries as entry (entry.key)}
    <div class="policy-field" class:prose-value={entry.key === 'prompt'}>
      <dt>{policyFieldLabel(entry.key)}</dt>
      <dd class:mono-value={entry.key.endsWith('_expr') || typeof entry.value === 'object'}>
        {policyValueLabel(entry.value)}
      </dd>
      {#if entry.source}
        <dd class="value-source" title={entry.source.title}>
          <span>{entry.source.label}</span>
          <time datetime={entry.source.changedAt || undefined}>{entry.source.changedLabel}</time>
        </dd>
      {/if}
    </div>
  {/each}
</dl>

<style>
  .policy-field-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0;
    border-top: calc(var(--sp-1) / 4) solid var(--constellation-section-divider);
  }

  .policy-field {
    display: grid;
    align-content: start;
    min-width: 0;
    border-bottom: calc(var(--sp-1) / 4) solid var(--constellation-section-divider);
  }

  .policy-field:nth-child(odd) {
    border-right: calc(var(--sp-1) / 4) solid var(--constellation-section-divider);
  }

  .policy-field.prose-value {
    grid-column: 1 / -1;
    border-right: 0;
  }

  dt {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    white-space: pre-wrap;
  }

  .mono-value {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .active .policy-field {
    gap: var(--sp-2);
    padding: var(--sp-3);
  }

  .active dd {
    font-size: 13px;
    line-height: 1.5;
  }

  .active .prose-value > dd:not(.value-source) {
    font-size: 14px;
  }

  .value-source {
    display: flex;
    flex-wrap: wrap;
    gap: var(--sp-1) var(--sp-3);
    color: var(--constellation-color-text-muted) !important;
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px !important;
    line-height: 1.4 !important;
  }

  .snapshot .policy-field {
    gap: var(--sp-2);
    padding: var(--sp-3);
  }

  .snapshot dd {
    font-size: 12px;
    line-height: 1.45;
  }

  .compact {
    grid-template-columns: 1fr;
  }

  .compact .policy-field,
  .compact .policy-field:nth-child(odd) {
    border-right: 0;
  }

  @media (max-width: 720px) {
    .policy-field-list {
      grid-template-columns: 1fr;
    }

    .policy-field,
    .policy-field:nth-child(odd) {
      border-right: 0;
    }
  }
</style>
