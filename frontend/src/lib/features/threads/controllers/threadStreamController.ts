import { cortex } from '$lib/stores/cortex.svelte';
import { theme } from '$lib/stores/theme.svelte';
import { isActiveRun } from '$lib/utils/cortexRunPresentation';
import { buildThreadTranscriptItems } from '../domain/threadStreamAdapter';
import type { ThreadStageMode, ThreadViewState } from '../domain/threadContracts';
import type { BrowserSessionState, Idea, StreamItem, VaultSecretPrompt } from '$lib/types/cortex';

export interface ThreadStoreLike {
  selectedIdeaId: string | null;
  selectedIdea?: Idea | null;
  stream: StreamItem[];
  streamLoading: boolean;
  browserSession?: BrowserSessionState | null;
  vaultSecretPrompt?: VaultSecretPrompt | null;
  approveRun(runId: number): Promise<void> | void;
  denyRun(runId: number): Promise<void> | void;
  sendMessage(content: string, attachments?: any[], options?: any): Promise<void> | void;
  selectIdea(id: string | null): Promise<void> | void;
  loadDirectThread?(id: string): Promise<boolean | void> | boolean | void;
  cancelAll?(): Promise<void> | void;
}

export interface ThreadStreamController {
  viewState(mode?: ThreadStageMode): ThreadViewState;
  select(ideaId: string | null): Promise<void>;
  loadDirect(ideaId: string): Promise<void>;
  sendReply(content: string, attachments?: any[], options?: any): Promise<void>;
  approveRun(runId: number): Promise<void>;
  denyRun(runId: number): Promise<void>;
  cancelAll(): Promise<void>;
}

function runSummary(item: StreamItem) {
  return {
    id: String(item.id),
    status: item.status || 'idle',
    active: isActiveRun(item),
    requiresApproval: Boolean(item.requires_approval),
    startedAt: item.started_at ?? null,
    completedAt: item.completed_at ?? null,
  };
}

export function buildThreadViewState(
  source: ThreadStoreLike,
  mode: ThreadStageMode = source.selectedIdeaId ? 'open' : 'closed',
): ThreadViewState {
  const rawStream = source.stream ?? [];
  const runs = rawStream.filter((item) => item.type === 'run').map(runSummary);
  const activeRuns = runs.filter((run) => run.active).length;
  const idea = source.selectedIdea ?? null;

  return {
    idea,
    rawStream,
    stream: buildThreadTranscriptItems({
      idea,
      stream: rawStream,
      themeMode: theme.mode === 'light' ? 'light' : 'dark',
      onApproveRun: (runId) => void source.approveRun(runId),
      onDenyRun: (runId) => void source.denyRun(runId),
    }),
    status: source.streamLoading ? 'loading' : activeRuns > 0 ? 'live' : 'idle',
    mode,
    activeRuns,
    runs,
    browserSessionId: source.browserSession?.id ?? null,
    vaultPromptId: source.vaultSecretPrompt?.id ?? null,
  };
}

export function createThreadStreamController(
  source: ThreadStoreLike = cortex as unknown as ThreadStoreLike,
): ThreadStreamController {
  return {
    viewState: (mode) => buildThreadViewState(source, mode),
    select: async (ideaId) => {
      await source.selectIdea(ideaId);
    },
    loadDirect: async (ideaId) => {
      if (source.loadDirectThread) {
        await source.loadDirectThread(ideaId);
        return;
      }
      await source.selectIdea(ideaId);
    },
    sendReply: async (content, attachments = [], options = {}) => {
      await source.sendMessage(content, attachments, options);
    },
    approveRun: async (runId) => {
      await source.approveRun(runId);
    },
    denyRun: async (runId) => {
      await source.denyRun(runId);
    },
    cancelAll: async () => {
      await source.cancelAll?.();
    },
  };
}

export const threadStreamController = createThreadStreamController();
