import { buildCortexThreadHref } from '../../threads/domain/threadLinks.ts';

export type ThreadRouteSelectionDecision =
  | { action: 'idle'; directThreadUrlPending: false }
  | { action: 'already-open'; ideaId: string; directThreadUrlPending: false }
  | { action: 'skip-repeat' }
  | { action: 'load-direct'; ideaId: string };

export interface ThreadRouteSelectionInput {
  requestedIdeaId?: string | null;
  selectedIdeaId?: string | null;
  panelOpen: boolean;
  lastRequestedIdeaId?: string | null;
}

export function decideThreadRouteSelection({
  requestedIdeaId,
  selectedIdeaId,
  panelOpen,
  lastRequestedIdeaId,
}: ThreadRouteSelectionInput): ThreadRouteSelectionDecision {
  if (!requestedIdeaId) return { action: 'idle', directThreadUrlPending: false };
  if (panelOpen && selectedIdeaId === requestedIdeaId) {
    return { action: 'already-open', ideaId: requestedIdeaId, directThreadUrlPending: false };
  }
  if (requestedIdeaId === lastRequestedIdeaId) return { action: 'skip-repeat' };
  return { action: 'load-direct', ideaId: requestedIdeaId };
}

export function buildSyncedThreadRouteHref(
  ideaId: string,
  sourceParams?: URLSearchParams,
): string {
  return buildCortexThreadHref(ideaId, sourceParams);
}
