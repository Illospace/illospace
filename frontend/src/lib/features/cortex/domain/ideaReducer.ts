import type { Idea } from '$lib/types/cortex';

export type NormalizedIdeaStatus = 'idle' | 'working' | 'done';
export type NormalizedIdea<T extends Idea = Idea> = Omit<T, 'status'> & { status: NormalizedIdeaStatus };

export const WORKING_IDEA_STATUSES = [
  'queued',
  'working',
  'running',
] as const;

export const DONE_IDEA_STATUSES = [
  'completed',
  'pending_approval',
  'needs_input',
  'unread_reply',
  'failed',
  'canceled',
  'cancelled',
  'superseded',
  'timeout',
  'blocked',
  'done',
] as const;

export type NormalizeIdeaOptions = {
  isIdeaSeen?: (idea: Pick<Idea, 'id' | 'updated_at' | 'created_at'>) => boolean;
};

export function normalizeIdeaStatus(status: string | null | undefined): NormalizedIdeaStatus {
  switch (status) {
    case 'queued':
    case 'working':
    case 'running':
      return 'working';
    case 'completed':
    case 'pending_approval':
    case 'needs_input':
    case 'unread_reply':
    case 'failed':
    case 'canceled':
    case 'cancelled':
    case 'superseded':
    case 'timeout':
    case 'blocked':
    case 'done':
      return 'done';
    default:
      return 'idle';
  }
}

export function normalizeIdea<T extends Idea>(
  idea: T,
  options: NormalizeIdeaOptions = {},
): NormalizedIdea<T> {
  const normalizedStatus = normalizeIdeaStatus(idea.status);
  const status = normalizedStatus === 'done' && options.isIdeaSeen?.(idea)
    ? 'idle'
    : normalizedStatus;
  return { ...idea, status } as NormalizedIdea<T>;
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
