<script lang="ts">
  import {
    ConstellationButton,
    ConstellationIcon,
    ConstellationPanel,
    ConstellationPill,
  } from '$lib/components/constellation';

  import type { StartupGuideStep, StartupStepKey, StartupStepStatus } from './types';

  let {
    steps,
    canManageSettings,
    onGoToStep,
  }: {
    steps: StartupGuideStep[];
    canManageSettings: boolean;
    onGoToStep: (step: StartupStepKey) => void;
  } = $props();

  const completedCount = $derived(steps.filter((step) => step.status === 'complete').length);
  const guideTone = $derived(completedCount === steps.length ? 'success' : 'info');
  const summary = $derived(`${completedCount}/${steps.length} ready`);

  function statusLabel(status: StartupStepStatus) {
    if (status === 'complete') return 'Ready';
    if (status === 'current') return 'Next';
    if (status === 'blocked') return 'Blocked';
    return 'Pending';
  }

  function statusTone(status: StartupStepStatus) {
    if (status === 'complete') return 'success';
    if (status === 'current') return 'info';
    if (status === 'blocked') return 'warning';
    return 'muted';
  }
</script>

<ConstellationPanel className="startup-guide" tone={guideTone} ariaLabel="First setup">
  <div class="guide-shell">
    <div class="guide-header">
      <div class="guide-copy">
        <p class="guide-eyebrow">First setup</p>
        <h2>Start Here</h2>
        <p>
          {#if canManageSettings}
            Set up this Illo workspace once. Access, models, and memory become the runtime baseline.
          {:else}
            An owner or admin needs to finish this workspace setup.
          {/if}
        </p>
      </div>
      <ConstellationPill variant={guideTone} leadingDot>{summary}</ConstellationPill>
    </div>

    <div class="guide-steps">
      {#each steps as step, index}
        <article class:complete={step.status === 'complete'} class:blocked={step.status === 'blocked'} class="guide-step">
          <div class="step-marker" aria-hidden="true">
            {#if step.status === 'complete'}
              <ConstellationIcon name="check" size={15} />
            {:else}
              {index + 1}
            {/if}
          </div>

          <div class="step-copy">
            <div class="step-title-row">
              <h3>{step.title}</h3>
              <ConstellationPill variant={statusTone(step.status)} leadingDot>{statusLabel(step.status)}</ConstellationPill>
            </div>
            <p>{step.detail}</p>
          </div>

          <ConstellationButton variant="quiet" size="sm" onclick={() => onGoToStep(step.key)}>
            {#snippet trailingVisual()}
              <ConstellationIcon name="chevron-right" size={13} />
            {/snippet}
            Open
          </ConstellationButton>
        </article>
      {/each}
    </div>

    <p class="guide-note">Memory is workspace-wide. Choose once; changing later requires a rebuild.</p>
  </div>
</ConstellationPanel>

<style>
  :global(.startup-guide .constellation-panel-content) {
    padding: 18px;
  }

  .guide-shell {
    display: grid;
    gap: 18px;
  }

  .guide-header {
    display: flex;
    gap: 18px;
    align-items: flex-start;
    justify-content: space-between;
  }

  .guide-copy {
    display: grid;
    gap: 6px;
    min-width: 0;
  }

  .guide-eyebrow {
    margin: 0;
    color: var(--constellation-text-muted);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  h2,
  h3,
  p {
    margin: 0;
    min-width: 0;
  }

  h2 {
    color: var(--constellation-text-primary);
    font-size: var(--constellation-type-title-sm);
    line-height: 1.1;
  }

  .guide-copy p,
  .step-copy p,
  .guide-note {
    color: var(--constellation-text-muted);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .guide-steps {
    display: grid;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .guide-step {
    display: grid;
    grid-template-columns: 34px minmax(0, 1fr) auto;
    gap: 14px;
    align-items: center;
    min-width: 0;
    padding: 14px 0;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .step-marker {
    display: grid;
    width: 30px;
    height: 30px;
    place-items: center;
    border: 1px solid var(--constellation-control-pill-border);
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-control-pill-muted-background);
    color: var(--constellation-text-muted);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
  }

  .guide-step.complete .step-marker {
    border-color: var(--constellation-control-pill-success-border);
    background: var(--constellation-control-pill-success-background);
    color: var(--constellation-control-pill-success-text);
  }

  .guide-step.blocked .step-marker {
    border-color: var(--constellation-control-pill-warning-border);
    background: var(--constellation-control-pill-warning-background);
    color: var(--constellation-control-pill-warning-text);
  }

  .step-copy {
    display: grid;
    gap: 5px;
    min-width: 0;
  }

  .step-title-row {
    display: flex;
    gap: 10px;
    align-items: center;
    min-width: 0;
  }

  h3 {
    color: var(--constellation-text-primary);
    font-size: var(--constellation-type-body);
    line-height: 1.2;
  }

  .guide-note {
    padding-left: 44px;
  }

  @media (max-width: 760px) {
    .guide-header,
    .step-title-row {
      align-items: flex-start;
      flex-direction: column;
    }

    .guide-step {
      grid-template-columns: 34px minmax(0, 1fr);
    }

    .guide-step :global(.constellation-button) {
      grid-column: 2;
      justify-self: start;
    }
  }
</style>
