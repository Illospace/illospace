<script lang="ts">
  import type { CycleRunRead } from '$lib/api/client';
  import { ConstellationPill } from '$lib/components/constellation';
  import {
    cycleRunPolicyInspection,
    formatPolicyDateTime,
    policyActorPresentation,
    policyFieldLabel,
    policyOriginatingRun,
    policySourceLabel,
    policyValueLabel,
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

        <dl class="snapshot-values">
          {#each inspection.configuration as entry (entry.key)}
            <div class:wide-value={entry.key === 'prompt'}>
              <dt>{policyFieldLabel(entry.key)}</dt>
              <dd class:mono-value={entry.key.endsWith('_expr') || typeof entry.value === 'object'}>
                {policyValueLabel(entry.value)}
              </dd>
            </div>
          {/each}
        </dl>

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
          {#if actor}
            <ConstellationPill
              variant={actor.kind === 'agent' ? 'info' : actor.kind === 'human' ? 'muted' : 'warning'}
              leadingDot
            >
              {actor.label}
            </ConstellationPill>
          {/if}
        </header>

        {#if inspection.change}
          <p class="change-rationale">{inspection.change.rationale || 'No rationale recorded.'}</p>
          <div class="change-facts">
            {#if inspection.change.version !== null}<span>Version {inspection.change.version}</span>{/if}
            {#if actor}<span><strong>Actor</strong> {actor.identity}</span>{/if}
            {#if inspection.change.applied_at}
              <time datetime={inspection.change.applied_at}>
                {formatPolicyDateTime(inspection.change.applied_at, displayTimezone)}
              </time>
            {/if}
            {#if originatingRun}
              <a href={`#cycle-run-${originatingRun.id}`}>Originating Run #{originatingRun.id}</a>
            {/if}
          </div>
          {#if inspection.change.changed_fields.length}
            <p class="changed-fields">
              Changed {inspection.change.changed_fields.map(policyFieldLabel).join(', ')}
            </p>
          {/if}
          <code>{policySourceLabel(inspection.change)}</code>
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
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
  }

  .run-policy-snapshot summary,
  .snapshot-section header,
  .snapshot-guidance > div,
  .change-facts {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .run-policy-snapshot summary {
    min-height: 40px;
    padding: 0 11px;
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
  .snapshot-guidance span,
  .change-facts,
  .changed-fields,
  .producing-change code {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
  }

  .run-policy-snapshot[open] summary {
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .snapshot-content {
    display: grid;
  }

  .snapshot-section {
    display: grid;
    gap: 12px;
    min-width: 0;
    padding: 12px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .snapshot-section:last-child {
    border-bottom: 0;
  }

  .snapshot-section header {
    align-items: flex-start;
  }

  .snapshot-section header > div {
    display: grid;
    gap: 3px;
  }

  .snapshot-section h5,
  .snapshot-guidance strong,
  .snapshot-guidance p,
  .change-rationale,
  .changed-fields {
    margin: 0;
  }

  .snapshot-section header span {
    font-weight: 650;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .snapshot-values {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0;
    border-top: 1px solid var(--constellation-section-divider);
  }

  .snapshot-values > div {
    display: grid;
    align-content: start;
    gap: 6px;
    min-width: 0;
    padding: 10px;
    border-bottom: 1px solid var(--constellation-section-divider);
  }

  .snapshot-values > div:nth-child(odd) {
    border-right: 1px solid var(--constellation-section-divider);
  }

  .snapshot-values > .wide-value {
    grid-column: 1 / -1;
    border-right: 0;
  }

  .snapshot-values dt,
  .snapshot-guidance strong {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .snapshot-values dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 12px;
    line-height: 1.45;
    white-space: pre-wrap;
  }

  .mono-value,
  .producing-change code {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .snapshot-guidance {
    display: grid;
    gap: 8px;
    padding: 10px;
    border-left: 3px solid var(--constellation-color-success);
    background: color-mix(in srgb, var(--constellation-color-success) 3%, transparent);
  }

  .snapshot-guidance ol {
    display: grid;
    gap: 7px;
    margin: 0;
    padding-left: 22px;
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
    background: color-mix(in srgb, var(--constellation-color-text-muted) 3%, transparent);
  }

  .change-facts {
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  .change-facts strong {
    color: var(--constellation-color-text-secondary);
    font-weight: 650;
  }

  .change-facts a {
    color: var(--constellation-color-text-secondary);
    font-weight: 650;
  }

  .changed-fields {
    letter-spacing: 0.04em;
  }

  .producing-change code {
    overflow-wrap: anywhere;
  }

  .snapshot-empty {
    margin: 0;
    padding: 12px;
  }

  @media (max-width: 720px) {
    .snapshot-values {
      grid-template-columns: 1fr;
    }

    .snapshot-values > div:nth-child(odd) {
      border-right: 0;
    }

    .snapshot-section header {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
