<script lang="ts">
  import { ConstellationButton, ConstellationNotice } from '$lib/components/constellation';

  import RuntimeSelect from './RuntimeSelect.svelte';
  import SetupCard from './SetupCard.svelte';
  import type { MemoryCheck, MemoryDraft, MemoryNoticeState, PillTone, RuntimeSettings } from './types';

  let {
    description,
    status,
    statusTone,
    memory,
    memoryDraft,
    embeddingModelOptions,
    notice,
    memoryCheck,
    canManageSettings,
    savingMemory,
    checkingMemory,
    onCheckMemory,
    onUpdateMemory,
    onSaveMemory,
    onAddApiKey,
    saveDisabled = false,
    checkDisabled = false,
  }: {
    description: string;
    status: string;
    statusTone: PillTone;
    memory: RuntimeSettings['memory'];
    memoryDraft: MemoryDraft;
    embeddingModelOptions: RuntimeSettings['memory']['embedding_model_options'];
    notice: MemoryNoticeState | null;
    memoryCheck: MemoryCheck | null;
    canManageSettings: boolean;
    savingMemory: boolean;
    checkingMemory: boolean;
    onCheckMemory: () => void;
    onUpdateMemory: (key: keyof MemoryDraft, value: string) => void;
    onSaveMemory: () => void;
    onAddApiKey: () => void;
    saveDisabled?: boolean;
    checkDisabled?: boolean;
  } = $props();

  const localModelLabel = $derived(memoryDraft.embedder === 'local_cpu' ? 'Local CPU model' : 'Local GPU model');
  const usesApiModel = $derived(memoryDraft.embedder === 'openai' || memoryDraft.embedder === 'gemini');
</script>

{#snippet addKeyActions()}
  <ConstellationButton variant="secondary" size="sm" onclick={onAddApiKey}>
    Add API key
  </ConstellationButton>
{/snippet}

<SetupCard
  eyebrow="Memory"
  title="Set Up Memory"
  {description}
  {status}
  {statusTone}
>
  {#snippet actions()}
    <ConstellationButton
      variant="secondary"
      onclick={onCheckMemory}
      loading={checkingMemory || savingMemory}
      disabled={!canManageSettings || checkDisabled}
    >
      Save & check
    </ConstellationButton>
  {/snippet}

  <div class="memory-notices">
    {#if notice}
      <ConstellationNotice
        title={notice.title}
        description={notice.detail}
        tone={notice.tone}
        compact
        actions={notice.showAddKeyAction ? addKeyActions : undefined}
      />
    {/if}

    {#if memoryCheck}
      <ConstellationNotice
        title={memoryCheck.status === 'ok' ? 'Memory check passed.' : 'Memory check failed.'}
        description={`${memoryCheck.detail}${memoryCheck.duration_ms ? ` (${memoryCheck.duration_ms} ms)` : ''}`}
        tone={memoryCheck.status === 'ok' ? 'success' : 'danger'}
        compact
      />
    {/if}
  </div>

  <div class="memory-grid">
    <RuntimeSelect
      id="memory-embedder"
      label="Embedder"
      value={memoryDraft.embedder}
      options={memory.embedder_options}
      disabled={!canManageSettings}
      onValueChange={(value) => onUpdateMemory('embedder', value)}
    />
    {#if usesApiModel}
      <RuntimeSelect
        id="memory-embedding-model"
        label="Embedding model"
        value={memoryDraft.embedding_model}
        options={embeddingModelOptions}
        disabled={!canManageSettings}
        onValueChange={(value) => onUpdateMemory('embedding_model', value)}
      />
    {:else}
      <label class="runtime-static-field" for="memory-local-model">
        <span>Embedding model</span>
        <div id="memory-local-model">{localModelLabel}</div>
      </label>
    {/if}
    <RuntimeSelect
      id="memory-reranker"
      label="Reranker"
      value={memoryDraft.reranker}
      options={memory.reranker_options}
      disabled={!canManageSettings}
      onValueChange={(value) => onUpdateMemory('reranker', value)}
    />
  </div>

  <div class="panel-actions">
    <ConstellationButton onclick={onSaveMemory} loading={savingMemory} disabled={!canManageSettings || saveDisabled}>
      Save memory
    </ConstellationButton>
  </div>
</SetupCard>

<style>
  .memory-notices {
    display: grid;
    gap: 18px;
  }

  .memory-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 18px;
  }

  .runtime-static-field {
    display: grid;
    gap: 7px;
    min-width: 0;
    color: var(--constellation-text-muted);
  }

  .runtime-static-field span {
    min-width: 0;
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .runtime-static-field div {
    display: flex;
    min-height: 42px;
    align-items: center;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-control-input-background) 72%, transparent);
    color: var(--constellation-text-primary);
    padding: 0 12px;
  }

  .panel-actions {
    display: flex;
    justify-content: flex-end;
  }

  @media (max-width: 980px) {
    .memory-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
