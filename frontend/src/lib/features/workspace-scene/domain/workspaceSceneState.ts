export type ScenePoint = {
  x: number;
  y: number;
};

export type OrbitNodeSceneState = 'free' | 'birth';

export type OrbitNode = {
  id: string;
  title: string;
  display_title?: string;
  description?: string | null;
  status: string;
  salience_score: number;
  position_x: number | null;
  position_y: number | null;
  thread_count?: number;
  attachments?: any[];
  updated_at?: string;
  created_at?: string;
  user_id?: string;
  orbit_anchor_type?: string | null;
  orbit_anchor_id?: string | null;
  author_name?: string;
  author_color?: string;
  user_color?: string;
  x: number;
  y: number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
  _recencyRank?: number;
  _ownerRank?: number;
  _ownerCount?: number;
  _ownerRingIndex?: number;
  _ownerSlotIndex?: number;
  _ownerSlotCount?: number;
  _ownerOrbitRadius?: number;
  _ownerSeedAngle?: number;
  _dragOrigX?: number;
  _dragOrigY?: number;
  _threadAnchorPinned?: boolean;
  _sceneState: OrbitNodeSceneState;
  _birthFromX?: number;
  _birthFromY?: number;
  _birthStartedAt?: number;
  _birthDurationMs?: number;
};

export function orbitNodeCoords(source: Partial<OrbitNode>): ScenePoint | null {
  const x = typeof source.x === 'number' ? source.x : source.position_x;
  const y = typeof source.y === 'number' ? source.y : source.position_y;
  return typeof x === 'number' && typeof y === 'number' ? { x, y } : null;
}

export function createOrbitNodeFromIdea(idea: any, initialCoords: ScenePoint): OrbitNode {
  return {
    id: idea.id,
    title: idea.title,
    display_title: idea.display_title,
    description: idea.description,
    status: idea.status,
    salience_score: idea.salience_score || 5,
    position_x: idea.position_x,
    position_y: idea.position_y,
    thread_count: idea.thread_count || 0,
    attachments: idea.attachments,
    updated_at: idea.updated_at,
    created_at: idea.created_at,
    user_id: idea.user_id,
    orbit_anchor_type: idea.orbit_anchor_type ?? null,
    orbit_anchor_id: idea.orbit_anchor_id ?? null,
    author_name: idea.author_name,
    author_color: idea.author_color,
    user_color: idea.user_color,
    _ownerRank: idea._ownerRank,
    _ownerCount: idea._ownerCount,
    _ownerRingIndex: idea._ownerRingIndex,
    _ownerSlotIndex: idea._ownerSlotIndex,
    _ownerSlotCount: idea._ownerSlotCount,
    _ownerOrbitRadius: idea._ownerOrbitRadius,
    _ownerSeedAngle: idea._ownerSeedAngle,
    x: initialCoords.x,
    y: initialCoords.y,
    fx: null,
    fy: null,
    _sceneState: 'free',
  };
}

export function applyIdeaSnapshotToSceneNode(
  node: OrbitNode,
  idea: any,
  options: { usePersistedCoordsWhenUnpositioned?: boolean } = {},
) {
  const nextCoords = orbitNodeCoords({
    x: idea.position_x ?? undefined,
    y: idea.position_y ?? undefined,
    position_x: idea.position_x,
    position_y: idea.position_y,
  });

  node.title = idea.title;
  node.display_title = idea.display_title;
  node.description = idea.description;
  node.status = idea.status;
  node.salience_score = idea.salience_score;
  node.position_x = idea.position_x;
  node.position_y = idea.position_y;
  node.thread_count = idea.thread_count;
  node.attachments = idea.attachments;
  node.updated_at = idea.updated_at;
  node.created_at = idea.created_at;
  node.user_id = idea.user_id;
  node.orbit_anchor_type = idea.orbit_anchor_type ?? null;
  node.orbit_anchor_id = idea.orbit_anchor_id ?? null;
  node.author_name = idea.author_name;
  node.author_color = idea.author_color;
  node.user_color = idea.user_color;
  node._ownerRank = idea._ownerRank;
  node._ownerCount = idea._ownerCount;
  node._ownerRingIndex = idea._ownerRingIndex;
  node._ownerSlotIndex = idea._ownerSlotIndex;
  node._ownerSlotCount = idea._ownerSlotCount;
  node._ownerOrbitRadius = idea._ownerOrbitRadius;
  node._ownerSeedAngle = idea._ownerSeedAngle;

  if (
    options.usePersistedCoordsWhenUnpositioned
    && nextCoords
    && (!Number.isFinite(node.x) || !Number.isFinite(node.y))
  ) {
    node.x = nextCoords.x;
    node.y = nextCoords.y;
  }
}

export function clearOwnerOrbitLayout(node: Partial<OrbitNode>) {
  delete node._ownerRank;
  delete node._ownerCount;
  delete node._ownerRingIndex;
  delete node._ownerSlotIndex;
  delete node._ownerSlotCount;
  delete node._ownerOrbitRadius;
  delete node._ownerSeedAngle;
}
