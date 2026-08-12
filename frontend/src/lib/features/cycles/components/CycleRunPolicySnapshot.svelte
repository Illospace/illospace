<script lang="ts">
  import type { CycleRunRead } from '$lib/api/client';
  import { ConstellationPill } from '$lib/components/constellation';
  import CyclePolicyFieldList from '$lib/features/cycles/components/CyclePolicyFieldList.svelte';
  import CyclePolicyProvenanceLine from '$lib/features/cycles/components/CyclePolicyProvenanceLine.svelte';
  import {
    cycleRunPolicyInspection,
    formatPolicyDateTime,
    policyActorPresentation,
    policyFieldLabel,
    policyOriginatingRun,
    policySourceLabel,
  } from '$lib/features/cycles/domain/effectivePolicy';

  let {
    run,
    runs,
    displayTimezone,
  }: {
    run: CycleRunRead;
    runs: readonly CycleRunRead[];
    displayTimezone: string;
  } = $props();

  const inspection = $derived(cycleRunPolicyInspection(run));
  const actor = $derived(
    inspection.change ? policyActorPresentation(inspection.change) : null,
  );
  const originatingRun = $derived(
    inspection.change ? policyOriginatingRun(inspection.change, runs) : null,
  );
</script>

<details class="run-policy-snapshot">
  <summary>
    <span>Execution snapshot</span>
    <small>
      {#if inspection.version !== null}
        Policy version {inspection.version}
      {:else if inspection.revisionNumber !== null}
        Revision {inspection.revisionNumber}
      {:else}
        Not recorded
      {/if}
    </small>
  </summary>

  <div class="snapshot-content">
    {#if inspection.hasSnapshot}
      <section class="snapshot-section" aria-labelledby={`run-${run.id}-policy-heading`}>
        <header>
          <div>
            <span>Admitted policy</span>
            <h5 id={`run-${run.id}-policy-heading`}>Policy snapshot</h5>
          </div>
          <ConstellationPill variant="muted">Read only</ConstellationPill>
        </header>

        <CyclePolicyFieldList entries={inspection.configuration} appearance="snapshot" />

        <div class="snapshot-guidance">
          <div>
            <strong>Guidance</strong>
            <span>{inspection.guidance.length} admitted</span>
          </div>
          {#if inspection.guidance.length}
            <ol>
              {#each inspection.guidance as guidance}<li>{guidance}</li>{/each}
            </ol>
          {:else}
            <p>No guidance was admitted.</p>
          {/if}
        </div>
      </section>

      <section class="snapshot-section producing-change" aria-labelledby={`run-${run.id}-change-heading`}>
        <header>
          <div>
            <span>Policy provenance</span>
            <h5 id={`run-${run.id}-change-heading`}>Change that produced this snapshot</h5>
          </div>
        </header>

        {#if inspection.change}
          {#if actor}
            <CyclePolicyProvenanceLine
              {actor}
              originatingRunId={originatingRun?.id}
              sourceLabel={policySourceLabel(inspection.change)}
              appearance="snapshot"
              version={inspection.change.version}
              appliedAt={inspection.change.applied_at}
              appliedAtLabel={formatPolicyDateTime(inspection.change.applied_at, displayTimezone)}
              rationaleLabel={inspection.change.rationale || 'No rationale recorded.'}
              changedFieldsLabel={inspection.change.changed_fields.length
                ? `Changed ${inspection.change.changed_fields.map(policyFieldLabel).join(', ')}`
                : null}
            />
          {/if}
        {:else}
          <p class="change-rationale">
            This run used the initial Cycle definition. No producing policy change was recorded.
          </p>
        {/if}
      </section>
    {:else}
      <p class="snapshot-empty">No admitted policy snapshot was recorded for this run.</p>
    {/if}
  </div>
</details>

<style>
  .run-policy-snapshot {
    grid-column: 1 / -1;
    min-width: 0;
    border: calc(var(--sp-1) / 4) solid var(--constellation-surface-nested-border);
    border-radius: var(--radius-md);
    background: var(--constellation-surface-nested-background);
  }

  .run-policy-snapshot summary,
  .snapshot-section header,
  .snapshot-guidance > div {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-3);
  }

  .run-policy-snapshot summary {
    min-height: 40px;
    padding: 0 var(--sp-3);
    color: var(--constellation-color-text-primary);
    cursor: pointer;
    list-style: none;
  }

  .run-policy-snapshot summary::-webkit-details-marker {
    display: none;
  }

  .run-policy-snapshot summary span,
  .snapshot-section h5 {
    font-size: 12px;
    font-weight: 600;
  }

  .run-policy-snapshot summary small,
  .snapshot-section header span,
  .snapshot-guidance span {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
  }

  .run-policy-snapshot[open] summary {
    border-bottom: calc(var(--sp-1) / 4) solid var(--constellation-surface-panel-separator);
  }

  .snapshot-content {
    display: grid;
  }

  .snapshot-section {
    display: grid;
    gap: var(--sp-3);
    min-width: 0;
    padding: var(--sp-3);
    border-bottom: calc(var(--sp-1) / 4) solid var(--constellation-surface-panel-separator);
  }

  .snapshot-section:last-child {
    border-bottom: 0;
  }

  .snapshot-section header {
    align-items: flex-start;
  }

  .snapshot-section header > div {
    display: grid;
    gap: var(--sp-1);
  }

  .snapshot-section h5,
  .snapshot-guidance strong,
  .snapshot-guidance p,
  .change-rationale {
    margin: 0;
  }

  .snapshot-section header span {
    font-weight: 650;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .snapshot-guidance strong {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .snapshot-guidance {
    display: grid;
    gap: var(--sp-2);
    padding: var(--sp-3);
    border-left: calc(var(--sp-1) * 0.75) solid var(--constellation-color-success);
    background: color-mix(in srgb, var(--constellation-color-success) 3%, transparent);
  }

  .snapshot-guidance ol {
    display: grid;
    gap: var(--sp-2);
    margin: 0;
    padding-left: var(--sp-5);
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.5;
  }

  .snapshot-guidance p,
  .change-rationale,
  .snapshot-empty {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.5;
  }

  .producing-change {
    grid-template-columns: minmax(0, 1fr) auto;
    background: color-mix(in srgb, var(--constellation-color-text-muted) 3%, transparent);
  }

  .producing-change > header {
    grid-column: 1;
    grid-row: 1;
  }

  .producing-change > .change-rationale {
    grid-column: 1 / -1;
  }

  .snapshot-empty {
    margin: 0;
    padding: var(--sp-3);
  }

  @media (max-width: 720px) {
    .producing-change {
      grid-template-columns: 1fr;
    }

    .snapshot-section header {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
