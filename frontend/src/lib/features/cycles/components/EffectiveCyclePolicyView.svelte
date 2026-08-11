<script lang="ts">
  import { onDestroy } from 'svelte';

  import {
    api,
    type CyclePolicyChangeRead,
    type CyclePolicyHistoryRead,
    type EffectiveCyclePolicyRead,
  } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationPill,
  } from '$lib/components/constellation';
  import {
    formatPolicyDateTime,
    policyConfigurationEntries,
    policyFieldLabel,
    policyFieldSource,
    policySourceLabel,
    policyValueLabel,
    retiredGuidance,
  } from '$lib/features/cycles/domain/effectivePolicy';

  let {
    cycleId,
    previewPolicy = null,
    previewHistory = null,
    displayTimezone = null,
    compact = false,
    refreshSerial = 0,
  }: {
    cycleId: number;
    previewPolicy?: EffectiveCyclePolicyRead | null;
    previewHistory?: CyclePolicyHistoryRead | null;
    displayTimezone?: string | null;
    compact?: boolean;
    refreshSerial?: number;
  } = $props();

  let policy = $state<EffectiveCyclePolicyRead | null>(null);
  let history = $state<CyclePolicyChangeRead[]>([]);
  let pagination = $state<CyclePolicyHistoryRead['pagination'] | null>(null);
  let loading = $state(true);
  let historyLoading = $state(false);
  let error = $state('');
  let historyError = $state('');
  let resolvedTimezone = $state(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  );
  let requestSerial = 0;

  const configurationEntries = $derived(
    policy ? policyConfigurationEntries(policy.configuration) : [],
  );
  const guidanceSource = $derived(
    policy ? policyFieldSource(policy.field_sources, 'guidance') : undefined,
  );

  function errorMessage(value: unknown, fallback: string): string {
    if (value && typeof value === 'object' && 'detail' in value) {
      return String((value as { detail?: unknown }).detail || fallback);
    }
    return fallback;
  }

  async function loadPolicy(selectedCycleId: number) {
    const serial = ++requestSerial;
    loading = true;
    error = '';
    historyError = '';
    policy = null;
    history = [];
    pagination = null;
    try {
      const [nextPolicy, nextHistory, runtime] = await Promise.all([
        api.getCycleBehaviorPolicy(selectedCycleId),
        api.getCycleBehaviorPolicyHistory(selectedCycleId),
        displayTimezone
          ? Promise.resolve(null)
          : api.runtimeSettings().catch(() => null),
      ]);
      if (serial !== requestSerial) return;
      policy = nextPolicy;
      history = nextHistory.items;
      pagination = nextHistory.pagination;
      resolvedTimezone = displayTimezone
        || runtime?.display?.display_timezone
        || Intl.DateTimeFormat().resolvedOptions().timeZone
        || 'UTC';
    } catch (loadError) {
      if (serial !== requestSerial) return;
      error = errorMessage(loadError, 'Effective behavior failed to load.');
    } finally {
      if (serial === requestSerial) loading = false;
    }
  }

  async function loadMoreHistory() {
    if (!pagination?.has_more || pagination.next_offset === null || historyLoading) return;
    const selectedCycleId = cycleId;
    const serial = requestSerial;
    historyLoading = true;
    historyError = '';
    try {
      const nextHistory = await api.getCycleBehaviorPolicyHistory(
        selectedCycleId,
        pagination.limit,
        pagination.next_offset,
      );
      if (serial !== requestSerial || selectedCycleId !== cycleId) return;
      history = [...history, ...nextHistory.items];
      pagination = nextHistory.pagination;
    } catch (loadError) {
      if (serial !== requestSerial) return;
      historyError = errorMessage(loadError, 'Older history failed to load.');
    } finally {
      if (serial === requestSerial) historyLoading = false;
    }
  }

  $effect(() => {
    const selectedCycleId = cycleId;
    refreshSerial;
    const suppliedPolicy = previewPolicy;
    const suppliedHistory = previewHistory;
    if (displayTimezone) resolvedTimezone = displayTimezone;
    if (suppliedPolicy) {
      requestSerial += 1;
      policy = suppliedPolicy;
      history = suppliedHistory?.items ?? [];
      pagination = suppliedHistory?.pagination ?? null;
      loading = false;
      error = '';
      historyError = '';
      return;
    }
    void loadPolicy(selectedCycleId);
  });

  onDestroy(() => {
    requestSerial += 1;
  });
</script>

<section class="effective-policy" class:compact aria-label="Effective cycle behavior">
  <header class="active-heading">
    <div>
      <span class="section-kicker">Effective behavior</span>
      <h3>Active now</h3>
      <p>What Illo will use on the next run.</p>
    </div>
    {#if policy}
      <div class="active-status">
        <ConstellationPill variant={policy.configuration.enabled ? 'success' : 'muted'} leadingDot>
          {policy.configuration.enabled ? 'Enabled' : 'Paused'}
        </ConstellationPill>
        <span>Version {policy.version}</span>
      </div>
    {/if}
  </header>

  {#if loading}
    <div class="policy-loading" aria-label="Loading effective behavior">
      <span></span>
      <span></span>
      <span></span>
    </div>
  {:else if error}
    <div class="policy-error" role="alert">
      <span>{error}</span>
      <ConstellationButton variant="secondary" size="sm" onclick={() => loadPolicy(cycleId)}>
        Retry
      </ConstellationButton>
    </div>
  {:else if policy}
    <div class="policy-content">
      <section class="policy-section" aria-labelledby={`cycle-${cycleId}-configuration`}>
        <div class="policy-section-heading">
          <h4 id={`cycle-${cycleId}-configuration`}>Mission and settings</h4>
          <span>{resolvedTimezone.replaceAll('_', ' ')}</span>
        </div>

        <dl class="policy-values">
          {#each configurationEntries as entry (entry.key)}
            {@const source = policyFieldSource(policy.field_sources, entry.key)}
            <div class="policy-value" class:prose-value={entry.key === 'prompt'}>
              <dt>{policyFieldLabel(entry.key)}</dt>
              <dd class:mono-value={entry.key.endsWith('_expr') || typeof entry.value === 'object'}>
                {policyValueLabel(entry.value)}
              </dd>
              <dd class="value-source" title={source?.rationale || undefined}>
                <span>From {source ? policySourceLabel(source) : policySourceLabel(policy.source)}</span>
                <time datetime={source?.changed_at || policy.source.changed_at || undefined}>
                  Changed {formatPolicyDateTime(source?.changed_at || policy.source.changed_at, resolvedTimezone)}
                </time>
              </dd>
            </div>
          {/each}
        </dl>
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
                    Changed {formatPolicyDateTime(guidanceSource?.changed_at || policy.source.changed_at, resolvedTimezone)}
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
                {#if Object.keys(target.config).length}
                  <pre>{policyValueLabel(target.config)}</pre>
                {/if}
                {#if target.rationale}<p>{target.rationale}</p>{/if}
                <div class="value-source">
                  <span>From {[target.source_type, target.source_id].filter(Boolean).join(' · ')}</span>
                  <time datetime={target.updated_at}>
                    Changed {formatPolicyDateTime(target.updated_at, resolvedTimezone)}
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

  {#if !loading && !error}
    <details class="history-region">
      <summary>
        <span>
          <strong>History</strong>
          <small>Prior versions are not active.</small>
        </span>
        <span>{history.length} changes</span>
      </summary>

      <div class="history-content">
        <div class="history-warning">
          <strong>Historical only</strong>
          <span>Text below does not guide the next run.</span>
        </div>

        {#if history.length}
          <ol class="history-list">
            {#each history as change (change.id)}
              {@const retired = retiredGuidance(change)}
              <li class="history-change">
                <header>
                  <div>
                    <strong>Version {change.version}</strong>
                    <time datetime={change.applied_at}>
                      {formatPolicyDateTime(change.applied_at, resolvedTimezone)}
                    </time>
                  </div>
                  <span>{change.changed_fields.map(policyFieldLabel).join(', ')}</span>
                </header>
                <p>{change.rationale}</p>
                <div class="history-source">
                  From {policySourceLabel(change)}
                  {#if change.reverted_from_id !== null}
                    · Reverted change {change.reverted_from_id}
                  {/if}
                </div>

                {#if retired.length}
                  <section class="retired-guidance" aria-label="Retired guidance">
                    <span>Retired guidance · not active</span>
                    {#each retired as guidance}
                      <blockquote>{guidance}</blockquote>
                    {/each}
                  </section>
                {/if}
              </li>
            {/each}
          </ol>
        {:else}
          <p class="empty-policy-value">No policy changes yet.</p>
        {/if}

        {#if historyError}
          <p class="history-error" role="alert">{historyError}</p>
        {/if}
        {#if pagination?.has_more}
          <ConstellationButton
            variant="quiet"
            size="sm"
            loading={historyLoading}
            onclick={loadMoreHistory}
          >
            Load older changes
          </ConstellationButton>
        {/if}
      </div>
    </details>
  {/if}
</section>

<style>
  .effective-policy {
    display: grid;
    min-width: 0;
    overflow: hidden;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-panel-background);
    color: var(--constellation-color-text-primary);
  }

  .active-heading,
  .policy-section-heading,
  .output-target-heading,
  .history-change header {
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
  .output-target p,
  .history-change p,
  .history-change blockquote {
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
  .policy-value dt,
  .live-marker,
  .retired-guidance > span {
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

  .policy-values {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0;
    margin: 0;
    border-top: 1px solid var(--constellation-section-divider);
  }

  .policy-value {
    display: grid;
    align-content: start;
    gap: 7px;
    min-width: 0;
    padding: 12px 10px;
    border-bottom: 1px solid var(--constellation-section-divider);
  }

  .policy-value:nth-child(odd) {
    border-right: 1px solid var(--constellation-section-divider);
  }

  .policy-value.prose-value {
    grid-column: 1 / -1;
    border-right: 0;
  }

  .policy-value dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  .policy-value.prose-value > dd:not(.value-source) {
    font-size: 14px;
  }

  .mono-value,
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
  .output-list,
  .history-list {
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

  .history-region summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 46px;
    padding: 0 14px;
    background: color-mix(in srgb, var(--constellation-color-text-muted) 5%, transparent);
    cursor: pointer;
    list-style: none;
  }

  .history-region summary::-webkit-details-marker {
    display: none;
  }

  .history-region summary > span:first-child {
    display: grid;
    gap: 2px;
  }

  .history-region summary strong {
    font-size: 13px;
    font-weight: 600;
  }

  .history-region summary small,
  .history-region summary > span:last-child,
  .history-source,
  .history-change header span,
  .history-change time {
    color: var(--constellation-color-text-muted);
    font-size: 10px;
  }

  .history-content {
    display: grid;
    gap: 12px;
    padding: 14px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-color-text-muted) 3%, transparent);
  }

  .history-warning {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 10px;
    padding: 8px 10px;
    border: 1px dashed var(--constellation-color-text-muted);
    border-radius: 6px;
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
  }

  .history-warning strong,
  .retired-guidance > span {
    color: var(--constellation-color-text-primary);
  }

  .history-change {
    display: grid;
    gap: 7px;
    padding: 11px 0;
    border-bottom: 1px solid var(--constellation-section-divider);
    opacity: 0.82;
  }

  .history-change:last-child {
    border-bottom: 0;
  }

  .history-change header > div {
    display: grid;
    gap: 3px;
  }

  .history-change header > span {
    max-width: 50%;
    text-align: right;
  }

  .history-change p {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .history-source {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .retired-guidance {
    display: grid;
    gap: 7px;
    margin-top: 3px;
    padding: 10px;
    border: 1px dashed var(--constellation-color-text-muted);
    border-radius: 6px;
    background: color-mix(in srgb, var(--constellation-color-text-muted) 6%, transparent);
  }

  .retired-guidance blockquote {
    padding-left: 10px;
    border-left: 2px solid var(--constellation-color-text-muted);
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    font-style: italic;
    line-height: 1.5;
  }

  .history-error,
  .policy-error {
    color: var(--constellation-color-danger);
    font-size: 12px;
  }

  .policy-error {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px;
  }

  .policy-loading {
    display: grid;
    gap: 8px;
    padding: 14px;
  }

  .policy-loading span {
    height: 48px;
    border-radius: 6px;
    background:
      linear-gradient(90deg, transparent, var(--constellation-skeleton-row-shimmer), transparent),
      var(--constellation-skeleton-row-background);
    background-size: 200% 100%;
    animation: policy-pulse 1.4s ease-in-out infinite;
  }

  .effective-policy.compact .policy-values {
    grid-template-columns: 1fr;
  }

  .effective-policy.compact .policy-value,
  .effective-policy.compact .policy-value:nth-child(odd) {
    border-right: 0;
  }

  @keyframes policy-pulse {
    from { background-position: 200% 0; }
    to { background-position: -200% 0; }
  }

  @media (max-width: 720px) {
    .policy-values {
      grid-template-columns: 1fr;
    }

    .policy-value,
    .policy-value:nth-child(odd) {
      border-right: 0;
    }

    .active-heading,
    .history-change header {
      align-items: flex-start;
      flex-direction: column;
    }

    .active-status {
      justify-items: start;
    }

    .history-change header > span {
      max-width: none;
      text-align: left;
    }
  }
</style>
