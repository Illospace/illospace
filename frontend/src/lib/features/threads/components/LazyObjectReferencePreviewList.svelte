<script lang="ts">
  import type { ObjectReferencePayload } from '$lib/api/client';

  let {
    objectReferences = [],
    threadReferences = [],
    compact = false,
    containerClass = 'object-reference-preview-list',
    keyPrefix = '',
  }: {
    objectReferences?: readonly ObjectReferencePayload[] | null;
    threadReferences?: readonly ObjectReferencePayload[] | null;
    compact?: boolean;
    containerClass?: string;
    keyPrefix?: string;
  } = $props();

  let load: Promise<void> | null = null;
  let loadError = $state(false);
  let ObjectReferencePreviewListComponent = $state<
    typeof import('./ObjectReferencePreviewList.svelte').default | null
  >(null);

  const hasReferences = $derived(Boolean(objectReferences?.length || threadReferences?.length));

  $effect(() => {
    if (hasReferences) void ensureLoaded();
  });

  function ensureLoaded() {
    if (ObjectReferencePreviewListComponent) return Promise.resolve();
    if (load) return load;

    loadError = false;
    load = import('$lib/features/threads/controllers/threadLazyModuleRegistry')
      .then((registry) => registry.loadObjectReferencePreviewList())
      .then((module) => {
        ObjectReferencePreviewListComponent = module.default;
      })
      .catch((error: unknown) => {
        console.error('Failed to load thread reference previews', error);
        loadError = true;
      })
      .finally(() => {
        load = null;
      });
    return load;
  }
</script>

{#if hasReferences}
  {#if ObjectReferencePreviewListComponent}
    <ObjectReferencePreviewListComponent
      {objectReferences}
      {threadReferences}
      {compact}
      {containerClass}
      {keyPrefix}
    />
  {:else if loadError}
    <div class="reference-preview-state is-error" role="alert">
      <span>Reference previews could not load.</span>
      <button type="button" onclick={() => void ensureLoaded()}>Retry</button>
    </div>
  {:else}
    <div class="reference-preview-state" role="status">Loading reference previews...</div>
  {/if}
{/if}

<style>
  .reference-preview-state {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
    color: var(--thread-message-meta, var(--constellation-color-text-muted));
    font-size: 11px;
  }

  .reference-preview-state.is-error {
    color: var(--constellation-negative-text, #d4808f);
  }

  .reference-preview-state button {
    border: 0;
    padding: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-decoration: underline;
    cursor: pointer;
  }
</style>
