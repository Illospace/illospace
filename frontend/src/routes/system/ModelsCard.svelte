<script lang="ts">
  import './runtimeCards.css';
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

<section class="runtime-section runtime-card model-routing" aria-labelledby="model-routing-heading">
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
