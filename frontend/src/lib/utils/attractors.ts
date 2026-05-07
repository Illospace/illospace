/**
 * attractors.ts — Orbit attractors for Cortex canvas.
 *
 * Team members become astres and workspace pins can join the same gravitational
 * field. Blobs orbit an explicit anchor when present, then fall back to their
 * owner astre.
 *
 * Pure logic module — no rendering. Consumed by the workspace-scene feature.
 */

// ── Types ───────────────────────────────────────────────────

export interface TeamMember {
  id: string;
  name: string;
  color: string;
  email?: string;
}

export interface Attractor {
  id: string;       // unique anchor ID (user ID for astres, pin:<id> for pins)
  kind?: 'user' | 'pin';
  anchorId?: string; // raw persisted ID when id is a namespaced anchor ID
  name: string;     // display name
  color: string;    // hex color
  initial: string;  // first letter
  email?: string;   // for @mention matching by email username
  x: number;
  y: number;
}

export interface OrbitAnchorRef {
  kind: 'user' | 'pin';
  id: string;
  key: string;
}

export interface AttractionTarget {
  x: number;
  y: number;
  suns: Attractor[];  // legacy name; contains the anchors pulling this node
}

export interface AttractorLayoutOptions {
  loadByUserId?: Record<string, number>;
  clusterExtentByUserId?: Record<string, number>;
  loadByAnchorId?: Record<string, number>;
  clusterExtentByAnchorId?: Record<string, number>;
}

export interface AttractorViewportTransform {
  x: number;
  y: number;
  k: number;
}

// ── Configuration (tunable) ─────────────────────────────────

export const ATTRACTOR_CFG = {
  /** Minimum astre-center gap in world pixels; viewport fitting happens later. */
  minAstreSpacing: 500,
  /** Combined load where cluster extent load boost reaches its ceiling */
  dualUserMaxLoad: 18,
  /** Minimum padding between user cluster extents */
  clusterGutter: 260,
  /** Minimum radius for 3+ astre layouts in world pixels. */
  multiUserMinRadius: 320,
  /** Solid sun core radius (px) — matches original CORE_RADIUS */
  coreRadius: 70,
  /** Outer glow radius (px) — warm halo ring */
  glowRadius: 100,
  /** Pulse animation amplitude (px) */
  pulseAmplitude: 5,
  /** Pulse period (ms) */
  pulsePeriod: 4000,
  /** Gravitational repulsion field per sun (px) — prevents bubble overlap */
  repulsionFieldRadius: 200,
  /** Repulsion velocity push strength */
  repulsionStrength: 8,
  /** Baseline tangential orbit speed */
  orbitSpinBase: 0.18,
  /** Extra orbit speed applied while the simulation is energetic */
  orbitSpinBoost: 0.09,
  /** Label offset below sun center (px) */
  labelOffset: 100,
  /** Number of ambient particles per sun */
  particleCount: 14,
} as const;

export type OrbitAnchorKind = NonNullable<Attractor['kind']>;

export interface OrbitAnchorPhysicsProfile {
  threadOrbitBaseRadius: number;
  threadOrbitRingGap: number;
  threadOrbitMinRadius: number;
  repulsionFieldRadius: number;
  repulsionStrength: number;
}

export const ORBIT_ANCHOR_PHYSICS_PROFILES: Record<OrbitAnchorKind, OrbitAnchorPhysicsProfile> = {
  user: {
    threadOrbitBaseRadius: 202,
    threadOrbitRingGap: 84,
    threadOrbitMinRadius: ATTRACTOR_CFG.coreRadius + 44,
    repulsionFieldRadius: ATTRACTOR_CFG.repulsionFieldRadius,
    repulsionStrength: ATTRACTOR_CFG.repulsionStrength,
  },
  pin: {
    threadOrbitBaseRadius: 136,
    threadOrbitRingGap: 64,
    threadOrbitMinRadius: 96,
    repulsionFieldRadius: 116,
    repulsionStrength: ATTRACTOR_CFG.repulsionStrength,
  },
};

export function orbitPhysicsProfileForKind(kind: Attractor['kind'] | null | undefined): OrbitAnchorPhysicsProfile {
  return ORBIT_ANCHOR_PHYSICS_PROFILES[kind === 'pin' ? 'pin' : 'user'];
}

export function orbitPhysicsProfileForAttractor(
  attractor: Pick<Attractor, 'kind'> | null | undefined,
): OrbitAnchorPhysicsProfile {
  return orbitPhysicsProfileForKind(attractor?.kind);
}

// ── Attractor Initialization ────────────────────────────────

/**
 * Position suns in a balanced layout around the canvas center.
 * - 1 member: sun at center (solo mode, replaces old core)
 * - 2 members: binary star, left/right
 * - N members: evenly spaced circle starting from top
 */
function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function easeOutCubic(value: number): number {
  return 1 - Math.pow(1 - value, 3);
}

export function estimateClusterExtent(
  itemCount: number,
  load: number,
  maxBubbleRadius: number = ATTRACTOR_CFG.coreRadius,
): number {
  const normalizedLoad = clamp(load / ATTRACTOR_CFG.dualUserMaxLoad, 0, 1);
  const loadBoost = easeOutCubic(normalizedLoad) * 86;
  const countBoost = Math.sqrt(Math.max(0, itemCount - 1)) * 42;
  return ATTRACTOR_CFG.coreRadius + Math.max(maxBubbleRadius, ATTRACTOR_CFG.coreRadius) + 58 + countBoost + loadBoost;
}

export function getDualUserSpacingDistance(
  _canvasW: number,
  _canvasH: number,
  leftExtent: number,
  rightExtent: number,
): number {
  const requestedSpacing = leftExtent + rightExtent + ATTRACTOR_CFG.clusterGutter;
  return Math.max(requestedSpacing, ATTRACTOR_CFG.minAstreSpacing);
}

function defaultClusterExtentForLoad(load: number): number {
  const normalizedLoad = clamp(load / ATTRACTOR_CFG.dualUserMaxLoad, 0, 1);
  const easedLoad = easeOutCubic(normalizedLoad);
  return estimateClusterExtent(Math.max(1, Math.round(load)), load, ATTRACTOR_CFG.coreRadius) + easedLoad * 14;
}

function extentForLayoutAnchor(anchorId: string, options: AttractorLayoutOptions = {}): number {
  const explicitExtent = options.clusterExtentByAnchorId?.[anchorId] ?? options.clusterExtentByUserId?.[anchorId];
  if (explicitExtent != null) {
    return Math.max(0, explicitExtent);
  }

  return defaultClusterExtentForLoad(Math.max(0, options.loadByAnchorId?.[anchorId] ?? options.loadByUserId?.[anchorId] ?? 0));
}

export function createAttractors(
  members: TeamMember[],
  canvasW: number,
  canvasH: number,
  options: AttractorLayoutOptions = {},
): Attractor[] {
  if (!members || members.length === 0) return [];

  const cx = canvasW / 2;
  const cy = canvasH / 2;
  const loadByUserId = options.loadByUserId ?? {};
  const clusterExtentByUserId = options.clusterExtentByUserId ?? {};
  const extentForUser = (userId: string) => {
    if (clusterExtentByUserId[userId] != null) {
      return Math.max(0, clusterExtentByUserId[userId]!);
    }
    return defaultClusterExtentForLoad(Math.max(0, loadByUserId[userId] ?? 0));
  };

  if (members.length === 2) {
    const spacing = getDualUserSpacingDistance(
      canvasW,
      canvasH,
      extentForUser(members[0].id),
      extentForUser(members[1].id),
    );
    const verticalGap = clamp(spacing * 0.16, 96, 180);
    const horizontalGap = spacing;
    const primaryXOffset = -horizontalGap / 2;
    const secondaryXOffset = horizontalGap / 2;
    const primaryYOffset = verticalGap * 0.125;
    const secondaryYOffset = primaryYOffset - verticalGap;

    return members.map((m, i) => ({
      id: m.id,
      kind: 'user',
      anchorId: m.id,
      name: m.name || m.email || 'Unknown',
      color: m.color || DEFAULT_COLORS[i % DEFAULT_COLORS.length],
      initial: (m.name || m.email || '?')[0].toUpperCase(),
      email: m.email,
      x: cx + (i === 0 ? primaryXOffset : secondaryXOffset),
      y: cy + (i === 0 ? primaryYOffset : secondaryYOffset),
    }));
  }

  const maxExtent = Math.max(
    ...members.map((member) => extentForUser(member.id)),
    ATTRACTOR_CFG.coreRadius + 170,
  );
  const neighborGap = maxExtent * 2 + ATTRACTOR_CFG.clusterGutter;
  const radiusFromGap = neighborGap / (2 * Math.sin(Math.PI / members.length));
  const radius = Math.max(radiusFromGap, ATTRACTOR_CFG.multiUserMinRadius);

  return members.map((m, i) => {
    let x: number, y: number;

    if (members.length === 1) {
      x = cx;
      y = cy - ATTRACTOR_CFG.coreRadius;
    } else {
      // Start from top (-π/2), distribute clockwise
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / members.length;
      x = cx + radius * Math.cos(angle);
      y = cy + radius * Math.sin(angle);
    }

    return {
      id: m.id,
      kind: 'user',
      anchorId: m.id,
      name: m.name || m.email || 'Unknown',
      color: m.color || DEFAULT_COLORS[i % DEFAULT_COLORS.length],
      initial: (m.name || m.email || '?')[0].toUpperCase(),
      email: m.email,
      x,
      y,
    };
  });
}

export function computeAttractorViewportTransform(
  attractors: Attractor[],
  canvasW: number,
  canvasH: number,
  options: AttractorLayoutOptions = {},
): AttractorViewportTransform {
  if (!attractors.length || canvasW <= 0 || canvasH <= 0) {
    return { x: 0, y: 0, k: 1 };
  }

  const padding = Math.min(Math.max(Math.min(canvasW, canvasH) * 0.08, 56), 120);
  const bounds = attractors.reduce(
    (acc, attractor) => {
      const extent = extentForLayoutAnchor(attractor.id, options);
      acc.minX = Math.min(acc.minX, attractor.x - extent);
      acc.maxX = Math.max(acc.maxX, attractor.x + extent);
      acc.minY = Math.min(acc.minY, attractor.y - extent);
      acc.maxY = Math.max(acc.maxY, attractor.y + extent);
      return acc;
    },
    {
      minX: Number.POSITIVE_INFINITY,
      maxX: Number.NEGATIVE_INFINITY,
      minY: Number.POSITIVE_INFINITY,
      maxY: Number.NEGATIVE_INFINITY,
    },
  );

  const contentWidth = Math.max(1, bounds.maxX - bounds.minX);
  const contentHeight = Math.max(1, bounds.maxY - bounds.minY);
  const availableWidth = Math.max(1, canvasW - padding * 2);
  const availableHeight = Math.max(1, canvasH - padding * 2);
  const fitScale = Math.min(availableWidth / contentWidth, availableHeight / contentHeight);
  const maxScale = attractors.length <= 1 ? 1.08 : 1;
  const minScale = 0.25;
  const scale = clamp(fitScale, minScale, maxScale);
  const contentCenterX = (bounds.minX + bounds.maxX) / 2;
  const contentCenterY = (bounds.minY + bounds.maxY) / 2;
  const targetCenterY = canvasW < 900 ? canvasH / 2 : canvasH * 0.43;

  return {
    x: canvasW / 2 - contentCenterX * scale,
    y: targetCenterY - contentCenterY * scale,
    k: scale,
  };
}

// ── Lookup Maps ─────────────────────────────────────────────

export interface AttractorLookup {
  byId: Map<string, Attractor>;
  byName: Map<string, Attractor>;
  byAnchorKey: Map<string, Attractor>;
}

export function orbitAnchorKey(kind: 'user' | 'pin' | null | undefined, id: string | null | undefined): string | null {
  if (!id) return null;
  return kind === 'pin' ? `pin:${id}` : id;
}

export function orbitAnchorRefForAttractor(anchor: Attractor | null | undefined): OrbitAnchorRef | null {
  if (!anchor) return null;
  const kind = anchor.kind === 'pin' ? 'pin' : 'user';
  const id = kind === 'pin' ? anchor.anchorId : (anchor.anchorId || anchor.id);
  const key = orbitAnchorKey(kind, id) ?? anchor.id;
  if (!id || !key) return null;
  return { kind, id, key };
}

export function buildLookup(attractors: Attractor[]): AttractorLookup {
  const byId = new Map<string, Attractor>();
  const byName = new Map<string, Attractor>();
  const byAnchorKey = new Map<string, Attractor>();

  for (const a of attractors) {
    byId.set(a.id, a);
    byAnchorKey.set(a.id, a);
    if (a.kind && a.anchorId) {
      byAnchorKey.set(orbitAnchorKey(a.kind, a.anchorId) ?? a.id, a);
    }
    const firstName = a.name.split(/\s+/)[0].toLowerCase();
    if (firstName) byName.set(firstName, a);
    const full = a.name.toLowerCase().replace(/\s+/g, '');
    if (full && full !== firstName) byName.set(full, a);
    // Also index by email username (part before @) for @mention matching
    if (a.email) {
      const emailUser = a.email.split('@')[0].toLowerCase();
      if (emailUser && !byName.has(emailUser)) byName.set(emailUser, a);
    }
  }

  return { byId, byName, byAnchorKey };
}


export function nearestAttractorWithinRadius(
  x: number,
  y: number,
  attractors: Attractor[],
  radius: number = ATTRACTOR_CFG.coreRadius * 1.35,
): Attractor | null {
  let nearest: Attractor | null = null;
  let nearestDistance = Number.POSITIVE_INFINITY;

  for (const attractor of attractors) {
    const dx = x - attractor.x;
    const dy = y - attractor.y;
    const distance = Math.sqrt(dx * dx + dy * dy);
    if (distance <= radius && distance < nearestDistance) {
      nearest = attractor;
      nearestDistance = distance;
    }
  }

  return nearest;
}

export function handoffTargetWithinRadius(
  x: number,
  y: number,
  attractors: Attractor[],
  currentOwnerId: string | null | undefined,
  radius: number = ATTRACTOR_CFG.coreRadius * 1.35,
): Attractor | null {
  return nearestAttractorWithinRadius(
    x,
    y,
    currentOwnerId ? attractors.filter((attractor) => attractor.id !== currentOwnerId) : attractors,
    radius,
  );
}

export function orbitAnchorTargetWithinRadius(
  x: number,
  y: number,
  attractors: Attractor[],
  currentAnchorKey: string | null | undefined,
  radius: number = ATTRACTOR_CFG.coreRadius * 1.35,
): Attractor | null {
  return nearestAttractorWithinRadius(
    x,
    y,
    currentAnchorKey
      ? attractors.filter((attractor) => orbitAnchorRefForAttractor(attractor)?.key !== currentAnchorKey)
      : attractors,
    radius,
  );
}

// ── Attraction Target Computation ───────────────────────────

/**
 * Determine which orbit anchor attracts a given node.
 *
 * Rules:
 * 1. Explicit orbit_anchor_type/orbit_anchor_id wins when it resolves.
 * 2. Creator's astre attracts when user_id matches a team member.
 * 3. If no anchors match, fall back to the current user's astre.
 * 4. If that is unavailable, return the workspace center.
 *
 * Caches result on `node._attractTarget` for per-tick performance.
 */
export function getAttractionTarget(
  node: any,
  lookup: AttractorLookup,
  fallbackX: number,
  fallbackY: number,
  currentUserId?: string,
): AttractionTarget {
  // Return cache if present
  if (node._attractTarget) return node._attractTarget;

  const suns: Attractor[] = [];
  const seen = new Set<string>();

  const explicitAnchorKey = orbitAnchorKey(node.orbit_anchor_type, node.orbit_anchor_id);
  if (explicitAnchorKey && lookup.byAnchorKey.has(explicitAnchorKey)) {
    const anchor = lookup.byAnchorKey.get(explicitAnchorKey)!;
    suns.push(anchor);
    seen.add(anchor.id);
  }

  // 1. Creator
  if (suns.length === 0 && node.user_id && lookup.byId.has(node.user_id)) {
    const sun = lookup.byId.get(node.user_id)!;
    suns.push(sun);
    seen.add(sun.id);
  }

  // Fallback: if creator ownership is missing, default to current user's sun.
  if (suns.length === 0 && currentUserId && lookup.byId.has(currentUserId)) {
    const mySun = lookup.byId.get(currentUserId)!;
    suns.push(mySun);
    seen.add(mySun.id);
  }

  let target: AttractionTarget;
  if (suns.length === 0) {
    target = { x: fallbackX, y: fallbackY, suns: [] };
  } else {
    const ownerSun = suns[0];
    target = { x: ownerSun.x, y: ownerSun.y, suns };
  }

  node._attractTarget = target;
  return target;
}

/**
 * Clear cached attraction targets (call on data refresh or team change).
 */
export function clearAttractionCache(nodes: any[]) {
  for (const n of nodes) {
    delete n._attractTarget;
  }
}

// ── D3 Force: Multi-Sun Repulsion ───────────────────────────

/**
 * Positional repulsion force that pushes nodes away from all sun cores.
 * Drop-in replacement for the old single `coreRepel` force.
 */
export function multiSunRepulsion(
  nodes: any[],
  attractors: Attractor[],
) {
  if (!attractors || attractors.length === 0) return;

  for (const n of nodes) {
    for (const a of attractors) {
      const { repulsionFieldRadius, repulsionStrength } = orbitPhysicsProfileForAttractor(a);
      const dx = n.x - a.x;
      const dy = n.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      if (dist < repulsionFieldRadius) {
        const proximity = 1 - dist / repulsionFieldRadius;
        const push = proximity * proximity * repulsionStrength;
        // Velocity-only push (no positional correction — avoids jitter)
        n.vx += (dx / dist) * push;
        n.vy += (dy / dist) * push;
      }
    }
  }
}

// ── D3 Force: Multi-Sun Orbit ───────────────────────────────

/**
 * Orbit force that pulls each node toward its creator-owned sun.
 *
 * Composes with existing temporal gravity: recency rank determines
 * how close to the sun the node orbits (recent = near, old = far).
 */
export function multiSunOrbit(
  nodes: any[],
  attractors: Attractor[],
  lookup: AttractorLookup,
  alpha: number,
  fallbackX: number,
  fallbackY: number,
  orbitBands: Record<string, { min: number; max: number }>,
  currentUserId?: string,
) {
  if (!attractors || attractors.length === 0) return;
  const baseStrength = nodes.length <= 3 ? 0.052 : 0.038;

  for (const n of nodes) {
    const target = getAttractionTarget(n, lookup, fallbackX, fallbackY, currentUserId);
    const band = orbitBands[n.status] || { min: 200, max: 350 };
    const targetDist = typeof n._ownerOrbitRadius === 'number'
      ? n._ownerOrbitRadius
      : (() => {
          const rank = n._recencyRank != null ? n._recencyRank : nodes.length;
          const t = Math.min(rank / Math.max(nodes.length, 1), 1);
          return band.min + t * (band.max - band.min);
        })();

    const dx = n.x - target.x;
    const dy = n.y - target.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const diff = dist - targetDist;

    // Radial spring: push/pull node toward target orbit distance
    // Use constant minimum strength so force persists even at low alpha
    const radialStrength = baseStrength * 0.6 + baseStrength * 0.4 * alpha;
    n.vx -= (dx / dist) * diff * radialStrength;
    n.vy -= (dy / dist) * diff * radialStrength;

    // Tangential velocity: create smooth circular orbital motion
    // Perpendicular to radial direction (dx, dy) → tangent is (-dy, dx)
    // Strength is mostly constant (not alpha-dependent) so orbits persist
    const tangentStrength = ATTRACTOR_CFG.orbitSpinBase + ATTRACTOR_CFG.orbitSpinBoost * alpha;
    n.vx += (-dy / dist) * tangentStrength;
    n.vy += (dx / dist) * tangentStrength;
  }
}

// ── Helpers ─────────────────────────────────────────────────

/** Illo (AI agent) distinctive color — exported for cross-module use */
export const ILLO_COLOR = '#00D4AA';

const DEFAULT_COLORS = [
  '#c51f4a', '#c026d3', '#6d28d9', '#4c1d95',
  '#087f5b', '#166534', '#6f8f00', '#9a7b00',
];
