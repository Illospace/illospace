import type { Idea } from '$lib/types/cortex';

export const SEEN_IDEA_REVISIONS_STORAGE_KEY = 'illo:cortex:seen-idea-revisions';

export type SeenIdeaRevisionMap = ReadonlyMap<string, string>;

export function ideaRevision(
  idea: Pick<Idea, 'updated_at' | 'created_at'> | null | undefined,
): string {
  return idea?.updated_at || idea?.created_at || '';
}

export function hasSeenRevision(
  seenRevision: string | null | undefined,
  currentRevision: string | null | undefined,
): boolean {
  if (!seenRevision || !currentRevision) return false;
  return new Date(seenRevision).getTime() >= new Date(currentRevision).getTime();
}

export function isIdeaSeen(
  idea: Pick<Idea, 'id' | 'updated_at' | 'created_at'>,
  seenRevisions: SeenIdeaRevisionMap,
): boolean {
  return hasSeenRevision(seenRevisions.get(idea.id), ideaRevision(idea));
}

export function markIdeaSeen(
  seenRevisions: SeenIdeaRevisionMap,
  ideaId: string,
  revision: string,
): Map<string, string> {
  const current = seenRevisions instanceof Map ? seenRevisions : new Map(seenRevisions);
  if (!ideaId || !revision) return current;
  const previousRevision = seenRevisions.get(ideaId);
  if (hasSeenRevision(previousRevision, revision)) return current;
  const next = new Map(seenRevisions);
  next.set(ideaId, revision);
  return next;
}

export function parseSeenIdeaRevisions(raw: string | null | undefined): Map<string, string> {
  if (!raw) return new Map();
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return new Map();
    return new Map(
      Object.entries(parsed as Record<string, unknown>)
        .filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
    );
  } catch {
    return new Map();
  }
}

export function serializeSeenIdeaRevisions(seenRevisions: SeenIdeaRevisionMap): string {
  return JSON.stringify(Object.fromEntries(seenRevisions));
}
