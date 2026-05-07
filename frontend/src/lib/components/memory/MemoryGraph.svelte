<script lang="ts">
  import { onMount, onDestroy, untrack } from 'svelte';
  import {
    createMemoryLayout,
    nodeRadius,
    nodeColor,
    nodeGlow,
    TYPE_CONFIG,
    type MemoryNode,
    type MemoryEdge,
    type SimilarityEdge,
    type LayoutResult,
  } from '$lib/utils/d3-memory-layout';

  let {
    nodes = [],
    edges = [],
    similarityEdges = [],
    onselect,
  }: {
    nodes: any[];
    edges: any[];
    similarityEdges?: any[];
    onselect?: (id: number) => void;
  } = $props();

  let container: HTMLDivElement;
  let svg: SVGSVGElement;
  let width = $state(800);
  let height = $state(600);
  let layout = $state<LayoutResult | null>(null);
  let selectedId = $state<string | null>(null);
  let hoveredId = $state<string | null>(null);
  let tick = $state(0);
  let rafHandle: number | null = null;
  let loaded = $state(false);

  // Pan & zoom state
  let viewBox = $state({ x: 0, y: 0, w: 800, h: 600 });
  let isPanning = $state(false);
  let panStart = { x: 0, y: 0, vx: 0, vy: 0 };

  function stopSimulation() {
    if (rafHandle !== null) {
      cancelAnimationFrame(rafHandle);
      rafHandle = null;
    }
    untrack(() => layout)?.simulation.stop();
  }

  function initLayout() {
    if (!nodes.length) return;
    const newLayout = createMemoryLayout(nodes, edges, similarityEdges, width, height);
    viewBox = { x: 0, y: 0, w: width, h: height };

    // Use requestAnimationFrame to batch tick updates — never block the event loop
    newLayout.simulation.on('tick', () => {
      if (rafHandle !== null) return;
      rafHandle = requestAnimationFrame(() => {
        rafHandle = null;
        tick++;
      });
    });

    layout = newLayout;

    // Trigger load animation
    setTimeout(() => { loaded = true; }, 50);
  }

  onMount(() => {
    if (container) {
      const rect = container.getBoundingClientRect();
      width = rect.width || 800;
      height = rect.height || 600;
    }
    initLayout();
  });

  onDestroy(() => {
    stopSimulation();
  });

  // Rebuild when input data changes
  $effect(() => {
    const nodeCount = nodes.length;
    const edgeCount = edges.length;
    const simCount = similarityEdges.length;
    if (nodeCount > 0) {
      untrack(() => {
        stopSimulation();
        loaded = false;
        initLayout();
      });
    }
  });

  function handleNodeClick(node: MemoryNode) {
    selectedId = selectedId === node.id ? null : node.id;
    if (onselect && selectedId) {
      onselect(parseInt(selectedId));
    }
  }

  function handleWheel(e: WheelEvent) {
    e.preventDefault();
    const scale = e.deltaY > 0 ? 1.1 : 0.9;
    const newW = viewBox.w * scale;
    const newH = viewBox.h * scale;
    const rect = svg.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * viewBox.w + viewBox.x;
    const my = ((e.clientY - rect.top) / rect.height) * viewBox.h + viewBox.y;
    viewBox = {
      x: mx - (mx - viewBox.x) * scale,
      y: my - (my - viewBox.y) * scale,
      w: newW,
      h: newH,
    };
  }

  function handlePointerDown(e: PointerEvent) {
    if (e.button !== 0) return;
    isPanning = true;
    panStart = { x: e.clientX, y: e.clientY, vx: viewBox.x, vy: viewBox.y };
  }

  function handlePointerMove(e: PointerEvent) {
    if (!isPanning) return;
    const rect = svg.getBoundingClientRect();
    const dx = ((e.clientX - panStart.x) / rect.width) * viewBox.w;
    const dy = ((e.clientY - panStart.y) / rect.height) * viewBox.h;
    viewBox = { ...viewBox, x: panStart.vx - dx, y: panStart.vy - dy };
  }

  function handlePointerUp() {
    isPanning = false;
  }

  // Resolve edge endpoints (after simulation, source/target are objects)
  function edgeCoords(edge: MemoryEdge | SimilarityEdge) {
    const s = edge.source as MemoryNode;
    const t = edge.target as MemoryNode;
    return { x1: s.x, y1: s.y, x2: t.x, y2: t.y };
  }

  // Type legend entries
  const legendTypes = $derived(
    [...new Set((layout?.nodes ?? []).map((n) => n.memory_type))].sort()
  );

  let selectedNode = $derived(
    selectedId ? layout?.nodes.find((n) => n.id === selectedId) ?? null : null
  );

  // Top-N labels always visible (by salience)
  const TOP_LABEL_COUNT = 15;
  const topLabelIds = $derived(
    new Set(
      [...(layout?.nodes ?? [])]
        .sort((a, b) => b.salience - a.salience)
        .slice(0, TOP_LABEL_COUNT)
        .map((n) => n.id)
    )
  );

  // Use tick to force reactivity on simulation updates
  void tick;
</script>

<div class="graph-container" class:loaded bind:this={container}>
  <!-- Ambient background effects -->
  <div class="bg-grain"></div>
  <div class="bg-glow bg-glow-1"></div>
  <div class="bg-glow bg-glow-2"></div>

  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <svg
    bind:this={svg}
    viewBox="{viewBox.x} {viewBox.y} {viewBox.w} {viewBox.h}"
    class="graph-svg"
    onwheel={handleWheel}
    onpointerdown={handlePointerDown}
    onpointermove={handlePointerMove}
    onpointerup={handlePointerUp}
    onpointerleave={handlePointerUp}
  >
    <defs>
      <!-- Glow filters per type -->
      {#each Object.entries(TYPE_CONFIG) as [type, cfg]}
        <filter id="glow-{type}" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur in="SourceGraphic" stdDeviation="4" result="blur" />
          <feFlood flood-color={cfg.color} flood-opacity="0.6" result="color" />
          <feComposite in="color" in2="blur" operator="in" result="glow" />
          <feMerge>
            <feMergeNode in="glow" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <radialGradient id="halo-{type}" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color={cfg.color} stop-opacity="0.35" />
          <stop offset="60%" stop-color={cfg.color} stop-opacity="0.08" />
          <stop offset="100%" stop-color={cfg.color} stop-opacity="0" />
        </radialGradient>
      {/each}

      <!-- Subtle edge gradient -->
      <linearGradient id="edge-grad" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="rgba(255,255,255,0.12)" />
        <stop offset="50%" stop-color="rgba(255,255,255,0.06)" />
        <stop offset="100%" stop-color="rgba(255,255,255,0.12)" />
      </linearGradient>
    </defs>

    {#if layout}
      <!-- Similarity edges (very subtle) -->
      <g class="edges-similarity" opacity={loaded ? 0.5 : 0}>
        {#each layout.similarityEdges as edge}
          {@const c = edgeCoords(edge)}
          {@const sim = typeof edge.similarity === 'number' ? edge.similarity : 0.5}
          <line
            x1={c.x1} y1={c.y1} x2={c.x2} y2={c.y2}
            stroke="rgba(120,140,180,{0.03 + sim * 0.08})"
            stroke-width={0.5 + sim * 0.5}
            stroke-dasharray={sim > 0.7 ? 'none' : '2,4'}
          />
        {/each}
      </g>

      <!-- Relationship edges -->
      <g class="edges-relationship" opacity={loaded ? 0.6 : 0}>
        {#each layout.edges as edge}
          {@const c = edgeCoords(edge)}
          <line
            x1={c.x1} y1={c.y1} x2={c.x2} y2={c.y2}
            stroke="rgba(255,255,255,0.07)"
            stroke-width="0.8"
          />
        {/each}
      </g>

      <!-- Nodes -->
      <g class="nodes">
        {#each layout.nodes as node, i (node.id)}
          {@const r = nodeRadius(node.salience)}
          {@const color = nodeColor(node.memory_type)}
          {@const glow = nodeGlow(node.memory_type)}
          {@const isSelected = selectedId === node.id}
          {@const isHovered = hoveredId === node.id}
          {@const showLabel = isSelected || isHovered || topLabelIds.has(node.id)}

          <g
            class="node-group"
            class:node-loaded={loaded}
            style="--delay: {Math.min(i * 8, 600)}ms; --color: {color}"
            transform="translate({node.x},{node.y})"
            onclick={() => handleNodeClick(node)}
            onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handleNodeClick(node); } }}
            onpointerenter={() => (hoveredId = node.id)}
            onpointerleave={() => (hoveredId = null)}
            role="button"
            tabindex="0"
          >
            <!-- Ambient halo (always visible, subtle) -->
            <circle
              r={r * 3}
              fill="url(#halo-{node.memory_type})"
              opacity={isSelected ? 0.8 : isHovered ? 0.6 : 0.25}
              class="node-halo"
            />

            <!-- Main node circle -->
            <circle
              {r}
              fill={color}
              opacity={isSelected ? 1 : isHovered ? 0.9 : 0.7}
              filter={isSelected || isHovered ? `url(#glow-${node.memory_type})` : 'none'}
              class="node-core"
            />

            <!-- Inner highlight -->
            <circle
              r={r * 0.4}
              fill="white"
              opacity={isSelected ? 0.3 : isHovered ? 0.2 : 0.1}
              cy={-r * 0.15}
            />

            <!-- Selection ring -->
            {#if isSelected}
              <circle
                r={r + 3}
                fill="none"
                stroke={color}
                stroke-width="1.5"
                stroke-dasharray="3,3"
                opacity="0.7"
                class="select-ring"
              />
            {/if}

            <!-- Label -->
            {#if showLabel}
              <g class="node-label-group" opacity={isSelected || isHovered ? 1 : 0.7}>
                <!-- Background pill -->
                <rect
                  x={-Math.min(node.title.length, 30) * 3.2}
                  y={r + 6}
                  width={Math.min(node.title.length, 30) * 6.4}
                  height="16"
                  rx="4"
                  fill="rgba(12,14,24,0.85)"
                  stroke="rgba(255,255,255,0.06)"
                  stroke-width="0.5"
                />
                <text
                  y={r + 18}
                  text-anchor="middle"
                  class="node-label"
                  fill={isSelected || isHovered ? 'rgba(255,255,255,0.9)' : 'rgba(255,255,255,0.55)'}
                >
                  {node.title.length > 30 ? node.title.slice(0, 29) + '…' : node.title}
                </text>
              </g>
            {/if}
          </g>
        {/each}
      </g>
    {/if}
  </svg>

  <!-- Stats bar -->
  <div class="stats-bar">
    <span class="stat">{layout?.nodes.length ?? 0} memories</span>
    <span class="stat-sep">·</span>
    <span class="stat">{layout?.similarityEdges.length ?? 0} similarity links</span>
    <span class="stat-sep">·</span>
    <span class="stat">{layout?.edges.length ?? 0} edges</span>
  </div>

  <!-- Legend -->
  <div class="legend">
    <div class="legend-title">Memory Types</div>
    {#each legendTypes as type}
      <div class="legend-item">
        <span class="legend-dot" style="background: {TYPE_CONFIG[type]?.color ?? '#888'}; box-shadow: 0 0 6px {TYPE_CONFIG[type]?.glow ?? '#8880'}"></span>
        <span class="legend-text">{TYPE_CONFIG[type]?.label ?? type}</span>
      </div>
    {/each}
  </div>

  <!-- Detail panel (slide-in from right) -->
  {#if selectedNode}
    <div class="detail-panel" class:detail-open={!!selectedNode}>
      <div class="detail-header">
        <div class="detail-type-badge" style="background: {nodeColor(selectedNode.memory_type)}20; color: {nodeColor(selectedNode.memory_type)}; border-color: {nodeColor(selectedNode.memory_type)}40">
          {selectedNode.memory_type}
        </div>
        <button class="detail-close" onclick={() => { selectedId = null; }}>✕</button>
      </div>

      <div class="detail-title">{selectedNode.title}</div>

      {#if selectedNode.content}
        <div class="detail-content">
          {selectedNode.content.length > 400
            ? selectedNode.content.slice(0, 397) + '…'
            : selectedNode.content}
        </div>
      {/if}

      <div class="detail-stats">
        <div class="detail-stat">
          <span class="detail-stat-label">Salience</span>
          <div class="detail-stat-bar">
            <div class="detail-stat-fill" style="width: {selectedNode.salience * 10}%; background: {nodeColor(selectedNode.memory_type)}"></div>
          </div>
          <span class="detail-stat-value">{selectedNode.salience}/10</span>
        </div>
        {#if selectedNode.emotion_label}
          <div class="detail-stat">
            <span class="detail-stat-label">Emotion</span>
            <span class="detail-stat-value emotion-tag">{selectedNode.emotion_label}</span>
          </div>
        {/if}
        {#if selectedNode.tags?.length}
          <div class="detail-tags">
            {#each selectedNode.tags as tag}
              <span class="detail-tag">{tag}</span>
            {/each}
          </div>
        {/if}
      </div>

      <div class="detail-meta">
        <span>Accessed {selectedNode.access_count}× </span>
        {#if selectedNode.created_at}
          <span>· Created {new Date(selectedNode.created_at).toLocaleDateString()}</span>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .graph-container {
    position: relative;
    width: 100%;
    height: 100%;
    min-height: 620px;
    background:
      radial-gradient(circle at top left, rgba(129, 173, 255, 0.14) 0%, transparent 24%),
      radial-gradient(circle at 78% 18%, rgba(102, 214, 194, 0.08) 0%, transparent 18%),
      linear-gradient(180deg, rgba(10, 18, 31, 0.96) 0%, rgba(7, 12, 22, 0.98) 100%);
    border-radius: 20px;
    overflow: hidden;
    font-family: inherit;
    opacity: 0;
    transition: opacity 0.45s ease;
    border: 1px solid rgba(255, 255, 255, 0.06);
    box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
  }

  .graph-container.loaded {
    opacity: 1;
  }

  .bg-grain {
    position: absolute;
    inset: 0;
    opacity: 0.018;
    background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
    background-size: 256px 256px;
    pointer-events: none;
    z-index: 1;
  }

  .bg-glow {
    position: absolute;
    border-radius: 50%;
    filter: blur(120px);
    pointer-events: none;
    z-index: 0;
    animation: glow-drift 30s ease-in-out infinite alternate;
  }

  .bg-glow-1 {
    width: 420px;
    height: 420px;
    top: -120px;
    left: -40px;
    background: radial-gradient(circle, rgba(94,156,239,0.08) 0%, transparent 70%);
  }

  .bg-glow-2 {
    width: 360px;
    height: 360px;
    bottom: -100px;
    right: -30px;
    background: radial-gradient(circle, rgba(155,124,232,0.07) 0%, transparent 70%);
    animation-delay: -12s;
  }

  @keyframes glow-drift {
    0% { transform: translate(0, 0) scale(1); }
    100% { transform: translate(35px, 24px) scale(1.06); }
  }

  .graph-svg {
    position: relative;
    z-index: 2;
    width: 100%;
    height: 100%;
    cursor: grab;
  }

  .graph-svg:active {
    cursor: grabbing;
  }

  .edges-similarity,
  .edges-relationship {
    transition: opacity 1s ease 0.3s;
  }

  .node-group {
    cursor: pointer;
    opacity: 0;
    transform-origin: center center;
  }

  .node-group.node-loaded {
    opacity: 1;
    transition: opacity 0.5s ease var(--delay);
  }

  .node-core {
    transition: opacity 0.2s ease, r 0.2s ease;
  }

  .node-halo {
    transition: opacity 0.3s ease;
  }

  @keyframes spin-ring {
    from { stroke-dashoffset: 0; }
    to { stroke-dashoffset: 24; }
  }

  .select-ring {
    animation: spin-ring 3s linear infinite;
  }

  .node-label {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 8px;
    font-weight: 500;
    pointer-events: none;
    user-select: none;
    letter-spacing: 0.03em;
  }

  .node-label-group {
    transition: opacity 0.2s ease;
  }

  .stats-bar {
    position: absolute;
    top: 18px;
    left: 18px;
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(9, 13, 22, 0.72);
    backdrop-filter: blur(14px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    padding: 9px 14px;
    z-index: 10;
    box-shadow: 0 10px 24px rgba(0,0,0,0.18);
  }

  .stat {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
    color: rgba(255, 255, 255, 0.66);
    letter-spacing: 0.03em;
    text-transform: uppercase;
  }

  .stat-sep {
    color: rgba(255, 255, 255, 0.18);
    font-size: 10px;
  }

  .legend {
    position: absolute;
    top: 18px;
    right: 18px;
    display: flex;
    flex-direction: column;
    gap: 7px;
    background: rgba(9, 13, 22, 0.72);
    backdrop-filter: blur(14px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 16px;
    padding: 12px 14px;
    z-index: 10;
    min-width: 148px;
    box-shadow: 0 10px 24px rgba(0,0,0,0.18);
  }

  .legend-title {
    font-size: 9px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: rgba(255, 255, 255, 0.4);
    margin-bottom: 2px;
  }

  .legend-item {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .legend-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .legend-text {
    font-size: 11px;
    color: rgba(255, 255, 255, 0.7);
    font-weight: 500;
  }

  .detail-panel {
    position: absolute;
    right: 18px;
    bottom: 18px;
    width: min(340px, calc(100% - 36px));
    background: rgba(8, 12, 21, 0.84);
    border: 1px solid rgba(255, 255, 255, 0.09);
    border-radius: 18px;
    padding: 18px;
    color: rgba(255, 255, 255, 0.88);
    backdrop-filter: blur(18px);
    z-index: 20;
    box-shadow: 0 18px 42px rgba(0, 0, 0, 0.28);
    animation: slide-in 0.22s ease-out;
  }

  @keyframes slide-in {
    from {
      opacity: 0;
      transform: translateY(18px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .detail-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
  }

  .detail-type-badge {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid;
  }

  .detail-close {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.08);
    color: rgba(255, 255, 255, 0.55);
    border-radius: 999px;
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .detail-close:hover {
    background: rgba(255, 255, 255, 0.1);
    color: rgba(255, 255, 255, 0.9);
  }

  .detail-title {
    font-size: 17px;
    font-weight: 650;
    margin-bottom: 12px;
    line-height: 1.35;
    letter-spacing: -0.02em;
  }

  .detail-content {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    color: rgba(255, 255, 255, 0.66);
    line-height: 1.8;
    margin-bottom: 18px;
    padding: 12px 13px;
    background: rgba(255, 255, 255, 0.03);
    border-radius: 14px;
    border: 1px solid rgba(255, 255, 255, 0.05);
  }

  .detail-stats {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-bottom: 14px;
  }

  .detail-stat {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .detail-stat-label {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: rgba(255, 255, 255, 0.4);
    min-width: 60px;
  }

  .detail-stat-bar {
    flex: 1;
    height: 4px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 999px;
    overflow: hidden;
  }

  .detail-stat-fill {
    height: 100%;
    border-radius: 999px;
    transition: width 0.3s ease;
  }

  .detail-stat-value {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    color: rgba(255, 255, 255, 0.56);
    min-width: 35px;
    text-align: right;
  }

  .emotion-tag {
    padding: 3px 8px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 999px;
    font-size: 10px;
  }

  .detail-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 4px;
  }

  .detail-tag {
    font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 10px;
    color: rgba(255, 255, 255, 0.5);
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 999px;
    padding: 3px 8px;
  }

  .detail-meta {
    font-size: 10px;
    color: rgba(255, 255, 255, 0.28);
    padding-top: 12px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }

  @media (max-width: 768px) {
    .graph-container {
      min-height: 420px;
    }

    .stats-bar,
    .legend {
      position: static;
      margin: 16px;
      width: calc(100% - 32px);
    }

    .legend {
      min-width: auto;
    }

    .detail-panel {
      position: static;
      width: auto;
      margin: 16px;
    }
  }
</style>
