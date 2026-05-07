/**
 * D3 Memory Layout — semantic proximity force-directed graph.
 *
 * Nodes are positioned by content similarity: memories with similar
 * embeddings cluster together. Type is shown via color only, NOT layout.
 * Similarity edges from pgvector cosine distance drive the force simulation.
 */
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  type Simulation,
} from 'd3-force';

export interface MemoryNode {
  id: string;
  x: number;
  y: number;
  vx?: number;
  vy?: number;
  title: string;
  memory_type: string;
  salience: number;
  content: string;
  emotional_valence: number;
  emotion_label: string | null;
  tags: string[];
  access_count: number;
  created_at: string;
  [key: string]: any;
}

export interface MemoryEdge {
  source: string | MemoryNode;
  target: string | MemoryNode;
  relationship: string;
  weight?: number;
  [key: string]: any;
}

export interface SimilarityEdge {
  source: string | MemoryNode;
  target: string | MemoryNode;
  similarity: number;
}

/** Muted, sophisticated type palette — dark theme optimized */
export const TYPE_CONFIG: Record<string, { color: string; glow: string; label: string }> = {
  lesson:     { color: '#e8645a', glow: '#e8645a40', label: 'Lesson' },
  pattern:    { color: '#d4a853', glow: '#d4a85340', label: 'Pattern' },
  decision:   { color: '#4ecb8d', glow: '#4ecb8d40', label: 'Decision' },
  fact:       { color: '#5e9cef', glow: '#5e9cef40', label: 'Fact' },
  preference: { color: '#9b7ce8', glow: '#9b7ce840', label: 'Preference' },
  episode:    { color: '#7a8899', glow: '#7a889940', label: 'Episode' },
  insight:    { color: '#e89040', glow: '#e8904040', label: 'Insight' },
  emotion:    { color: '#e87aaf', glow: '#e87aaf40', label: 'Emotion' },
};

/** Node radius: salience-proportional, min 8px max 28px */
export function nodeRadius(salience: number): number {
  return 8 + (salience / 10) * 20;
}

export function nodeColor(memoryType: string): string {
  return TYPE_CONFIG[memoryType]?.color ?? TYPE_CONFIG.fact.color;
}

export function nodeGlow(memoryType: string): string {
  return TYPE_CONFIG[memoryType]?.glow ?? TYPE_CONFIG.fact.glow;
}

export interface LayoutResult {
  nodes: MemoryNode[];
  edges: MemoryEdge[];
  similarityEdges: SimilarityEdge[];
  simulation: Simulation<MemoryNode, any>;
}

/** Max nodes to render — prevents browser freeze on large graphs. */
const MAX_NODES = 200;

/**
 * Create a force simulation driven by semantic similarity.
 * Similarity edges attract similar memories; charge force repels all.
 * No type-based clustering — layout is purely content-driven.
 */
export function createMemoryLayout(
  rawNodes: any[],
  rawEdges: any[],
  rawSimilarityEdges: any[],
  width: number,
  height: number,
): LayoutResult {
  // Cap node count: keep highest-salience nodes
  const cappedRaw = rawNodes.length > MAX_NODES
    ? [...rawNodes].sort((a, b) => (b.salience ?? 5) - (a.salience ?? 5)).slice(0, MAX_NODES)
    : rawNodes;

  // Spread nodes from center with random initial positions
  const cx = width / 2;
  const cy = height / 2;
  const spread = Math.min(width, height) * 0.35;

  const nodes: MemoryNode[] = cappedRaw.map((n) => ({
    id: String(n.id ?? n.key ?? Math.random()),
    x: cx + (Math.random() - 0.5) * spread,
    y: cy + (Math.random() - 0.5) * spread,
    title: n.key || n.content?.slice(0, 50) || 'Memory',
    memory_type: n.memory_type || 'fact',
    salience: n.salience ?? 5,
    content: n.content || '',
    emotional_valence: n.emotional_valence ?? n.emotion_valence ?? 0,
    emotion_label: n.emotion_label || null,
    tags: n.tags || [],
    access_count: n.access_count ?? 0,
    created_at: n.created_at || '',
  }));

  const nodeIds = new Set(nodes.map((n) => n.id));

  // Existing relationship edges
  const edges: MemoryEdge[] = rawEdges
    .filter((e: any) => {
      const src = String(e.source_id ?? e.source);
      const tgt = String(e.target_id ?? e.target);
      return src && tgt && nodeIds.has(src) && nodeIds.has(tgt);
    })
    .map((e: any) => ({
      source: String(e.source_id ?? e.source),
      target: String(e.target_id ?? e.target),
      relationship: e.relationship || '',
      weight: e.weight ?? 1,
    }));

  // Similarity edges from pgvector
  const similarityEdges: SimilarityEdge[] = (rawSimilarityEdges || [])
    .filter((e: any) => {
      const src = String(e.source_id ?? e.source);
      const tgt = String(e.target_id ?? e.target);
      return src && tgt && nodeIds.has(src) && nodeIds.has(tgt);
    })
    .map((e: any) => ({
      source: String(e.source_id ?? e.source),
      target: String(e.target_id ?? e.target),
      similarity: e.similarity ?? 0.5,
    }));

  // Use similarity edges as the primary force links
  // Higher similarity = shorter distance = stronger attraction
  const forceLinks = similarityEdges.map((e) => ({
    source: e.source as string,
    target: e.target as string,
    // Map similarity [0.4..1.0] to distance [180..30]
    distance: 180 - (e.similarity as number) * 150,
    strength: 0.15 + (e.similarity as number) * 0.35,
  }));

  // Also add relationship edges with moderate force
  for (const e of edges) {
    forceLinks.push({
      source: e.source as string,
      target: e.target as string,
      distance: 80,
      strength: 0.2,
    });
  }

  const simulation = forceSimulation<MemoryNode>(nodes)
    .force(
      'link',
      forceLink<MemoryNode, any>(forceLinks)
        .id((d) => d.id)
        .distance((d: any) => d.distance)
        .strength((d: any) => d.strength),
    )
    .force('charge', forceManyBody().strength(-180).distanceMax(400))
    .force('center', forceCenter(cx, cy).strength(0.03))
    .force(
      'collide',
      forceCollide<MemoryNode>().radius((d) => nodeRadius(d.salience) + 4).strength(0.7),
    )
    .alphaDecay(0.015)
    .alphaMin(0.001)
    .velocityDecay(0.35);

  return { nodes, edges, similarityEdges, simulation };
}
