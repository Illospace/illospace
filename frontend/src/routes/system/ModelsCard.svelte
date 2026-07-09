<script lang="ts">
  import { DEFAULT_MODEL_FIELD } from './constants';
  import RuntimeSelect from './RuntimeSelect.svelte';
  import type { RuntimeOption } from './types';

  let {
    modelDraft,
    modelOptions,
    thinkingOptions,
    canManageSettings,
    onUpdateModel,
    onUpdateThinking,
  }: {
    modelDraft: { default: string; thinking: string };
    modelOptions: RuntimeOption[];
    thinkingOptions: RuntimeOption[];
    canManageSettings: boolean;
    onUpdateModel: (value: string) => void;
    onUpdateThinking: (value: string) => void;
  } = $props();
</script>

<section class="runtime-section model-routing" aria-labelledby="model-routing-heading">
  <header class="runtime-section-heading">
    <div>
      <h2 id="model-routing-heading">Model</h2>
      <p>Choose the default connected model for new runs.</p>
    </div>
  </header>

  <div class="model-row">
    <div class="model-copy">
      <h3>{DEFAULT_MODEL_FIELD.label}</h3>
      <p>{DEFAULT_MODEL_FIELD.help}</p>
    </div>

    <RuntimeSelect
      id="model-default"
      label="Default model"
      labelHidden
      value={modelDraft.default}
      options={modelOptions}
      disabled={!canManageSettings}
      onValueChange={onUpdateModel}
    />
  </div>

  <div class="model-row">
    <div class="model-copy">
      <h3>Default effort</h3>
      <p>Reasoning effort used for new runs unless a composer selection overrides it.</p>
    </div>

    <RuntimeSelect
      id="model-thinking"
      label="Default reasoning effort"
      labelHidden
      value={modelDraft.thinking}
      options={thinkingOptions}
      disabled={!canManageSettings}
      onValueChange={onUpdateThinking}
    />
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
  .model-copy h3 {
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
  .model-copy p {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .model-row {
    display: grid;
    grid-template-columns: minmax(220px, 1fr) minmax(260px, 360px);
    align-items: center;
    gap: clamp(16px, 2vw, 28px);
    min-width: 0;
    padding: 12px 0 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .model-copy {
    display: grid;
    gap: 5px;
    min-width: 0;
  }

  .model-copy h3 {
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    line-height: 1;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  @media (max-width: 760px) {
    .model-row {
      grid-template-columns: 1fr;
      gap: 10px;
    }
  }
</style>
