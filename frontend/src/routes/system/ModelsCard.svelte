<script lang="ts">
  import { MODEL_FIELDS } from './constants';
  import RuntimeSelect from './RuntimeSelect.svelte';
  import type { ModelTier, RuntimeOption } from './types';

  let {
    modelDraft,
    modelOptions,
    canManageSettings,
    onUpdateModel,
  }: {
    modelDraft: Record<ModelTier, string>;
    modelOptions: RuntimeOption[];
    canManageSettings: boolean;
    onUpdateModel: (tier: ModelTier, value: string) => void;
  } = $props();
</script>

<section class="runtime-section model-routing" aria-labelledby="model-routing-heading">
  <header class="runtime-section-heading">
    <div>
      <h2 id="model-routing-heading">Model routing</h2>
      <p>Choose which connected model handles each workload tier.</p>
    </div>
  </header>

  <div class="model-routing-list">
    {#each MODEL_FIELDS as field}
      <article class="model-tier-row">
        <div class="model-tier-copy">
          <h3>{field.label}</h3>
          <p>{field.help}</p>
        </div>

        <RuntimeSelect
          id={`model-${field.key}`}
          label={`${field.label} model`}
          labelHidden
          value={modelDraft[field.key]}
          options={modelOptions}
          disabled={!canManageSettings}
          onValueChange={(value) => onUpdateModel(field.key, value)}
        />
      </article>
    {/each}
  </div>
</section>

<style>
  .runtime-section {
    display: grid;
    gap: 18px;
    min-width: 0;
    padding: 22px 0;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .runtime-section-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
  }

  .runtime-section-heading div {
    display: grid;
    gap: 7px;
    min-width: 0;
  }

  .runtime-section-heading h2,
  .model-tier-copy h3 {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-weight: 560;
    letter-spacing: 0;
  }

  .runtime-section-heading h2 {
    font-size: 18px;
    line-height: 1.2;
  }

  .runtime-section-heading p,
  .model-tier-copy p {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .model-routing-list {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .model-tier-row {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) minmax(260px, 360px);
    align-items: center;
    gap: clamp(16px, 2vw, 28px);
    min-width: 0;
    padding: 12px 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .model-tier-row:first-child {
    border-top: 0;
    padding-top: 0;
  }

  .model-tier-row:last-child {
    padding-bottom: 0;
  }

  .model-tier-copy {
    display: grid;
    gap: 5px;
    min-width: 0;
  }

  .model-tier-copy h3 {
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    line-height: 1;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  @media (max-width: 760px) {
    .model-tier-row {
      grid-template-columns: 1fr;
      gap: 10px;
    }
  }
</style>
