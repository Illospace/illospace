import type { Idea } from '$lib/types/cortex';
import {
  estimateClusterExtent,
  type AttractorLayoutOptions,
} from '$lib/utils/attractors';

export type WorkspaceFieldLayoutOptions = AttractorLayoutOptions;

export const EMPTY_WORKSPACE_LAYOUT_OPTIONS: WorkspaceFieldLayoutOptions = {};

export function computeUserThreadLoadById(ideas: readonly Idea[]): Record<string, number> {
  return ideas.reduce((acc: Record<string, number>, idea: Idea) => {
    if (idea?.archived_at || !idea?.user_id) return acc;
    const rawThreadCount = Number(idea.thread_count ?? 0);
    const normalizedThreadCount = Number.isFinite(rawThreadCount) ? rawThreadCount : 0;
    const ideaLoad = Math.max(1, normalizedThreadCount);
    acc[idea.user_id] = (acc[idea.user_id] || 0) + ideaLoad;
    return acc;
  }, {});
}

export function computeWorkspaceClusterExtentByUser(ideas: readonly Idea[]): Record<string, number> {
  const countsByUserId = ideas.reduce((acc: Record<string, number>, idea: Idea) => {
    if (idea?.archived_at || !idea?.user_id) return acc;
    acc[idea.user_id] = (acc[idea.user_id] || 0) + 1;
    return acc;
  }, {});

  const loadByUserId = computeUserThreadLoadById(ideas);
  const userIds = new Set([
    ...Object.keys(countsByUserId),
    ...Object.keys(loadByUserId),
  ]);

  return Array.from(userIds).reduce((acc: Record<string, number>, userId) => {
    const blobCount = countsByUserId[userId] ?? 0;
    const load = loadByUserId[userId] ?? blobCount;
    acc[userId] = estimateClusterExtent(Math.max(1, blobCount), load);
    return acc;
  }, {});
}

export function createWorkspaceFieldLayoutOptions(
  ideas: readonly Idea[],
  directThreadActive: boolean,
): WorkspaceFieldLayoutOptions {
  if (directThreadActive) return EMPTY_WORKSPACE_LAYOUT_OPTIONS;

  return {
    clusterExtentByUserId: computeWorkspaceClusterExtentByUser(ideas),
    loadByUserId: computeUserThreadLoadById(ideas),
  };
}
