<script lang="ts">
  let {
    block,
  }: {
    block: { type: string; content: string; title?: string; language?: string };
  } = $props();

  let load: Promise<void> | null = null;
  let loadError = $state(false);
  let StreamVisualBlockComponent = $state<typeof import('./StreamVisualBlock.svelte').default | null>(null);

  $effect(() => {
    void ensureLoaded();
  });

  function ensureLoaded() {
    if (StreamVisualBlockComponent) return Promise.resolve();
    if (load) return load;

    loadError = false;
    load = import('$lib/features/threads/controllers/threadLazyModuleRegistry')
      .then((registry) => registry.loadStreamVisualBlock())
      .then((module) => {
        StreamVisualBlockComponent = module.default;
      })
      .catch((error: unknown) => {
        console.error('Failed to load thread visual block', error);
        loadError = true;
      })
      .finally(() => {
        load = null;
      });
    return load;
  }
</script>

{#if StreamVisualBlockComponent}
  <StreamVisualBlockComponent {block} />
{:else if loadError}
  <div class="visual-block-load-state is-error" role="alert">
    <span>Visual content could not load.</span>
    <button type="button" onclick={() => void ensureLoaded()}>Retry</button>
  </div>
{:else}
  <div class="visual-block-load-state" role="status">Loading visual content...</div>
{/if}

<style>
  .visual-block-load-state {
    padding: 12px;
    color: var(--thread-message-meta, var(--constellation-color-text-muted));
    font-size: 12px;
  }

  .visual-block-load-state.is-error {
    color: var(--constellation-negative-text, #d4808f);
  }

  .visual-block-load-state button {
    border: 0;
    margin-left: 8px;
    padding: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    text-decoration: underline;
    cursor: pointer;
  }
</style>
