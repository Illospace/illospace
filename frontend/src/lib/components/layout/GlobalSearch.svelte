<script lang="ts">
  import { api } from '$lib/api/client';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { goto } from '$app/navigation';

  let { visible = false, onclose }: { visible: boolean; onclose: () => void } = $props();

  let query = $state('');
  let results = $state<any[]>([]);
  let loading = $state(false);
  let selectedIndex = $state(0);
  let debounceTimer: ReturnType<typeof setTimeout>;

  function handleInput(e: Event) {
    const val = (e.target as HTMLInputElement).value;
    query = val;
    selectedIndex = 0;
    clearTimeout(debounceTimer);
    if (!val.trim()) {
      results = [];
      return;
    }
    loading = true;
    debounceTimer = setTimeout(async () => {
      try {
        results = await api.globalSearch(val);
      } catch {
        results = [];
      } finally {
        loading = false;
      }
    }, 250);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      onclose();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIndex = Math.min(selectedIndex + 1, results.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIndex = Math.max(selectedIndex - 1, 0);
    } else if (e.key === 'Enter' && results.length > 0) {
      e.preventDefault();
      selectResult(results[selectedIndex]);
    }
  }

  function selectResult(result: any) {
    if (result.type === 'idea') {
      goto('/cortex');
      setTimeout(() => cortex.selectIdea(result.id), 100);
    } else if (result.type === 'memory') {
      goto(`/memory`);
    } else if (result.type === 'skill') {
      goto(`/skills`);
    }
    onclose();
  }

  const TYPE_ICONS: Record<string, string> = {
    idea: '💡',
    memory: '🧠',
    skill: '⚡',
    emotion: '💭',
  };

  $effect(() => {
    if (visible) {
      query = '';
      results = [];
      selectedIndex = 0;
    }
  });
</script>

{#if visible}
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="search-overlay" onclick={onclose}>
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="search-dialog" onclick={(e) => e.stopPropagation()} onkeydown={handleKeydown}>
      <div class="search-input-row">
        <span class="search-icon">🔍</span>
        <!-- svelte-ignore a11y_autofocus -->
        <input
          type="text"
          class="search-input"
          placeholder="Search ideas, memories, skills..."
          value={query}
          oninput={handleInput}
          autofocus
        />
        <kbd class="search-kbd">Esc</kbd>
      </div>

      {#if loading}
        <div class="search-status">Searching...</div>
      {:else if query && results.length === 0}
        <div class="search-status">No results found</div>
      {:else if results.length > 0}
        <div class="search-results">
          {#each results as result, i (result.id ?? i)}
            <button
              class="search-result"
              class:selected={i === selectedIndex}
              onclick={() => selectResult(result)}
              onmouseenter={() => (selectedIndex = i)}
            >
              <span class="result-icon">{TYPE_ICONS[result.type] ?? '●'}</span>
              <div class="result-body">
                <div class="result-title">{result.title || result.content?.slice(0, 60) || result.name}</div>
                {#if result.snippet}
                  <div class="result-snippet">{result.snippet}</div>
                {/if}
              </div>
              <span class="result-type">{result.type}</span>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .search-overlay {
    position: fixed;
    inset: 0;
    z-index: 300;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 15vh;
    backdrop-filter: blur(4px);
  }

  .search-dialog {
    background: var(--bg-2);
    border: 1px solid var(--border-2);
    border-radius: 12px;
    width: 520px;
    max-width: 90vw;
    box-shadow: 0 16px 48px rgba(0, 0, 0, 0.5);
    overflow: hidden;
    animation: search-in 0.15s ease-out;
  }

  @keyframes search-in {
    from { opacity: 0; transform: scale(0.95); }
    to { opacity: 1; transform: scale(1); }
  }

  .search-input-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--border-1);
  }

  .search-icon { font-size: 16px; }

  .search-input {
    flex: 1;
    background: none;
    border: none;
    color: var(--text-1);
    font-size: var(--text-md);
    font-family: var(--font-sans);
    outline: none;
  }
  .search-input::placeholder { color: var(--text-3); }

  .search-kbd {
    background: var(--bg-3);
    border: 1px solid var(--border-2);
    border-radius: 4px;
    padding: 1px 6px;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-3);
  }

  .search-status {
    padding: 20px;
    text-align: center;
    color: var(--text-3);
    font-size: var(--text-sm);
  }

  .search-results {
    max-height: 340px;
    overflow-y: auto;
    padding: 4px;
  }

  .search-result {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 10px 12px;
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    border-radius: 6px;
    color: var(--text-1);
    font-family: var(--font-sans);
  }
  .search-result:hover,
  .search-result.selected {
    background: var(--bg-3);
  }

  .result-icon { font-size: 16px; flex-shrink: 0; }

  .result-body {
    flex: 1;
    min-width: 0;
  }

  .result-title {
    font-size: var(--text-sm);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .result-snippet {
    font-size: var(--text-xs);
    color: var(--text-3);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    margin-top: 2px;
  }

  .result-type {
    font-size: var(--text-xs);
    color: var(--text-3);
    text-transform: capitalize;
    flex-shrink: 0;
  }
</style>
