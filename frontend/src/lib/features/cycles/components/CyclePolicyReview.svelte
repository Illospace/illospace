<script lang="ts">
  import { onMount, tick } from 'svelte';

  import {
    ConstellationButton,
    ConstellationNotice,
    ConstellationTextarea,
  } from '$lib/components/constellation';
  import { presentedPolicyDiff } from '$lib/features/cycles/domain/effectivePolicy';
  import type { PolicyReviewProps } from '$lib/features/cycles/domain/effectivePolicyWorkflow';

  let {
    cycleId,
    review,
    rationale,
    applying,
    applyDisabled,
    activeRunBoundary,
    error,
    compact = false,
    onRationaleChange,
    onBack,
    onApply,
  }: PolicyReviewProps & {
    cycleId: number;
    compact?: boolean;
    onRationaleChange: (rationale: string) => void;
    onBack: () => void;
    onApply: () => void;
  } = $props();

  const diffEntries = $derived(presentedPolicyDiff(review.preview));
  let reviewHeadingEl: HTMLHeadingElement | undefined = $state();

  function inputValue(event: Event): string {
    return (event.currentTarget as HTMLTextAreaElement).value;
  }

  onMount(() => {
    tick().then(() => reviewHeadingEl?.focus());
  });
</script>

<section class:compact class="policy-review" aria-label="Review cycle behavior change">
  <header class="editor-heading">
    <div>
      <span class="section-kicker">Review</span>
      <h4 bind:this={reviewHeadingEl} tabindex="-1">
        {review.kind === 'revert' ? 'Review revert' : 'Review behavior change'}
      </h4>
    </div>
    <p>{activeRunBoundary}</p>
  </header>

  {#if review.preview.changed_fields.length}
    <div class="diff-list">
      {#each diffEntries as entry (entry.key)}
        <article class="diff-entry">
          <h5>{entry.label}</h5>
          {#if entry.kind === 'schedule'}
            <div class="diff-columns">
              <div>
                <span>Before</span>
                <strong>{entry.before.schedule_human}</strong>
                <code>{entry.before.schedule_expr}</code>
                <small>{entry.before.timezone.replaceAll('_', ' ')}</small>
              </div>
              <div>
                <span>After</span>
                <strong>{entry.after.schedule_human}</strong>
                <code>{entry.after.schedule_expr}</code>
                <small>{entry.after.timezone.replaceAll('_', ' ')}</small>
              </div>
            </div>
          {:else if entry.kind === 'guidance'}
            <div class="guidance-diff">
              {#if entry.retired.length}
                <div class="guidance-retire">
                  <span>Retire</span>
                  {#each entry.retired as item}<p>{item}</p>{/each}
                </div>
              {/if}
              {#if entry.added.length}
                <div class="guidance-add">
                  <span>Add</span>
                  {#each entry.added as item}<p>{item}</p>{/each}
                </div>
              {/if}
            </div>
          {:else}
            <div class="diff-columns">
              <div><span>Before</span><p>{entry.before}</p></div>
              <div><span>After</span><p>{entry.after}</p></div>
            </div>
          {/if}
        </article>
      {/each}
    </div>
  {:else}
    <ConstellationNotice
      title="No effective change"
      description="This draft matches the current behavior. There is nothing to apply."
      tone="neutral"
      compact
    />
  {/if}

  {#each review.preview.warnings as warning (warning.code)}
    <p class="review-warning">{warning.message}</p>
  {/each}

  <div class="editor-field rationale-field">
    <label for={`cycle-${cycleId}-policy-rationale`}>Rationale <span>Required</span></label>
    <ConstellationTextarea
      id={`cycle-${cycleId}-policy-rationale`}
      value={rationale}
      rows={3}
      maxlength={5000}
      required
      aria-required="true"
      placeholder="Why should this behavior change?"
      oninput={(event) => onRationaleChange(inputValue(event))}
    />
  </div>

  {#if error}<p class="editor-error" role="alert">{error}</p>{/if}

  <div class="editor-actions">
    <ConstellationButton variant="quiet" size="sm" onclick={onBack}>
      {review.kind === 'edit' ? 'Back to draft' : 'Cancel'}
    </ConstellationButton>
    <ConstellationButton
      variant="primary"
      size="sm"
      loading={applying}
      disabled={applyDisabled}
      onclick={onApply}
    >
      {review.kind === 'revert' ? 'Apply revert' : 'Apply change'}
    </ConstellationButton>
  </div>
</section>

<style>
  .policy-review {
    display: grid;
    gap: 16px;
    min-width: 0;
    padding: 16px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-color-info) 3%, transparent);
  }

  .editor-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .editor-heading h4,
  .editor-heading p,
  .diff-entry h5,
  .diff-entry p,
  .review-warning,
  .editor-error {
    margin: 0;
  }

  .section-kicker,
  .editor-field > label {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    line-height: 1.2;
    text-transform: uppercase;
  }

  .section-kicker {
    letter-spacing: 0.12em;
  }

  .editor-heading h4 {
    margin-top: 4px;
    font-size: 15px;
    font-weight: 650;
  }

  .editor-heading p {
    max-width: 360px;
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.5;
    text-align: right;
  }

  .diff-list {
    display: grid;
    gap: 10px;
    min-width: 0;
  }

  .diff-entry {
    display: grid;
    gap: 9px;
    min-width: 0;
    padding: 12px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 7px;
    background: var(--constellation-surface-nested-background);
  }

  .diff-entry h5 {
    font-size: 12px;
    font-weight: 650;
  }

  .diff-columns,
  .guidance-diff {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .diff-columns > div,
  .guidance-diff > div {
    display: grid;
    align-content: start;
    gap: 6px;
    min-width: 0;
    padding: 9px;
    border-left: 2px solid var(--constellation-color-text-muted);
    background: color-mix(in srgb, var(--constellation-color-text-muted) 4%, transparent);
  }

  .diff-columns span,
  .guidance-diff span {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .diff-columns strong,
  .diff-columns p,
  .guidance-diff p {
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  .diff-columns code {
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
  }

  .diff-columns small {
    color: var(--constellation-color-text-muted);
    font-size: 10px;
  }

  .guidance-retire {
    border-left-color: var(--constellation-color-warning) !important;
  }

  .guidance-add {
    border-left-color: var(--constellation-color-success) !important;
  }

  .review-warning {
    padding: 8px 10px;
    border-left: 2px solid var(--constellation-color-info);
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.45;
  }

  .editor-field {
    display: grid;
    align-content: start;
    gap: 7px;
    min-width: 0;
  }

  .editor-field > label {
    color: var(--constellation-color-text-secondary);
    letter-spacing: 0.08em;
  }

  .editor-field > label span {
    color: var(--constellation-color-warning);
    letter-spacing: 0;
    text-transform: none;
  }

  .editor-field :global(.constellation-textarea) {
    width: 100%;
  }

  .rationale-field {
    padding-top: 2px;
  }

  .editor-error {
    color: var(--constellation-color-danger);
    font-size: 11px;
    line-height: 1.4;
  }

  .editor-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--constellation-section-divider);
  }

  .policy-review.compact .diff-columns,
  .policy-review.compact .guidance-diff {
    grid-template-columns: 1fr;
  }

  .policy-review.compact .editor-heading {
    flex-direction: column;
  }

  .policy-review.compact .editor-heading p {
    max-width: none;
    text-align: left;
  }

  @media (max-width: 720px) {
    .editor-heading {
      align-items: flex-start;
      flex-direction: column;
    }

    .editor-heading p {
      max-width: none;
      text-align: left;
    }

    .diff-columns,
    .guidance-diff {
      grid-template-columns: 1fr;
    }
  }
</style>
