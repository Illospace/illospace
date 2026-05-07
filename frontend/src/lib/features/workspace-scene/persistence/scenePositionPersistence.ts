import type { OrbitNode } from '../domain/workspaceSceneState';

export type SceneIdeaPosition = {
  id: string;
  x: number;
  y: number;
};

export function collectSceneIdeaPositions(
  nodes: OrbitNode[],
  isPreviewIdeaId: (id: unknown) => boolean,
): SceneIdeaPosition[] {
  return nodes
    .filter((idea) => !isPreviewIdeaId(idea?.id) && idea.x != null && idea.y != null)
    .map((idea) => ({ id: idea.id, x: idea.x, y: idea.y }));
}

export function persistSceneIdeaPositions(
  positions: SceneIdeaPosition[],
  fetcher: typeof fetch = fetch,
) {
  if (positions.length <= 0) return Promise.resolve();

  return fetcher('/api/cortex/ideas/positions', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ positions }),
  }).then(() => {});
}
