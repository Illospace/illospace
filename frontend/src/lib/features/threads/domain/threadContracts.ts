import type { Idea, StreamItem } from '$lib/types/cortex';
import type { CortexThreadStageTranscriptItem } from './threadTranscriptAdapter';

export type ThreadStageMode = 'closed' | 'opening' | 'open' | 'dismissing';

export type ThreadStreamStatus = 'idle' | 'loading' | 'live' | 'error';

export type ThreadTranscriptBlock = CortexThreadStageTranscriptItem;

export interface ThreadRunSummary {
  id: string;
  status: string;
  active: boolean;
  requiresApproval: boolean;
  startedAt?: string | null;
  completedAt?: string | null;
}

export interface ThreadViewState {
  idea: Idea | null;
  stream: ThreadTranscriptBlock[];
  rawStream: StreamItem[];
  status: ThreadStreamStatus;
  mode: ThreadStageMode;
  activeRuns: number;
  runs: ThreadRunSummary[];
  browserSessionId?: string | null;
  vaultPromptId?: string | null;
  error?: string | null;
}

export interface ThreadStageOrigin {
  x: number | string;
  y: number | string;
}

export interface ThreadStageState {
  mode: ThreadStageMode;
  ideaId: string | null;
  origin: ThreadStageOrigin | null;
}

export function threadStreamStatusFromFlags(options: {
  selectedIdeaId?: string | null;
  loading?: boolean;
  hasLiveRuns?: boolean;
  error?: unknown;
}): ThreadStreamStatus {
  if (options.error) return 'error';
  if (options.loading) return 'loading';
  if (options.hasLiveRuns) return 'live';
  return options.selectedIdeaId ? 'idle' : 'idle';
}

export function isThreadStageOpen(mode: ThreadStageMode): boolean {
  return mode === 'opening' || mode === 'open';
}
