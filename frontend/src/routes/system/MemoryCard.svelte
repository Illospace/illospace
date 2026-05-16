<script lang="ts">
  import { ConstellationButton, ConstellationNotice } from '$lib/components/constellation';

  import RuntimeSelect from './RuntimeSelect.svelte';
  import SetupCard from './SetupCard.svelte';
  import type { MemoryCheck, MemoryDraft, MemoryNoticeState, RuntimeOption, RuntimeSettings } from './types';

  let {
    description,
    memory,
    memoryDraft,
    embeddingModelOptions,
    vaultKeyOptions,
    selectedVaultKey,
    notice,
    memoryCheck,
    canManageSettings,
    checkingMemory,
    vaultLoading,
    syncingVaultKey,
    onCheckMemory,
    onUpdateMemory,
    onSelectVaultKey,
    onAddApiKey,
    checkDisabled = false,
  }: {
    description: string;
    memory: RuntimeSettings['memory'];
    memoryDraft: MemoryDraft;
    embeddingModelOptions: RuntimeSettings['memory']['embedding_model_options'];
    vaultKeyOptions: RuntimeOption[];
    selectedVaultKey: string;
    notice: MemoryNoticeState | null;
    memoryCheck: MemoryCheck | null;
    canManageSettings: boolean;
    checkingMemory: boolean;
    vaultLoading: boolean;
    syncingVaultKey: boolean;
    onCheckMemory: () => void;
    onUpdateMemory: (key: keyof MemoryDraft, value: string) => void;
    onSelectVaultKey: (value: string) => void;
    onAddApiKey: () => void;
    checkDisabled?: boolean;
  } = $props();

  const localModelLabel = $derived(memoryDraft.embedder === 'local_cpu' ? 'Local CPU model' : 'Local GPU model');
  const usesApiModel = $derived(memoryDraft.embedder === 'openai' || memoryDraft.embedder === 'gemini');
  const showReranker = $derived(memory.reranker_options.length > 1);
</script>

{#snippet addKeyActions()}
  <ConstellationButton variant="secondary" size="sm" onclick={onAddApiKey}>
    Open Vault
  </ConstellationButton>
{/snippet}

<SetupCard
  eyebrow="Memory"
  title="Memory & retrieval"
  {description}
>
  {#snippet actions()}
    <ConstellationButton
      variant="secondary"
      onclick={onCheckMemory}
      loading={checkingMemory}
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

  <div class="memory-flow-grid" class:has-reranker={showReranker}>
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
        id="memory-vault-key"
        label="Vault key"
        value={selectedVaultKey}
        options={vaultKeyOptions}
        disabled={!canManageSettings || vaultLoading || syncingVaultKey}
        onValueChange={onSelectVaultKey}
      />
      <RuntimeSelect
        id="memory-embedding-model"
        label="Embedding model"
        value={memoryDraft.embedding_model}
        options={embeddingModelOptions}
        disabled={!canManageSettings}
        onValueChange={(value) => onUpdateMemory('embedding_model', value)}
      />
    {:else}
      <label class="runtime-static-field" for="memory-local-key">
        <span>Vault key</span>
        <div id="memory-local-key">No key needed</div>
      </label>
      <label class="runtime-static-field" for="memory-local-model">
        <span>Embedding model</span>
        <div id="memory-local-model">{localModelLabel}</div>
      </label>
    {/if}
    {#if showReranker}
      <RuntimeSelect
        id="memory-reranker"
        label="Reranker"
        value={memoryDraft.reranker}
        options={memory.reranker_options}
        disabled={!canManageSettings}
        onValueChange={(value) => onUpdateMemory('reranker', value)}
      />
    {/if}
  </div>

</SetupCard>

<style>
  .memory-notices {
    display: grid;
    gap: 18px;
  }

  .memory-flow-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 18px;
    align-items: end;
    padding-top: 14px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .memory-flow-grid.has-reranker {
    grid-template-columns: repeat(4, minmax(0, 1fr));
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

  @media (max-width: 980px) {
    .memory-flow-grid,
    .memory-flow-grid.has-reranker {
      grid-template-columns: 1fr;
    }
  }
</style>
