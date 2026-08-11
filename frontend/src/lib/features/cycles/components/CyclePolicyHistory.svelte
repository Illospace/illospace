<script lang="ts">
  import type { CyclePolicyHistoryRead, CycleRunRead } from '$lib/api/client';
  import { ConstellationButton } from '$lib/components/constellation';
  import CyclePolicyProvenanceLine from '$lib/features/cycles/components/CyclePolicyProvenanceLine.svelte';
  import {
    formatPolicyDateTime,
    policyActorPresentation,
    policyFieldLabel,
    policyOriginatingRun,
    policySourceLabel,
    retiredGuidance,
  } from '$lib/features/cycles/domain/effectivePolicy';
  import type { CyclePolicyWorkflowState } from '$lib/features/cycles/domain/effectivePolicyWorkflow';

  let {
    history,
    runs = [],
    displayTimezone,
    editable,
    workflowKind,
    revertingChangeId,
    historyError,
    onRevert,
    onLoadMore,
  }: {
    history: CyclePolicyHistoryRead;
    runs?: readonly CycleRunRead[];
    displayTimezone: string;
    editable: boolean;
    workflowKind: CyclePolicyWorkflowState['kind'];
    revertingChangeId: number | null;
    historyError?: string;
    onRevert: (changeId: number) => void;
    onLoadMore: () => Promise<void>;
  } = $props();

  let loadingMore = $state(false);

  async function loadMore(): Promise<void> {
    if (loadingMore) return;
    loadingMore = true;
    try {
      await onLoadMore();
    } finally {
      loadingMore = false;
    }
  }
</script>

<details class="history-region">
  <summary>
    <span>
      <strong>History</strong>
      <small>Prior versions are not active.</small>
    </span>
    <span>{history.items.length} changes</span>
  </summary>

  <div class="history-content">
    <div class="history-warning">
      <strong>Historical only</strong>
      <span>Text below does not guide the next run.</span>
    </div>

    {#if history.items.length}
      <ol class="history-list">
        {#each history.items as change (change.id)}
          {@const retired = retiredGuidance(change)}
          {@const actor = policyActorPresentation(change)}
          {@const originatingRun = policyOriginatingRun(change, runs)}
          <li class="history-change">
            <header>
              <div>
                <strong>Version {change.version}</strong>
                <time datetime={change.applied_at}>
                  {formatPolicyDateTime(change.applied_at, displayTimezone)}
                </time>
              </div>
              <div class="history-change-actions">
                <span>{change.changed_fields.map(policyFieldLabel).join(', ')}</span>
                {#if editable}
                  <ConstellationButton
                    variant="quiet"
                    size="sm"
                    loading={workflowKind === 'reverting' && revertingChangeId === change.id}
                    disabled={workflowKind !== 'view'}
                    onclick={() => onRevert(change.id)}
                  >
                    Revert
                  </ConstellationButton>
                {/if}
              </div>
            </header>
            <p>{change.rationale}</p>
            <CyclePolicyProvenanceLine
              {actor}
              originatingRunId={originatingRun?.id}
              sourceLabel={policySourceLabel(change)}
              appearance="history"
              revertedFromId={change.reverted_from_id}
            />

            {#if retired.length}
              <section class="retired-guidance" aria-label="Retired guidance">
                <span>Retired guidance · not active</span>
                {#each retired as guidance}<blockquote>{guidance}</blockquote>{/each}
              </section>
            {/if}
          </li>
        {/each}
      </ol>
    {:else}
      <p class="empty-policy-value">No policy changes yet.</p>
    {/if}

    {#if historyError}<p class="history-error" role="alert">{historyError}</p>{/if}
    {#if history.pagination.has_more}
      <ConstellationButton variant="quiet" size="sm" loading={loadingMore} onclick={loadMore}>
        Load older changes
      </ConstellationButton>
    {/if}
  </div>
</details>

<style>
  .history-region summary,
  .history-change header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .history-region summary {
    align-items: center;
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

  .history-list {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
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

  .history-change-actions {
    justify-items: end;
  }

  .history-change-actions > span {
    max-width: 50%;
    text-align: right;
  }

  .history-change p,
  .history-change blockquote {
    margin: 0;
  }

  .history-change p {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
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

  .retired-guidance > span {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.12em;
    line-height: 1.2;
    text-transform: uppercase;
  }

  .retired-guidance blockquote {
    padding-left: 10px;
    border-left: 2px solid var(--constellation-color-text-muted);
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    font-style: italic;
    line-height: 1.5;
  }

  .empty-policy-value {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .history-error {
    color: var(--constellation-color-danger);
    font-size: 12px;
  }

  @media (max-width: 720px) {
    .history-change header {
      align-items: flex-start;
      flex-direction: column;
    }

    .history-change-actions {
      justify-items: start;
    }

    .history-change-actions > span {
      max-width: none;
      text-align: left;
    }
  }
</style>
