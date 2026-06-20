<script lang="ts">
  import { ConstellationButton, ConstellationNotice, ConstellationTextInput } from '$lib/components/constellation';

  import RuntimeSelect from './RuntimeSelect.svelte';
  import type { MemoryCheck, MemoryDraft, MemoryNoticeState, RuntimeOption, RuntimeSettings } from './types';

  let {
    memory,
    memoryDraft,
    embeddingModelOptions,
    vaultKeyOptions,
    selectedVaultKey,
    memoryApiKey,
    notice,
    memoryCheck,
    canManageSettings,
    vaultLoading,
    syncingVaultKey,
    savingMemoryApiKey,
    onUpdateMemory,
    onSelectVaultKey,
    onMemoryApiKeyChange,
    onSaveMemoryApiKey,
  }: {
    memory: RuntimeSettings['memory'];
    memoryDraft: MemoryDraft;
    embeddingModelOptions: RuntimeSettings['memory']['embedding_model_options'];
    vaultKeyOptions: RuntimeOption[];
    selectedVaultKey: string;
    memoryApiKey: string;
    notice: MemoryNoticeState | null;
    memoryCheck: MemoryCheck | null;
    canManageSettings: boolean;
    vaultLoading: boolean;
    syncingVaultKey: boolean;
    savingMemoryApiKey: boolean;
    onUpdateMemory: (key: keyof MemoryDraft, value: string) => void;
    onSelectVaultKey: (value: string) => void;
    onMemoryApiKeyChange: (value: string) => void;
    onSaveMemoryApiKey: () => void;
  } = $props();

  const localModelLabel = $derived(memoryDraft.embedder === 'local_cpu' ? 'Local CPU model' : 'Local GPU model');
  const usesApiModel = $derived(memoryDraft.embedder === 'openai' || memoryDraft.embedder === 'gemini');
  const showReranker = $derived(memory.reranker_options.length > 1);
  const memoryKeyLabel = $derived(memoryDraft.embedder === 'gemini' ? 'Gemini key' : 'OpenAI key');
  const memoryKeyPlaceholder = $derived(memoryDraft.embedder === 'gemini' ? 'AIza...' : 'sk-...');
  const memoryKeyConnected = $derived(Boolean(memory.api_key_statuses?.[memoryDraft.embedder === 'gemini' ? 'gemini' : 'openai']));
</script>

<section class="runtime-section memory-runtime" aria-labelledby="memory-runtime-heading">
  <header class="runtime-section-heading">
    <div>
      <h2 id="memory-runtime-heading">Memory & retrieval</h2>
      <p>Configure embeddings and retrieval checks.</p>
    </div>
  </header>

  <div class="memory-stack">
    {#if notice}
      <ConstellationNotice title={notice.title} description={notice.detail || ''} tone={notice.tone} compact />
    {/if}

    {#if memoryCheck}
      <ConstellationNotice
        title={memoryCheck.status === 'ok' ? 'Memory check passed.' : 'Memory check failed.'}
        description={`${memoryCheck.detail}${memoryCheck.duration_ms ? ` (${memoryCheck.duration_ms} ms)` : ''}`}
        tone={memoryCheck.status === 'ok' ? 'success' : 'danger'}
        compact
      />
    {/if}

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

    {#if usesApiModel}
      <div class="memory-key-row">
        <div class="memory-key-copy">
          <span>{memoryKeyLabel}</span>
          <strong>{memoryKeyConnected ? 'Connected' : 'Missing'}</strong>
        </div>
        <ConstellationTextInput
          id="memory-api-key"
          type="password"
          placeholder={memoryKeyPlaceholder}
          value={memoryApiKey}
          autocomplete="off"
          mono
          disabled={!canManageSettings || savingMemoryApiKey}
          oninput={(event) => onMemoryApiKeyChange((event.currentTarget as HTMLInputElement).value)}
        />
        <ConstellationButton
          variant="secondary"
          size="sm"
          onclick={onSaveMemoryApiKey}
          loading={savingMemoryApiKey}
          disabled={!canManageSettings || !memoryApiKey.trim() || syncingVaultKey}
        >
          Rotate
        </ConstellationButton>
      </div>
    {/if}
  </div>
</section>

<style>
  .runtime-section {
    display: grid;
    gap: 18px;
    min-width: 0;
    padding: 22px 0;
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

  .runtime-section-heading h2 {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 18px;
    font-weight: 560;
    line-height: 1.2;
    letter-spacing: 0;
  }

  .runtime-section-heading p {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .memory-stack {
    display: grid;
    gap: 16px;
    min-width: 0;
  }

  .memory-flow-grid {
    display: grid;
    gap: 14px;
    min-width: 0;
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
    border-radius: 12px;
    background: color-mix(in srgb, var(--constellation-control-input-background) 72%, transparent);
    color: var(--constellation-text-primary);
    padding: 0 12px;
  }

  .memory-key-row {
    display: grid;
    grid-template-columns: minmax(120px, 0.34fr) minmax(180px, 1fr) auto;
    align-items: center;
    gap: 10px;
    min-width: 0;
    padding-top: 2px;
  }

  .memory-key-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .memory-key-copy span,
  .memory-key-copy strong {
    min-width: 0;
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .memory-key-copy span {
    color: var(--constellation-color-text-secondary);
    font-weight: 700;
  }

  .memory-key-copy strong {
    color: var(--constellation-color-text-primary);
    font-weight: 700;
  }

  @media (max-width: 760px) {
    .memory-key-row {
      grid-template-columns: 1fr;
    }
  }
</style>
