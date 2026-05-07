export type CortexOrbitPerformanceProfile = {
  alphaDecay: number;
  alphaMin: number;
  idleAlphaTarget: number;
  collideIterations: number;
  velocityDecay: number;
};

export type CortexOrbitCollisionContext = {
  canvasW: number;
  panelOpen: boolean;
  selectedIdeaId: string | null;
};

export const CORTEX_ORBIT_BANDS: Record<string, { min: number; max: number }> = {
  working: { min: 190, max: 255 },
  idle: { min: 225, max: 295 },
  done: { min: 225, max: 295 },
};

export function cortexOrbitPerformanceProfile(nodeCount: number): CortexOrbitPerformanceProfile {
  if (nodeCount >= 220) {
    return {
      alphaDecay: 0.018,
      alphaMin: 0.003,
      idleAlphaTarget: 0.004,
      collideIterations: 1,
      velocityDecay: 0.38,
    };
  }
  if (nodeCount >= 120) {
    return {
      alphaDecay: 0.012,
      alphaMin: 0.002,
      idleAlphaTarget: 0.006,
      collideIterations: 2,
      velocityDecay: 0.36,
    };
  }
  if (nodeCount >= 60) {
    return {
      alphaDecay: 0.008,
      alphaMin: 0.0015,
      idleAlphaTarget: 0.009,
      collideIterations: 2,
      velocityDecay: 0.35,
    };
  }
  return {
    alphaDecay: 0.0045,
    alphaMin: 0.001,
    idleAlphaTarget: 0.012,
    collideIterations: 3,
    velocityDecay: 0.34,
  };
}

export function cortexOrbitDynamicRadius(
  salience: number | null | undefined,
  title: string | null | undefined,
  threadCount = 0,
): number {
  const base = 22 + ((salience || 5) / 10) * 55;
  const len = (title || '').length;
  const titleMin = 35 + Math.sqrt(len) * 6;
  const mass = (threadCount || 0) * 2;
  const engagementBonus = Math.sqrt(mass) * 4;
  return Math.max(base + engagementBonus, Math.min(titleMin, 130));
}

export function cortexOrbitBubbleCollisionRadius(
  node: {
    id?: string;
    salience_score?: number | null;
    display_title?: string | null;
    title?: string | null;
    thread_count?: number | null;
    status?: string | null;
  },
  context: CortexOrbitCollisionContext,
): number {
  const baseRadius = cortexOrbitDynamicRadius(
    node.salience_score,
    node.display_title || node.title,
    node.thread_count || 0,
  ) + 8;
  const isThreadAnchor = context.panelOpen && node.id === context.selectedIdeaId;
  const anchoredSpace = isThreadAnchor ? (context.canvasW < 900 ? 112 : 156) : 0;
  return baseRadius + anchoredSpace;
}
