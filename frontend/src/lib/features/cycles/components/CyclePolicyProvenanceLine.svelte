<script lang="ts">
  import { ConstellationPill } from '$lib/components/constellation';
  import {
    cycleRunAnchorId,
    type CyclePolicyActorPresentation,
  } from '$lib/features/cycles/domain/effectivePolicy';

  let {
    actor,
    originatingRunId = null,
    sourceLabel,
    appearance,
    version = null,
    appliedAt = null,
    appliedAtLabel = null,
    rationaleLabel = null,
    changedFieldsLabel = null,
    revertedFromId = null,
  }: {
    actor: CyclePolicyActorPresentation;
    originatingRunId?: number | null;
    sourceLabel: string;
    appearance: 'history' | 'snapshot';
    version?: number | null;
    appliedAt?: string | null;
    appliedAtLabel?: string | null;
    rationaleLabel?: string | null;
    changedFieldsLabel?: string | null;
    revertedFromId?: number | null;
  } = $props();
</script>

<div
  class="policy-provenance"
  class:history={appearance === 'history'}
  class:snapshot={appearance === 'snapshot'}
  class:agent-source={actor.kind === 'agent'}
  class:has-changed-fields={Boolean(changedFieldsLabel)}
>
  <div class="actor-pill">
    <ConstellationPill
      variant={actor.kind === 'agent' ? 'info' : actor.kind === 'human' ? 'muted' : 'warning'}
      leadingDot
    >
      {actor.label}
    </ConstellationPill>
  </div>
  {#if rationaleLabel}<p class="change-rationale">{rationaleLabel}</p>{/if}
  <div class="provenance-facts">
    {#if version !== null}<span>Version {version}</span>{/if}
    <span><strong>Actor</strong> {actor.identity}</span>
    {#if appliedAt && appliedAtLabel}
      <time datetime={appliedAt}>{appliedAtLabel}</time>
    {/if}
    {#if originatingRunId !== null}
      <a href={`#${cycleRunAnchorId(originatingRunId)}`}>Originating Run #{originatingRunId}</a>
    {/if}
  </div>
  {#if changedFieldsLabel}<p class="changed-fields">{changedFieldsLabel}</p>{/if}
  <code>{sourceLabel}</code>
  {#if revertedFromId !== null}<span class="reverted-change">Reverted change {revertedFromId}</span>{/if}
</div>

<style>
  .policy-provenance {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
  }

  .history {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--sp-2) var(--sp-3);
    padding: var(--sp-2);
    border-left: calc(var(--sp-1) / 2) solid var(--constellation-color-text-muted);
    background: color-mix(in srgb, var(--constellation-color-text-muted) 3%, transparent);
  }

  .history.agent-source {
    border-left-color: var(--constellation-control-pill-info-text);
    background: color-mix(in srgb, var(--constellation-control-pill-info-text) 4%, transparent);
  }

  .history .provenance-facts {
    display: contents;
  }

  .snapshot {
    display: contents;
  }

  .snapshot .actor-pill {
    grid-column: 2;
    grid-row: 1;
    justify-self: end;
  }

  .snapshot .provenance-facts {
    display: flex;
    grid-column: 1 / -1;
    grid-row: 3;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--sp-3);
  }

  .snapshot .change-rationale {
    grid-column: 1 / -1;
    grid-row: 2;
  }

  .snapshot .changed-fields {
    grid-column: 1 / -1;
    grid-row: 4;
  }

  .snapshot code {
    grid-column: 1 / -1;
    grid-row: 4;
  }

  .snapshot.has-changed-fields code {
    grid-row: 5;
  }

  .change-rationale {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-family: var(--font-sans);
    font-size: 12px;
    line-height: 1.5;
  }

  .changed-fields {
    margin: 0;
    letter-spacing: 0.04em;
  }

  strong {
    color: var(--constellation-color-text-secondary);
    font-weight: 650;
  }

  a {
    color: var(--constellation-color-text-secondary);
    font-weight: 650;
  }

  code {
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-muted);
    font-family: inherit;
    font-size: inherit;
  }

  @media (max-width: 720px) {
    .snapshot .actor-pill {
      grid-column: 1;
      grid-row: 2;
      justify-self: start;
    }

    .snapshot .change-rationale {
      grid-row: 3;
    }

    .snapshot .provenance-facts {
      grid-row: 4;
    }

    .snapshot .changed-fields,
    .snapshot code {
      grid-row: 5;
    }

    .snapshot.has-changed-fields code {
      grid-row: 6;
    }
  }
</style>
