<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { ui } from '$lib/stores/ui.svelte';
  import MemoryGraph from '$lib/components/memory/MemoryGraph.svelte';

  const TYPE_COLORS: Record<string, string> = {
    lesson: '#f87171',
    pattern: '#57CFA0',
    fact: '#5b8def',
    episode: '#718096',
    decision: '#34d399',
    preference: '#a78bfa',
    insight: '#57CFA0',
    emotion: '#f472b6',
  };

  // ── State ──────────────────────────────────────────────────────────────
  let nodes = $state<any[]>([]);
  let edges = $state<any[]>([]);
  let similarityEdges = $state<any[]>([]);
  let loading = $state(true);
  let searchTerm = $state('');
  let activeTypes = $state<Set<string>>(new Set());
  let allTypes = $state<string[]>([]);
  let typeCounts = $state<Record<string, number>>({});

  // Detail panel
  let selectedId = $state<number | null>(null);
  let selectedMemory = $state<any | null>(null);
  let neighborhood = $state<any[]>([]);
  let detailLoading = $state(false);

  // Tabs
  let activeTab = $state<'mine' | 'org'>('mine');
  let orgMemories = $state<any[]>([]);
  let orgLoading = $state(false);

  // Stale
  let staleMemories = $state<any[]>([]);
  let staleOpen = $state(false);
  let staleLoading = $state(false);

  // Add form
  let addFormOpen = $state(false);
  let newContent = $state('');
  let newType = $state('fact');
  let newSalience = $state(5);
  let newTags = $state('');
  let submitting = $state(false);

  // Edit state
  let editingId = $state<number | null>(null);
  let editContent = $state('');

  // ── Computed ───────────────────────────────────────────────────────────
  let filteredNodes = $derived.by(() => {
    let result = nodes;
    if (activeTypes.size > 0 && activeTypes.size < allTypes.length) {
      result = result.filter(n => activeTypes.has(n.memory_type));
    }
    if (searchTerm.trim()) {
      const q = searchTerm.toLowerCase();
      result = result.filter(n =>
        (n.content || '').toLowerCase().includes(q) ||
        (n.tags || []).some((t: string) => t.toLowerCase().includes(q))
      );
    }
    return result;
  });

  let filteredEdges = $derived.by(() => {
    const ids = new Set(filteredNodes.map((n: any) => n.id));
    return edges.filter((e: any) => ids.has(e.source_id) && ids.has(e.target_id));
  });

  let sortedMemories = $derived(
    [...filteredNodes].sort((a, b) => (b.salience || 0) - (a.salience || 0))
  );

  let spotlightMemories = $derived(sortedMemories.slice(0, 4));

  let selectedNodeSummary = $derived.by(() => {
    if (!selectedId) return selectedMemory;
    return filteredNodes.find((mem: any) => mem.id === selectedId) || selectedMemory;
  });

  let memoryTypeSummary = $derived.by(() =>
    allTypes
      .map((type) => ({
        type,
        count: filteredNodes.filter((mem: any) => mem.memory_type === type).length,
      }))
      .filter((entry) => entry.count > 0)
      .sort((a, b) => b.count - a.count)
      .slice(0, 5)
  );

  // ── Load ───────────────────────────────────────────────────────────────
  onMount(async () => {
    try {
      // Try similarity endpoint first, fallback to basic graph
      let graph;
      try {
        graph = await api.getGraphSimilarity(250);
        similarityEdges = graph.similarity_edges || [];
      } catch {
        graph = await api.getGraph(250);
        similarityEdges = [];
      }
      nodes = graph.nodes || [];
      edges = graph.edges || [];
      // Build type info
      const types = new Set<string>();
      const counts: Record<string, number> = {};
      for (const n of nodes) {
        const t = n.memory_type || 'fact';
        types.add(t);
        counts[t] = (counts[t] || 0) + 1;
      }
      allTypes = [...types].sort();
      typeCounts = counts;
      activeTypes = new Set(allTypes);
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load memory graph', 'error');
    } finally {
      loading = false;
    }
  });

  // ── Selection ──────────────────────────────────────────────────────────
  async function selectMemory(id: number) {
    if (selectedId === id) {
      selectedId = null;
      selectedMemory = null;
      neighborhood = [];
      return;
    }
    selectedId = id;
    detailLoading = true;
    try {
      const [mem, nbrs] = await Promise.all([
        api.getMemory(id),
        api.memoryNeighborhood(id),
      ]);
      selectedMemory = mem;
      neighborhood = nbrs;
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load memory details', 'error');
      selectedMemory = nodes.find(n => n.id === id) || null;
      neighborhood = [];
    } finally {
      detailLoading = false;
    }
  }

  // ── Type filter toggle ─────────────────────────────────────────────────
  function toggleType(t: string) {
    const next = new Set(activeTypes);
    if (next.has(t)) {
      next.delete(t);
    } else {
      next.add(t);
    }
    activeTypes = next;
  }

  // ── Actions ────────────────────────────────────────────────────────────
  async function confirmMemory(id: number) {
    try {
      const updated = await api.confirmMemory(id);
      patchNode(id, updated);
      if (selectedMemory?.id === id) selectedMemory = updated;
      ui.toast('Memory confirmed', 'success');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to confirm', 'error');
    }
  }

  async function flagMemory(id: number) {
    try {
      const updated = await api.flagMemory(id);
      patchNode(id, updated);
      if (selectedMemory?.id === id) selectedMemory = updated;
      ui.toast('Memory flagged for review', 'info');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to flag', 'error');
    }
  }

  async function promoteMemory(id: number, visibility: string) {
    try {
      const updated = await api.promoteMemory(id, visibility);
      patchNode(id, updated);
      if (selectedMemory?.id === id) selectedMemory = updated;
      ui.toast(`Memory promoted to ${visibility}`, 'success');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to promote', 'error');
    }
  }

  async function saveEdit(id: number) {
    try {
      const updated = await api.patchMemory(id, { content: editContent });
      patchNode(id, updated);
      if (selectedMemory?.id === id) selectedMemory = updated;
      editingId = null;
      editContent = '';
      ui.toast('Memory updated', 'success');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to update', 'error');
    }
  }

  function startEdit(mem: any) {
    editingId = mem.id;
    editContent = mem.content || '';
  }

  // ── Add memory ─────────────────────────────────────────────────────────
  async function submitMemory() {
    if (!newContent.trim()) return;
    submitting = true;
    try {
      const tags = newTags.split(',').map(t => t.trim()).filter(Boolean);
      const mem = await api.createMemory({
        content: newContent,
        memory_type: newType,
        tags: tags.length ? tags : undefined,
      });
      nodes = [mem, ...nodes];
      // Update counts
      const t = mem.memory_type || 'fact';
      typeCounts = { ...typeCounts, [t]: (typeCounts[t] || 0) + 1 };
      if (!allTypes.includes(t)) allTypes = [...allTypes, t].sort();
      activeTypes = new Set([...activeTypes, t]);
      // Reset form
      newContent = '';
      newType = 'fact';
      newSalience = 5;
      newTags = '';
      addFormOpen = false;
      ui.toast('Memory created', 'success');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to create memory', 'error');
    } finally {
      submitting = false;
    }
  }

  // ── Stale ──────────────────────────────────────────────────────────────
  async function loadStale() {
    staleLoading = true;
    try {
      staleMemories = await api.getStale();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load stale memories', 'error');
    } finally {
      staleLoading = false;
    }
  }

  function toggleStale() {
    staleOpen = !staleOpen;
    if (staleOpen && staleMemories.length === 0) loadStale();
  }

  async function archiveStale(id: number) {
    try {
      await api.patchMemory(id, { scope: 'archived' });
      staleMemories = staleMemories.filter(m => m.id !== id);
      nodes = nodes.filter(n => n.id !== id);
      ui.toast('Memory archived', 'success');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to archive', 'error');
    }
  }

  // ── Org tab ────────────────────────────────────────────────────────────
  async function loadOrgMemories() {
    orgLoading = true;
    try {
      orgMemories = await api.orgMemories();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load org memories', 'error');
    } finally {
      orgLoading = false;
    }
  }

  function switchTab(tab: 'mine' | 'org') {
    activeTab = tab;
    if (tab === 'org' && orgMemories.length === 0) loadOrgMemories();
  }

  // ── Helpers ────────────────────────────────────────────────────────────
  function patchNode(id: number, updated: any) {
    nodes = nodes.map(n => (n.id === id ? { ...n, ...updated } : n));
  }

  function formatDate(d: string | null) {
    if (!d) return '—';
    return new Date(d).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  }

  function visIcon(v: string) {
    if (v === 'org') return 'org';
    if (v === 'team') return 'team';
    return 'private';
  }
</script>

<div class="page-header card editorial-hero">
  <div class="hero-copy">
    <div class="eyebrow">Memory atlas</div>
    <h2 class="page-title">Start with the map, then read with focus.</h2>
    <p class="page-subtitle">
      The first atlas iteration puts hierarchy first: a graph hero for orientation, a browse stack for deliberate scanning, and a roomy inspector for reading one memory in context.
    </p>
  </div>

  <div class="hero-metrics" aria-label="Memory overview">
    <div class="hero-metric">
      <span class="hero-metric-label">Visible</span>
      <strong>{filteredNodes.length}</strong>
    </div>
    <div class="hero-metric">
      <span class="hero-metric-label">Total</span>
      <strong>{nodes.length}</strong>
    </div>
    <div class="hero-metric">
      <span class="hero-metric-label">High salience</span>
      <strong>{nodes.filter((mem) => (mem.salience || 0) >= 7).length}</strong>
    </div>
  </div>

  <div class="page-actions">
    <button class="btn btn-sm btn-ghost" onclick={() => toggleStale()}>
      {staleOpen ? 'Hide' : 'Show'} Stale
    </button>
    <button class="btn btn-sm btn-primary" onclick={() => (addFormOpen = !addFormOpen)}>
      + Add Memory
    </button>
  </div>
</div>

<!-- Add memory form -->
{#if addFormOpen}
  <div class="card add-form">
    <div class="card-header">
      <span class="card-title">Add Memory</span>
      <button class="btn btn-xs btn-ghost" onclick={() => (addFormOpen = false)}>x</button>
    </div>
    <div class="add-form-body">
      <textarea
        class="input"
        rows="3"
        placeholder="Memory content (required)"
        bind:value={newContent}
      ></textarea>
      <div class="add-form-row">
        <select class="input" bind:value={newType}>
          <option value="fact">fact</option>
          <option value="lesson">lesson</option>
          <option value="pattern">pattern</option>
          <option value="episode">episode</option>
          <option value="decision">decision</option>
          <option value="preference">preference</option>
          <option value="insight">insight</option>
        </select>
        <label class="salience-label">
          Salience
          <input type="range" min="1" max="10" step="1" bind:value={newSalience} />
          <span>{newSalience}</span>
        </label>
        <input class="input" placeholder="tags (comma-separated)" bind:value={newTags} />
      </div>
      <div class="add-form-actions">
        <button class="btn btn-sm btn-primary" onclick={submitMemory} disabled={submitting || !newContent.trim()}>
          {submitting ? 'Saving...' : 'Save memory'}
        </button>
        <button class="btn btn-sm btn-ghost" onclick={() => (addFormOpen = false)}>Cancel</button>
      </div>
    </div>
  </div>
{/if}

<!-- Stale section -->
{#if staleOpen}
  <div class="card stale-section">
    <div class="card-header">
      <span class="card-title">Stale Memories</span>
      <button class="btn btn-xs btn-ghost" onclick={() => (staleOpen = false)}>x</button>
    </div>
    {#if staleLoading}
      <div class="empty-state">Loading stale memories...</div>
    {:else if staleMemories.length === 0}
      <div class="empty-state">No stale memories found.</div>
    {:else}
      <div class="stale-list">
        {#each staleMemories as mem (mem.id)}
          <div class="stale-item">
            <div class="stale-info">
              <span class="type-badge" style="color: {TYPE_COLORS[mem.memory_type] || '#718096'}">{mem.memory_type}</span>
              <span class="stale-content">{(mem.content || '').slice(0, 100)}</span>
            </div>
            <div class="stale-actions">
              <button class="btn btn-xs btn-danger" onclick={() => archiveStale(mem.id)}>Archive</button>
              <button class="btn btn-xs btn-ghost" onclick={() => { staleMemories = staleMemories.filter(m => m.id !== mem.id); }}>Keep</button>
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<div class="memory-controls card">
  <div class="controls-topline">
    <div>
      <div class="eyebrow">Curate and scan</div>
      <div class="controls-title">Filter the workspace without collapsing it.</div>
    </div>

    <div class="tab-bar">
      <button
        class="tab-btn"
        class:active={activeTab === 'mine'}
        onclick={() => switchTab('mine')}
      >My Brain</button>
      <button
        class="tab-btn"
        class:active={activeTab === 'org'}
        onclick={() => switchTab('org')}
      >Org Brain</button>
    </div>
  </div>

  <div class="toolbar">
    <input
      type="text"
      class="input search-input"
      placeholder="Search memories by content or tags..."
      bind:value={searchTerm}
    />
    <div class="type-filters">
      {#each allTypes as t (t)}
        <button
          class="type-btn"
          class:active={activeTypes.has(t)}
          style="--type-color: {TYPE_COLORS[t] || '#718096'}"
          onclick={() => toggleType(t)}
        >
          {t}
          <span class="type-count">{typeCounts[t] || 0}</span>
        </button>
      {/each}
    </div>
  </div>
</div>

{#if loading}
  <div class="card loading-card">Loading memory graph...</div>
{:else}
  <div class="memory-layout">
    <div class="memory-main">
      {#if activeTab === 'mine'}
        <section class="graph-stage card">
          <div class="section-heading graph-heading">
            <div>
              <div class="eyebrow">Spatial overview</div>
              <h3>Memory atlas</h3>
              <p>Designed for quick scanning: stronger focus in the graph, quieter chrome around it.</p>
            </div>
            <div class="section-meta">
              <span>{filteredEdges.length} explicit links</span>
              <span>{similarityEdges.length} similarity threads</span>
              <span>Drag to pan · Scroll to zoom</span>
            </div>
          </div>

          <div class="graph-wrapper">
            <MemoryGraph nodes={filteredNodes} edges={filteredEdges} {similarityEdges} onselect={selectMemory} />
          </div>
        </section>

        <div class="browse-shell">
          <section class="card browse-rail">
            <div class="section-heading browse-heading">
              <div>
                <div class="eyebrow">Browse guide</div>
                <h3>Read the atlas in layers</h3>
                <p>Start with the graph, skim the most important cards, then open the inspector for the full memory.</p>
              </div>
            </div>

            <div class="browse-section">
              <div class="browse-label">Current focus</div>
              {#if selectedNodeSummary}
                <button class="focus-card" onclick={() => selectMemory(selectedNodeSummary.id)}>
                  <div class="focus-card-top">
                    <span class="type-badge" style="color: {TYPE_COLORS[selectedNodeSummary.memory_type] || '#718096'}">
                      {selectedNodeSummary.memory_type}
                    </span>
                    <span class="memory-id">#{selectedNodeSummary.id}</span>
                  </div>
                  <div class="focus-card-title">Inspector ready</div>
                  <div class="focus-card-copy">{(selectedNodeSummary.content || '').slice(0, 180)}</div>
                </button>
              {:else}
                <div class="focus-empty">Select a node or memory card to pin it here before reading in the inspector.</div>
              {/if}
            </div>

            <div class="browse-section">
              <div class="browse-label">Spotlight memories</div>
              <div class="spotlight-list">
                {#each spotlightMemories as mem (mem.id)}
                  <button class="spotlight-item" onclick={() => selectMemory(mem.id)}>
                    <div class="spotlight-top">
                      <span class="type-badge" style="color: {TYPE_COLORS[mem.memory_type] || '#718096'}">{mem.memory_type}</span>
                      <span class="salience-badge">sal {mem.salience}</span>
                    </div>
                    <div class="spotlight-copy">{(mem.content || '').slice(0, 120)}</div>
                  </button>
                {/each}
              </div>
            </div>

            <div class="browse-section">
              <div class="browse-label">Visible types</div>
              <div class="type-summary-list">
                {#each memoryTypeSummary as entry (entry.type)}
                  <div class="type-summary-item">
                    <div class="type-summary-name">
                      <span class="type-dot" style="--dot-color: {TYPE_COLORS[entry.type] || '#718096'}"></span>
                      <span>{entry.type}</span>
                    </div>
                    <strong>{entry.count}</strong>
                  </div>
                {/each}
              </div>
            </div>
          </section>

          <section class="card memory-list-card">
            <div class="section-heading list-heading">
              <div>
                <div class="eyebrow">Reading stack</div>
                <h3>All memories</h3>
                <p>Skimmable cards stay compact here so the inspector can stay generous.</p>
              </div>
              <span class="count-badge">{sortedMemories.length}</span>
            </div>

            <div class="memory-list">
              {#each sortedMemories as mem (mem.id)}
                <article
                  class="memory-item memory-item-card"
                  class:selected={selectedId === mem.id}
                  class:flagged={(mem.tags || []).includes('needs_review')}
                >
                  {#if editingId === mem.id}
                    <div class="memory-item-header">
                      <span class="type-badge" style="color: {TYPE_COLORS[mem.memory_type] || '#718096'}">
                        {mem.memory_type}
                      </span>
                      <span class="memory-id">#{mem.id}</span>
                      {#if (mem.tags || []).includes('confirmed')}
                        <span class="confirmed-badge">confirmed</span>
                      {/if}
                      <span class="salience-badge">sal {mem.salience}</span>
                    </div>
                    <div class="edit-form">
                      <textarea class="input" rows="3" bind:value={editContent}></textarea>
                      <div class="edit-actions">
                        <button class="btn btn-xs btn-primary" onclick={() => saveEdit(mem.id)}>Save</button>
                        <button class="btn btn-xs btn-ghost" onclick={() => (editingId = null)}>Cancel</button>
                      </div>
                    </div>
                  {:else}
                    <button
                      type="button"
                      class="memory-item-body"
                      aria-pressed={selectedId === mem.id}
                      onclick={() => selectMemory(mem.id)}
                    >
                      <div class="memory-item-header">
                        <span class="type-badge" style="color: {TYPE_COLORS[mem.memory_type] || '#718096'}">
                          {mem.memory_type}
                        </span>
                        <span class="memory-id">#{mem.id}</span>
                        {#if (mem.tags || []).includes('confirmed')}
                          <span class="confirmed-badge">confirmed</span>
                        {/if}
                        <span class="salience-badge">sal {mem.salience}</span>
                      </div>
                      <div class="memory-item-text">{(mem.content || '').slice(0, 220)}</div>
                    </button>
                  {/if}
                  <div class="memory-item-footer">
                    <div class="visibility-label">{visIcon(mem.visibility || 'private')}</div>
                    <div class="memory-item-actions">
                      <button class="btn btn-xs btn-ghost" title="Confirm" onclick={() => confirmMemory(mem.id)}>Confirm</button>
                      <button class="btn btn-xs btn-ghost" title="Flag" onclick={() => flagMemory(mem.id)}>Flag</button>
                      <button class="btn btn-xs btn-ghost" title="Edit" onclick={() => startEdit(mem)}>Edit</button>
                      {#if (mem.salience || 0) >= 7 && (!mem.visibility || mem.visibility === 'private')}
                        <button class="btn btn-xs btn-ghost" title="Promote to org" onclick={() => promoteMemory(mem.id, 'org')}>Promote</button>
                      {/if}
                    </div>
                  </div>
                </article>
              {/each}
              {#if sortedMemories.length === 0}
                <div class="empty-state">No memories match your filters.</div>
              {/if}
            </div>
          </section>
        </div>
      {:else}
        <section class="card org-card">
          <div class="section-heading list-heading">
            <div>
              <div class="eyebrow">Shared intelligence</div>
              <h3>Org brain memories</h3>
              <p>Promoted memories become part of the team’s long-term reading stack.</p>
            </div>
          </div>
          {#if orgLoading}
            <div class="empty-state">Loading org memories...</div>
          {:else if orgMemories.length === 0}
            <div class="empty-state">No org-level memories yet. Promote high-salience memories to share them here.</div>
          {:else}
            <div class="memory-list org-list">
              {#each orgMemories as mem (mem.id)}
                <button class="memory-item" onclick={() => selectMemory(mem.id)}>
                  <div class="memory-item-header">
                    <span class="type-badge" style="color: {TYPE_COLORS[mem.memory_type] || '#718096'}">
                      {mem.memory_type}
                    </span>
                    <span class="memory-id">#{mem.id}</span>
                    <span class="salience-badge">sal {mem.salience}</span>
                  </div>
                  <div class="memory-item-text">{(mem.content || '').slice(0, 220)}</div>
                </button>
              {/each}
            </div>
          {/if}
        </section>
      {/if}
    </div>

    <aside class:selected={!!selectedMemory} class="detail-panel card">
      <div class="section-heading detail-heading">
        <div>
          <div class="eyebrow">Inspector</div>
          <h3>{selectedMemory ? `Memory #${selectedMemory.id}` : 'Select a memory'}</h3>
          <p>
            {selectedMemory
              ? 'Read the full entry, scan metadata, and act without collapsing the rest of the atlas.'
              : 'Choose a node or browse card to open a quieter, roomier read.'}
          </p>
        </div>
        {#if selectedMemory}
          <button class="btn btn-xs btn-ghost" onclick={() => { selectedId = null; selectedMemory = null; }}>x</button>
        {/if}
      </div>

      {#if !selectedMemory}
        <div class="detail-empty">
          <div class="detail-empty-kicker">Nothing selected</div>
          <p>The inspector is intentionally roomy so individual memories feel more legible when you open them.</p>
        </div>
      {:else if detailLoading}
        <div class="empty-state">Loading...</div>
      {:else}
        <div class="detail-body">
          <div class="detail-meta-grid">
            <div class="detail-row">
              <span class="detail-label">Type</span>
              <span class="type-badge" style="color: {TYPE_COLORS[selectedMemory.memory_type] || '#718096'}">
                {selectedMemory.memory_type}
              </span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Salience</span>
              <span>{selectedMemory.salience}</span>
            </div>
            {#if selectedMemory.emotion_label}
              <div class="detail-row">
                <span class="detail-label">Emotion</span>
                <span>{selectedMemory.emotion_label} (v:{selectedMemory.emotion_valence?.toFixed(2)})</span>
              </div>
            {/if}
            <div class="detail-row">
              <span class="detail-label">Created</span>
              <span>{formatDate(selectedMemory.created_at)}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Access count</span>
              <span>{selectedMemory.access_count ?? 0}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Visibility</span>
              <span>{selectedMemory.visibility || 'private'}</span>
            </div>
          </div>

          {#if selectedMemory.tags?.length}
            <div class="detail-section">
              <span class="detail-label">Tags</span>
              <div class="tag-list">
                {#each selectedMemory.tags as tag}
                  <span class="tag">{tag}</span>
                {/each}
              </div>
            </div>
          {/if}

          <div class="detail-section detail-story">
            <span class="detail-label">Content</span>
            <div class="detail-content">{selectedMemory.content}</div>
          </div>

          {#if selectedMemory.source}
            <div class="detail-section">
              <span class="detail-label">Source</span>
              <div class="detail-content source">{selectedMemory.source}</div>
            </div>
          {/if}

          {#if neighborhood.length > 0}
            <div class="detail-section">
              <span class="detail-label">Connections ({neighborhood.length})</span>
              <div class="connections-list">
                {#each neighborhood as edge}
                  <div class="connection-item">
                    <span class="conn-rel">{edge.relationship}</span>
                    <span class="conn-target">
                      #{edge.source_id === selectedMemory.id ? edge.target_id : edge.source_id}
                    </span>
                    <span class="conn-weight">w:{edge.weight?.toFixed(2)}</span>
                  </div>
                {/each}
              </div>
            </div>
          {/if}

          <div class="detail-actions">
            <button class="btn btn-sm btn-ghost" onclick={() => confirmMemory(selectedMemory.id)}>Confirm</button>
            <button class="btn btn-sm btn-ghost" onclick={() => flagMemory(selectedMemory.id)}>Flag</button>
            <button class="btn btn-sm btn-ghost" onclick={() => startEdit(selectedMemory)}>Edit</button>
            {#if (!selectedMemory.visibility || selectedMemory.visibility === 'private')}
              <button class="btn btn-sm btn-primary" onclick={() => promoteMemory(selectedMemory.id, 'org')}>Promote</button>
            {/if}
          </div>
        </div>
      {/if}
    </aside>
  </div>
{/if}

<style>
  :global(.page-content:has(.editorial-hero)) {
    max-width: 1560px;
  }

  .eyebrow {
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: color-mix(in srgb, var(--accent) 68%, white 32%);
  }

  .page-header {
    display: grid;
    grid-template-columns: minmax(0, 1.5fr) auto;
    gap: clamp(1.5rem, 3vw, 2.5rem);
    align-items: start;
    margin-bottom: var(--sp-5);
  }

  .editorial-hero {
    padding: clamp(1.25rem, 2vw, 1.75rem);
    border-radius: 24px;
    background:
      radial-gradient(circle at top left, color-mix(in srgb, var(--accent) 8%, transparent) 0, transparent 30%),
      linear-gradient(180deg, color-mix(in srgb, var(--bg-1) 94%, white 6%), color-mix(in srgb, var(--bg-0) 98%, black 2%));
    border: 1px solid color-mix(in srgb, var(--border-1) 84%, white 16%);
    box-shadow: 0 12px 32px rgba(8, 10, 18, 0.08);
  }

  .hero-copy {
    max-width: 58rem;
    display: grid;
    gap: var(--sp-2);
  }

  .page-title {
    margin: 0;
    font-size: clamp(1.75rem, 3vw, 2.4rem);
    line-height: 1.02;
    letter-spacing: -0.035em;
  }

  .page-subtitle {
    margin: 0;
    max-width: 48rem;
    font-size: 1rem;
    line-height: 1.75;
    color: var(--text-2);
  }

  .hero-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(120px, 1fr));
    gap: var(--sp-2);
    align-self: end;
  }

  .hero-metric {
    display: grid;
    gap: 0.35rem;
    padding: 0.9rem 1rem;
    background: color-mix(in srgb, var(--bg-1) 88%, white 12%);
    border: 1px solid color-mix(in srgb, var(--border-1) 82%, white 18%);
    border-radius: 16px;
  }

  .hero-metric-label {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--text-3);
  }

  .hero-metric strong {
    font-size: clamp(1.35rem, 2vw, 1.9rem);
    font-weight: 650;
    letter-spacing: -0.03em;
  }

  .page-actions {
    grid-column: 1 / -1;
    display: flex;
    justify-content: flex-end;
    gap: var(--sp-2);
  }

  .memory-controls {
    margin-bottom: var(--sp-5);
    padding: clamp(1rem, 2vw, 1.6rem);
    border-radius: 24px;
  }

  .controls-topline {
    display: flex;
    justify-content: space-between;
    gap: var(--sp-4);
    align-items: end;
    margin-bottom: var(--sp-3);
  }

  .controls-title,
  .section-heading h3 {
    margin: 0.35rem 0 0;
    font-size: clamp(1.25rem, 2vw, 1.75rem);
    letter-spacing: -0.03em;
  }

  .section-heading {
    display: flex;
    justify-content: space-between;
    gap: var(--sp-4);
    align-items: start;
    margin-bottom: var(--sp-3);
  }

  .section-heading p {
    margin: 0.45rem 0 0;
    color: var(--text-2);
    line-height: 1.6;
    max-width: 42rem;
  }

  .section-meta {
    align-self: end;
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.55rem;
    font-size: 0.74rem;
    color: var(--text-3);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .section-meta span {
    padding: 0.38rem 0.7rem;
    border-radius: 999px;
    background: color-mix(in srgb, var(--bg-1) 78%, white 22%);
    border: 1px solid color-mix(in srgb, var(--border-1) 84%, white 16%);
  }

  .add-form,
  .stale-section {
    margin-bottom: var(--sp-4);
    border-radius: 22px;
  }

  .add-form-body {
    display: flex;
    flex-direction: column;
    gap: var(--sp-3);
    padding: var(--sp-4);
  }

  .add-form-row {
    display: flex;
    gap: var(--sp-3);
    flex-wrap: wrap;
    align-items: center;
  }

  .add-form-row select { flex: 0 0 160px; }
  .add-form-row input.input { flex: 1; min-width: 200px; }

  .salience-label {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--text-xs);
    color: var(--text-3);
    flex: 1;
    min-width: 220px;
  }

  .salience-label input[type="range"] { flex: 1; }
  .add-form-actions,
  .edit-actions,
  .stale-actions { display: flex; gap: var(--sp-2); flex-wrap: wrap; }

  .stale-list {
    max-height: 280px;
    overflow-y: auto;
    padding: 0 var(--sp-2) var(--sp-2);
  }

  .stale-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1rem 1.1rem;
    border-radius: 16px;
    border: 1px solid var(--border-1);
    background: color-mix(in srgb, var(--bg-1) 85%, white 15%);
    gap: var(--sp-2);
    margin-top: var(--sp-2);
  }

  .stale-info {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    flex: 1;
    min-width: 0;
  }

  .stale-content {
    font-size: 0.86rem;
    color: var(--text-2);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .toolbar {
    display: flex;
    gap: var(--sp-3);
    flex-wrap: wrap;
    align-items: center;
  }

  .search-input { flex: 1; min-width: 280px; }

  .type-filters {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
  }

  .type-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.6rem 0.9rem;
    border: 1px solid color-mix(in srgb, var(--type-color) 45%, var(--border-1));
    border-radius: 999px;
    background: color-mix(in srgb, var(--bg-1) 70%, transparent);
    color: var(--type-color);
    font-size: 0.8rem;
    cursor: pointer;
    opacity: 0.72;
    transition: transform 0.15s ease, opacity 0.15s ease, background 0.15s ease;
  }

  .type-btn:hover {
    transform: translateY(-1px);
    opacity: 1;
  }

  .type-btn.active {
    background: color-mix(in srgb, var(--type-color) 14%, var(--bg-1));
    opacity: 1;
  }

  .type-count {
    font-size: 0.68rem;
    opacity: 0.72;
  }

  .tab-bar {
    display: inline-flex;
    gap: 0.45rem;
    padding: 0.35rem;
    border: 1px solid var(--border-1);
    border-radius: 999px;
    background: color-mix(in srgb, var(--bg-1) 92%, white 8%);
  }

  .tab-btn {
    padding: 0.7rem 1.1rem;
    background: transparent;
    border: none;
    border-radius: 999px;
    color: var(--text-3);
    font-size: 0.9rem;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .tab-btn.active {
    color: var(--text-1);
    background: rgba(255, 255, 255, 0.06);
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.05);
  }

  .loading-card {
    padding: var(--sp-8);
    color: var(--text-3);
    border-radius: 24px;
  }

  .memory-layout {
    display: grid;
    grid-template-columns: minmax(0, 1.7fr) minmax(320px, 420px);
    gap: clamp(1.25rem, 2vw, 2rem);
    align-items: start;
  }

  .memory-main {
    min-width: 0;
    display: grid;
    gap: var(--sp-4);
  }

  .graph-stage,
  .memory-list-card,
  .org-card,
  .detail-panel {
    border-radius: 26px;
    overflow: hidden;
  }

  .graph-stage {
    padding: clamp(0.9rem, 1.7vw, 1.2rem);
    background: linear-gradient(180deg, color-mix(in srgb, var(--bg-1) 96%, white 4%), color-mix(in srgb, var(--bg-0) 98%, black 2%));
    border: 1px solid color-mix(in srgb, var(--border-1) 84%, white 16%);
    box-shadow: 0 16px 38px rgba(8, 10, 18, 0.08);
  }

  .graph-wrapper {
    min-height: 620px;
    border-radius: 20px;
    overflow: hidden;
    background: linear-gradient(180deg, color-mix(in srgb, var(--bg-1) 55%, #101523 45%), color-mix(in srgb, var(--bg-0) 48%, #09111f 52%));
    border: 1px solid color-mix(in srgb, var(--border-1) 76%, rgba(143, 186, 255, 0.22) 24%);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.04), 0 18px 40px rgba(7, 12, 20, 0.18);
  }

  .graph-wrapper :global(.graph-container) {
    height: 100% !important;
    min-height: 620px;
    border-radius: 20px;
  }

  .memory-list-card,
  .org-card {
    padding: clamp(1rem, 2vw, 1.4rem);
  }

  .memory-list {
    display: grid;
    gap: 0.9rem;
    max-height: 820px;
    overflow-y: auto;
    padding-right: 0.3rem;
  }

  .org-list {
    max-height: none;
  }

  .memory-item {
    display: block;
    width: 100%;
    text-align: left;
    padding: 1.2rem 1.25rem;
    border: 1px solid color-mix(in srgb, var(--border-1) 76%, white 24%);
    border-radius: 20px;
    background: linear-gradient(180deg, color-mix(in srgb, var(--bg-1) 94%, white 6%), color-mix(in srgb, var(--bg-0) 96%, black 4%));
    color: inherit;
    cursor: pointer;
    font: inherit;
    transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
  }

  .memory-item:hover {
    transform: translateY(-1px);
    border-color: color-mix(in srgb, var(--accent) 38%, var(--border-1));
    box-shadow: 0 18px 38px rgba(10, 13, 22, 0.12);
  }

  .memory-item.selected {
    border-color: color-mix(in srgb, var(--accent) 65%, white 35%);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 30%, transparent), 0 22px 46px rgba(10, 13, 22, 0.16);
    background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 7%, var(--bg-1)), color-mix(in srgb, var(--bg-0) 97%, var(--accent) 3%));
  }

  .memory-item.flagged {
    border-color: color-mix(in srgb, var(--negative) 55%, var(--border-1));
  }

  .memory-item-card {
    cursor: default;
  }

  .memory-item-body {
    display: block;
    width: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .memory-item-body:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--accent) 72%, white 28%);
    outline-offset: 4px;
    border-radius: 14px;
  }

  .memory-item-header {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    flex-wrap: wrap;
    margin-bottom: 0.7rem;
  }

  .type-badge,
  .memory-id,
  .confirmed-badge,
  .salience-badge {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }

  .type-badge { font-weight: 700; }

  .memory-id {
    color: var(--text-4);
  }

  .confirmed-badge {
    color: var(--positive);
    background: color-mix(in srgb, var(--positive) 12%, transparent);
    padding: 0.2rem 0.5rem;
    border-radius: 999px;
  }

  .salience-badge {
    margin-left: auto;
    color: var(--warning);
  }

  .memory-item-text {
    font-size: 0.98rem;
    color: var(--text-2);
    line-height: 1.75;
    margin-bottom: 1rem;
  }

  .memory-item-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
  }

  .memory-item-actions {
    display: flex;
    gap: var(--sp-1);
    flex-wrap: wrap;
    opacity: 0.72;
    transition: opacity 0.15s ease;
  }

  .memory-item:hover .memory-item-actions,
  .memory-item.selected .memory-item-actions {
    opacity: 1;
  }

  .visibility-label {
    font-size: 0.72rem;
    color: var(--text-4);
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }

  .count-badge {
    color: var(--accent);
    font-size: 1rem;
    font-weight: 700;
  }

  .edit-form {
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    margin-top: var(--sp-2);
  }

  .detail-panel {
    min-height: min(82vh, 980px);
    padding: clamp(1rem, 2vw, 1.5rem);
    position: sticky;
    top: var(--sp-4);
    display: grid;
    align-content: start;
    gap: var(--sp-3);
    background:
      radial-gradient(circle at top, color-mix(in srgb, var(--accent) 10%, transparent), transparent 38%),
      linear-gradient(180deg, color-mix(in srgb, var(--bg-0) 90%, #0f1220 10%), color-mix(in srgb, var(--bg-0) 98%, black 2%));
  }

  .detail-panel:not(.selected) {
    opacity: 0.92;
  }

  .detail-empty {
    min-height: 360px;
    display: grid;
    place-content: center;
    gap: var(--sp-2);
    padding: var(--sp-5);
    border: 1px dashed color-mix(in srgb, var(--border-1) 75%, white 25%);
    border-radius: 22px;
    color: var(--text-2);
    text-align: left;
  }

  .detail-empty-kicker {
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--text-4);
  }

  .detail-body {
    display: grid;
    gap: 1.15rem;
  }

  .detail-meta-grid {
    display: grid;
    gap: 0.8rem;
    padding: 1.1rem 1.15rem;
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 20px;
    background: rgba(255,255,255,0.02);
  }

  .detail-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: var(--sp-3);
    font-size: 0.92rem;
  }

  .detail-label {
    color: var(--text-3);
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
  }

  .detail-section {
    display: grid;
    gap: 0.7rem;
  }

  .detail-story {
    padding: 1.1rem 1.15rem;
    border-radius: 22px;
    background: rgba(255,255,255,0.025);
    border: 1px solid rgba(255,255,255,0.06);
  }

  .detail-content {
    font-size: 0.98rem;
    color: var(--text-2);
    line-height: 1.82;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .detail-content.source {
    color: var(--text-3);
    font-style: italic;
  }

  .tag-list {
    display: flex;
    gap: 0.45rem;
    flex-wrap: wrap;
  }

  .tag {
    font-size: 0.72rem;
    padding: 0.28rem 0.6rem;
    border-radius: 999px;
    background: var(--bg-2);
    color: var(--text-2);
  }

  .connections-list {
    display: grid;
    gap: 0.6rem;
  }

  .connection-item {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: 0.82rem;
    padding: 0.7rem 0.85rem;
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 16px;
  }

  .conn-rel { color: var(--accent); font-weight: 600; }
  .conn-target { color: var(--text-2); }
  .conn-weight { color: var(--text-4); margin-left: auto; }

  .detail-actions {
    display: flex;
    gap: var(--sp-2);
    padding-top: var(--sp-3);
    border-top: 1px solid var(--border-1);
    flex-wrap: wrap;
  }

  .empty-state {
    padding: var(--sp-5);
    text-align: center;
    color: var(--text-3);
    font-size: 0.92rem;
  }

  @media (max-width: 1100px) {
    .page-header,
    .memory-layout,
    .controls-topline {
      grid-template-columns: 1fr;
      display: grid;
    }

    .browse-shell {
      grid-template-columns: 1fr;
    }

    .section-meta {
      justify-content: flex-start;
    }

    .hero-metrics {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .browse-rail,
    .detail-panel {
      position: static;
      min-height: auto;
    }
  }

  @media (max-width: 768px) {
    .editorial-hero,
    .memory-controls,
    .graph-stage,
    .memory-list-card,
    .org-card,
    .detail-panel {
      border-radius: 20px;
    }

    .hero-metrics {
      grid-template-columns: 1fr;
    }

    .graph-wrapper,
    .graph-wrapper :global(.graph-container) {
      min-height: 420px;
    }

    .memory-item,
    .stale-item {
      padding: 1rem;
    }

    .memory-item-footer {
      align-items: flex-start;
    }
  }
</style>
