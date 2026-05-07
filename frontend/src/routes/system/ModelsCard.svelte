<script lang="ts">
  import { ConstellationButton } from '$lib/components/constellation';

  import { MODEL_FIELDS } from './constants';
  import RuntimeSelect from './RuntimeSelect.svelte';
  import SetupCard from './SetupCard.svelte';
  import type { ModelTier, RuntimeOption } from './types';

  let {
    description,
    modelDraft,
    modelOptions,
    canManageSettings,
    savingModels,
    onUpdateModel,
    onSaveModels,
  }: {
    description: string;
    modelDraft: Record<ModelTier, string>;
    modelOptions: RuntimeOption[];
    canManageSettings: boolean;
    savingModels: boolean;
    onUpdateModel: (tier: ModelTier, value: string) => void;
    onSaveModels: () => void;
  } = $props();
</script>

<SetupCard
  eyebrow="Models"
  title="Choose Models"
  {description}
  status="configured"
  statusTone="success"
>
  <div class="model-grid">
    {#each MODEL_FIELDS as field}
      <div class="tier-field">
        <RuntimeSelect
          id={`model-${field.key}`}
          label={field.label}
          value={modelDraft[field.key]}
          options={modelOptions}
          disabled={!canManageSettings}
          onValueChange={(value) => onUpdateModel(field.key, value)}
        />
        <p>{field.help}</p>
      </div>
    {/each}
  </div>

  <div class="panel-actions">
    <ConstellationButton onclick={onSaveModels} loading={savingModels} disabled={!canManageSettings}>
      Save models
    </ConstellationButton>
  </div>
</SetupCard>

<style>
  .model-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 18px;
  }

  .tier-field {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .tier-field p {
    margin: 0;
    color: var(--constellation-text-muted);
    font-size: var(--constellation-type-body-sm);
  }

  .panel-actions {
    display: flex;
    justify-content: flex-end;
  }

  @media (max-width: 980px) {
    .model-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
