/**
 * Union-Find clustering for idea graph.
 * Groups connected ideas into clusters. Used for galaxy collapse at low zoom.
 */

export interface Cluster {
  id: number;
  nodeIds: string[];
  centroidX: number;
  centroidY: number;
  label: string;
  size: number;
}

export function buildClusters(
  nodes: { id: string; x: number; y: number; title: string }[],
  links: { source: string; target: string }[],
  minClusterSize = 3,
): Cluster[] {
  if (nodes.length === 0) return [];

  // Union-Find
  const parent = new Map<string, string>();
  const rank = new Map<string, number>();

  function find(x: string): string {
    if (!parent.has(x)) { parent.set(x, x); rank.set(x, 0); }
    if (parent.get(x) !== x) parent.set(x, find(parent.get(x)!));
    return parent.get(x)!;
  }

  function union(a: string, b: string) {
    const ra = find(a), rb = find(b);
    if (ra === rb) return;
    const rankA = rank.get(ra) ?? 0, rankB = rank.get(rb) ?? 0;
    if (rankA < rankB) parent.set(ra, rb);
    else if (rankA > rankB) parent.set(rb, ra);
    else { parent.set(rb, ra); rank.set(ra, rankA + 1); }
  }

  // Init all nodes
  for (const n of nodes) find(n.id);

  // Union connected pairs
  const nodeSet = new Set(nodes.map((n) => n.id));
  for (const l of links) {
    if (nodeSet.has(l.source) && nodeSet.has(l.target)) {
      union(l.source, l.target);
    }
  }

  // Group by root
  const groups = new Map<string, string[]>();
  for (const n of nodes) {
    const root = find(n.id);
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root)!.push(n.id);
  }

  // Build clusters
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  const clusters: Cluster[] = [];
  let idx = 0;

  for (const [, members] of groups) {
    if (members.length < minClusterSize) continue;

    const clusterNodes = members.map((id) => nodeMap.get(id)!).filter(Boolean);
    const cx = clusterNodes.reduce((s, n) => s + n.x, 0) / clusterNodes.length;
    const cy = clusterNodes.reduce((s, n) => s + n.y, 0) / clusterNodes.length;

    // Label: most common word across titles (2+ chars, not stopwords)
    const stops = new Set(['the', 'and', 'for', 'with', 'this', 'that', 'from', 'not', 'but']);
    const words = new Map<string, number>();
    for (const n of clusterNodes) {
      for (const w of n.title.toLowerCase().split(/\W+/)) {
        if (w.length > 2 && !stops.has(w)) words.set(w, (words.get(w) ?? 0) + 1);
      }
    }
    let label = 'cluster';
    let maxCount = 0;
    for (const [w, c] of words) {
      if (c > maxCount) { maxCount = c; label = w; }
    }

    clusters.push({ id: idx++, nodeIds: members, centroidX: cx, centroidY: cy, label, size: members.length });
  }

  return clusters;
}
