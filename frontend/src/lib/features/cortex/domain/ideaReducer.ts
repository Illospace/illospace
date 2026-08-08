import type { Idea } from '$lib/types/cortex';
import { DONE_IDEA_STATUSES, WORKING_IDEA_STATUSES } from '../../../constants/statuses.ts';

export type NormalizedIdeaStatus = 'idle' | 'working' | 'done';
export type NormalizedIdea<T extends Idea = Idea> = Omit<T, 'status'> & { status: NormalizedIdeaStatus };

export type NormalizeIdeaOptions = {
  isIdeaSeen?: (idea: Pick<Idea, 'id' | 'updated_at' | 'created_at'>) => boolean;
};

const WORKING_IDEA_STATUS_SET: ReadonlySet<string> = new Set(WORKING_IDEA_STATUSES);
const DONE_IDEA_STATUS_SET: ReadonlySet<string> = new Set(DONE_IDEA_STATUSES);

export function normalizeIdeaStatus(status: string | null | undefined): NormalizedIdeaStatus {
  const value = String(status || '').trim().toLowerCase();
  if (WORKING_IDEA_STATUS_SET.has(value)) return 'working';
  if (DONE_IDEA_STATUS_SET.has(value)) return 'done';
  return 'idle';
}

export function normalizeIdea<T extends Idea>(
  idea: T,
  options: NormalizeIdeaOptions = {},
): NormalizedIdea<T> {
  const sourceStatus = String(idea.status || '').trim().toLowerCase();
  const lifecycleStatus = sourceStatus && sourceStatus !== 'idle' && sourceStatus !== 'done'
    ? sourceStatus
    : idea.lifecycle_status;
  const normalizedStatus = normalizeIdeaStatus(idea.status);
  const status = normalizedStatus === 'done' && options.isIdeaSeen?.(idea)
    ? 'idle'
    : normalizedStatus;
  return {
    ...idea,
    ...(lifecycleStatus ? { lifecycle_status: lifecycleStatus } : {}),
    status,
  } as NormalizedIdea<T>;
}

export function normalizeIdeas<T extends Idea>(
  ideas: readonly T[],
  options: NormalizeIdeaOptions = {},
): Array<NormalizedIdea<T>> {
  return ideas.map((idea) => normalizeIdea(idea, options));
}

export function patchIdeaById<T extends Idea>(
  ideas: readonly T[],
  id: string,
  patch: Partial<Idea>,
): T[] {
  return ideas.map((idea) => (idea.id === id ? { ...idea, ...patch } as T : idea));
}

export function upsertNormalizedIdea<T extends Idea>(
  ideas: readonly T[],
  idea: T,
  options: NormalizeIdeaOptions = {},
): Array<NormalizedIdea<T>> {
  const normalized = normalizeIdea(idea, options);
  const existing = ideas.some((entry) => entry.id === normalized.id);
  const normalizedIdeas = normalizeIdeas(ideas, options);
  if (!existing) return [...normalizedIdeas, normalized];
  return normalizedIdeas.map((entry) =>
    entry.id === normalized.id ? { ...entry, ...normalized } as NormalizedIdea<T> : entry,
  );
}

export function hasWorkingIdeas<T extends Pick<Idea, 'id' | 'status'>>(
  ideas: readonly T[],
  options: { isLocalPreviewIdeaId?: (id: unknown) => boolean } = {},
): boolean {
  return ideas.some((idea) =>
    normalizeIdeaStatus(idea.status) === 'working' && !options.isLocalPreviewIdeaId?.(idea.id),
  );
}
