import type { Idea } from '$lib/types/cortex';

export type ArchivedIdeaCountsByUser = Record<string, number>;

export type ArchiveCountState = {
  countsByUser: ArchivedIdeaCountsByUser;
  archivedIdeaIds: ReadonlySet<string>;
};

export type ArchiveIdeaIdentity = Pick<Idea, 'id' | 'user_id'>;

export function emptyArchiveCountState(): ArchiveCountState {
  return {
    countsByUser: {},
    archivedIdeaIds: new Set(),
  };
}

export function registerArchivedIdea(
  state: ArchiveCountState,
  idea: ArchiveIdeaIdentity | null | undefined,
): ArchiveCountState {
  if (!idea?.id || !idea.user_id || state.archivedIdeaIds.has(idea.id)) return state;
  const archivedIdeaIds = new Set(state.archivedIdeaIds);
  archivedIdeaIds.add(idea.id);
  return {
    archivedIdeaIds,
    countsByUser: {
      ...state.countsByUser,
      [idea.user_id]: (state.countsByUser[idea.user_id] ?? 0) + 1,
    },
  };
}

export function unregisterArchivedIdea(
  state: ArchiveCountState,
  idea: ArchiveIdeaIdentity | null | undefined,
): ArchiveCountState {
  if (!idea?.id || !idea.user_id || !state.archivedIdeaIds.has(idea.id)) return state;
  const archivedIdeaIds = new Set(state.archivedIdeaIds);
  archivedIdeaIds.delete(idea.id);
  return {
    archivedIdeaIds,
    countsByUser: {
      ...state.countsByUser,
      [idea.user_id]: Math.max(0, (state.countsByUser[idea.user_id] ?? 1) - 1),
    },
  };
}

export function seedArchivedIdeaCounts(
  state: ArchiveCountState,
  ideas: ReadonlyArray<Partial<Idea> & { id?: string; user_id?: string; archived_at?: string | null }>,
): ArchiveCountState {
  return ideas.reduce((next, idea) => {
    if (!idea?.archived_at || !idea.id || !idea.user_id) return next;
    return registerArchivedIdea(next, idea as ArchiveIdeaIdentity);
  }, state);
}

export function archivedIdeaCountForUser(
  countsByUser: ArchivedIdeaCountsByUser,
  userId: string | null | undefined,
): number {
  if (!userId) return 0;
  return countsByUser[userId] ?? 0;
}

export function rememberArchivedIdea<T extends Idea>(
  archivedIdeas: readonly T[],
  idea: T | null | undefined,
  options: { archivedAt?: string; limit?: number } = {},
): T[] {
  if (!idea?.id) return [...archivedIdeas];
  const archivedAt = options.archivedAt || new Date().toISOString();
  const limit = options.limit ?? 12;
  const archived = {
    ...idea,
    status: 'archived',
    archived_at: archivedAt,
  } as T;
  return [
    archived,
    ...archivedIdeas.filter((entry) => entry.id !== idea.id),
  ].slice(0, limit);
}
