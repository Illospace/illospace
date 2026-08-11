<script lang="ts">
  import type { EffectiveCyclePolicyRead } from '$lib/api/client';
  import { ConstellationButton, ConstellationPill } from '$lib/components/constellation';
  import CyclePolicyFieldList from '$lib/features/cycles/components/CyclePolicyFieldList.svelte';
  import {
    formatPolicyDateTime,
    policyConfigurationEntries,
    policyFieldLabel,
    policyFieldSource,
    policySourceLabel,
    policyValueLabel,
  } from '$lib/features/cycles/domain/effectivePolicy';

  let {
    cycleId,
    policy,
    displayTimezone,
    showDetails,
    showHeader = true,
    canEdit,
    compact = false,
    onEdit,
  }: {
    cycleId: number;
    policy: EffectiveCyclePolicyRead | null;
    displayTimezone: string;
    showDetails: boolean;
    showHeader?: boolean;
    canEdit: boolean;
    compact?: boolean;
    onEdit: () => void;
  } = $props();

  const configurationEntries = $derived(policy
    ? policyConfigurationEntries(policy.configuration).map((entry) => {
        const source = policyFieldSource(policy.field_sources, entry.key);
        const changedAt = source?.changed_at || policy.source.changed_at;
        return {
          ...entry,
          source: {
            title: source?.rationale || undefined,
            label: `From ${source ? policySourceLabel(source) : policySourceLabel(policy.source)}`,
            changedAt,
            changedLabel: `Changed ${formatPolicyDateTime(changedAt, displayTimezone)}`,
          },
        };
      })
    : []);
  const guidanceSource = $derived(
    policy ? policyFieldSource(policy.field_sources, 'guidance') : undefined,
  );
</script>

{#if showHeader}
  <header class="active-heading">
    <div>
      <span class="section-kicker">Effective behavior</span>
      <h3>Active now</h3>
      <p>What Illo will use on the next run.</p>
    </div>
    {#if policy}
      <div class="active-actions">
        <div class="active-status">
          <ConstellationPill variant={policy.configuration.enabled ? 'success' : 'muted'} leadingDot>
            {policy.configuration.enabled ? 'Enabled' : 'Paused'}
          </ConstellationPill>
          <span>Version {policy.version}</span>
        </div>
        {#if canEdit}
          <ConstellationButton variant="secondary" size="sm" onclick={onEdit}>
            Edit behavior
          </ConstellationButton>
        {/if}
      </div>
    {/if}
  </header>
{/if}

{#if policy && showDetails}
  <div class:compact class="policy-content">
    <section class="policy-section" aria-labelledby={`cycle-${cycleId}-configuration`}>
      <div class="policy-section-heading">
        <h4 id={`cycle-${cycleId}-configuration`}>Mission and settings</h4>
        <span>{displayTimezone.replaceAll('_', ' ')}</span>
      </div>

      <CyclePolicyFieldList entries={configurationEntries} appearance="active" {compact} />
    </section>

    <section class="policy-section active-guidance" aria-labelledby={`cycle-${cycleId}-guidance`}>
      <div class="policy-section-heading">
        <div>
          <span class="live-marker">Live</span>
          <h4 id={`cycle-${cycleId}-guidance`}>Active guidance</h4>
        </div>
        <span>{policy.guidance.length} active</span>
      </div>

      {#if policy.guidance.length}
        <ol class="guidance-list">
          {#each policy.guidance as guidance}
            <li>
              <p>{guidance}</p>
              <div class="value-source" title={guidanceSource?.rationale || undefined}>
                <span>From {guidanceSource ? policySourceLabel(guidanceSource) : policySourceLabel(policy.source)}</span>
                <time datetime={guidanceSource?.changed_at || policy.source.changed_at || undefined}>
                  Changed {formatPolicyDateTime(guidanceSource?.changed_at || policy.source.changed_at, displayTimezone)}
                </time>
              </div>
            </li>
          {/each}
        </ol>
      {:else}
        <p class="empty-policy-value">No active guidance.</p>
      {/if}
    </section>

    <section class="policy-section" aria-labelledby={`cycle-${cycleId}-outputs`}>
      <div class="policy-section-heading">
        <h4 id={`cycle-${cycleId}-outputs`}>Output targets</h4>
        <ConstellationPill variant="muted">Read only</ConstellationPill>
      </div>

      {#if policy.output_targets.length}
        <div class="output-list">
          {#each policy.output_targets as target (target.id)}
            <article class="output-target">
              <div class="output-target-heading">
                <strong>{target.label || policyFieldLabel(target.target_type)}</strong>
                <span>{target.target_type}</span>
              </div>
              {#if target.target_id}<p>Target {target.target_id}</p>{/if}
              {#if Object.keys(target.config).length}<pre>{policyValueLabel(target.config)}</pre>{/if}
              {#if target.rationale}<p>{target.rationale}</p>{/if}
              <div class="value-source">
                <span>From {[target.source_type, target.source_id].filter(Boolean).join(' · ')}</span>
                <time datetime={target.updated_at}>
                  Changed {formatPolicyDateTime(target.updated_at, displayTimezone)}
                </time>
              </div>
            </article>
          {/each}
        </div>
      {:else}
        <p class="empty-policy-value">No output targets.</p>
      {/if}
    </section>
  </div>
{/if}

<style>
  .active-heading,
  .policy-section-heading,
  .output-target-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .active-heading {
    padding: 14px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-color-success) 7%, transparent);
  }

  .active-heading h3,
  .active-heading p,
  .policy-section-heading h4,
  .guidance-list p,
  .output-target p {
    margin: 0;
  }

  .active-heading h3 {
    margin-top: 3px;
    font-size: 16px;
    font-weight: 650;
    letter-spacing: -0.01em;
  }

  .active-heading p {
    margin-top: 5px;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .section-kicker,
  .live-marker {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.12em;
    line-height: 1.2;
    text-transform: uppercase;
  }

  .active-status {
    display: grid;
    justify-items: end;
    gap: 6px;
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
  }

  .active-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .policy-content {
    display: grid;
  }

  .policy-section {
    display: grid;
    gap: 12px;
    min-width: 0;
    padding: 14px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .policy-section-heading {
    align-items: center;
  }

  .policy-section-heading > div {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .policy-section-heading h4 {
    font-size: 13px;
    font-weight: 600;
  }

  .policy-section-heading > span {
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
  }

  .live-marker {
    padding: 3px 6px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--constellation-color-success) 14%, transparent);
    color: var(--constellation-color-success);
  }

  .output-target pre {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .value-source {
    display: flex;
    flex-wrap: wrap;
    gap: 3px 10px;
    color: var(--constellation-color-text-muted) !important;
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px !important;
    line-height: 1.4 !important;
  }

  .active-guidance {
    border-left: 3px solid var(--constellation-color-success);
    background: color-mix(in srgb, var(--constellation-color-success) 3%, transparent);
  }

  .guidance-list,
  .output-list {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .guidance-list {
    counter-reset: active-guidance;
  }

  .guidance-list li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 6px 10px;
    padding: 10px 0;
    border-bottom: 1px solid var(--constellation-section-divider);
    counter-increment: active-guidance;
  }

  .guidance-list li::before {
    content: counter(active-guidance, decimal-leading-zero);
    color: var(--constellation-color-success);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    line-height: 1.7;
  }

  .guidance-list li:last-child {
    border-bottom: 0;
  }

  .guidance-list .value-source {
    grid-column: 2;
  }

  .guidance-list p {
    font-size: 13px;
    line-height: 1.55;
  }

  .output-target {
    display: grid;
    gap: 7px;
    padding: 11px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 7px;
    background: var(--constellation-surface-nested-background);
  }

  .output-target-heading strong {
    font-size: 13px;
    font-weight: 600;
  }

  .output-target-heading span,
  .output-target p,
  .output-target pre {
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.45;
  }

  .output-target-heading span {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .output-target pre {
    max-width: 100%;
    margin: 0;
    overflow: auto;
    white-space: pre-wrap;
  }

  .empty-policy-value {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  @media (max-width: 720px) {
    .active-heading,
    .active-actions {
      align-items: flex-start;
      flex-direction: column;
    }

    .active-status {
      justify-items: start;
    }
  }
</style>
