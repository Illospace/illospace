import { isActiveRun, mergeRunProgressSnapshot } from '$lib/utils/cortexRunPresentation';
import {
  applyAgentActivityToStream,
  applyAgentTextDeltaToStream,
  applyRunCompletedToStream,
  mergeLiveStreamState,
  runUiEventKey,
  type CortexRunStreamItem,
} from '$lib/utils/cortexRunStream';
import type { StreamItem } from '$lib/types/cortex';

export type ThreadRunUiEventType =
  | 'run_started'
  | 'step_started'
  | 'tool_started'
  | 'tool_finished'
  | 'text_delta'
  | 'run_completed';

export interface ThreadRunUiEvent {
  type: ThreadRunUiEventType | string;
  idea_id?: string | null;
  run_id?: string | number | null;
  root_run_id?: string | number | null;
  label?: string | null;
  activity?: string | null;
  tool_name?: string | null;
  status?: string | null;
  strategy?: string | null;
  profile?: string | null;
  [key: string]: unknown;
}

export interface ThreadRunEventReducerState {
  stream: StreamItem[];
  selectedIdeaId: string | null;
  executionProfile?: string;
  seenEventKeys?: ReadonlySet<string>;
}

export interface ThreadRunEventReducerResult {
  stream: StreamItem[];
  seenEventKeys: Set<string>;
  handled: boolean;
  shouldRefreshStream: boolean;
  shouldEnsureReconcile: boolean;
  shouldStopReconcile: boolean;
  shouldRefreshBrowserSession: boolean;
}

export function isRootRunUiEvent(msg: Pick<ThreadRunUiEvent, 'run_id' | 'root_run_id'>): boolean {
  const runId = msg?.run_id;
  const rootRunId = msg?.root_run_id;
  if (runId == null || runId === '' || rootRunId == null || rootRunId === '') return true;
  return String(runId) === String(rootRunId);
}

export function activeThreadRunCount(stream: readonly StreamItem[]): number {
  return stream.filter((item) => item.type === 'run' && isActiveRun(item)).length;
}

export function mergeThreadStreamState(options: {
  loadedItems: readonly StreamItem[];
  liveStream: readonly StreamItem[];
  ideaId: string | null;
}): StreamItem[] {
  return mergeLiveStreamState(
    [...options.loadedItems] as CortexRunStreamItem[],
    [...options.liveStream] as CortexRunStreamItem[],
    options.ideaId,
    (item) => item.type === 'run' && isActiveRun(item),
    mergeRunProgressSnapshot,
  ) as StreamItem[];
}

export function dedupeRunUiEvent(
  msg: ThreadRunUiEvent,
  seenEventKeys: ReadonlySet<string> = new Set(),
): { duplicate: boolean; nextSeenEventKeys: Set<string>; eventKey: string | null } {
  const nextSeenEventKeys = new Set(seenEventKeys);
  const eventKey = runUiEventKey(msg);
  if (!eventKey) return { duplicate: false, nextSeenEventKeys, eventKey: null };
  if (nextSeenEventKeys.has(eventKey)) {
    return { duplicate: true, nextSeenEventKeys, eventKey };
  }
  nextSeenEventKeys.add(eventKey);
  return { duplicate: false, nextSeenEventKeys, eventKey };
}

export function reduceRunUiEventToThreadStream(
  state: ThreadRunEventReducerState,
  msg: ThreadRunUiEvent,
): ThreadRunEventReducerResult {
  const seen = state.seenEventKeys ?? new Set<string>();
  const { duplicate, nextSeenEventKeys } = dedupeRunUiEvent(msg, seen);
  const base: Omit<ThreadRunEventReducerResult, 'stream'> = {
    seenEventKeys: nextSeenEventKeys,
    handled: false,
    shouldRefreshStream: false,
    shouldEnsureReconcile: false,
    shouldStopReconcile: false,
    shouldRefreshBrowserSession: false,
  };

  if (duplicate || !msg?.idea_id || msg.idea_id !== state.selectedIdeaId || !isRootRunUiEvent(msg)) {
    return { ...base, stream: state.stream };
  }

  if (msg.type === 'text_delta') {
    return {
      ...base,
      handled: true,
      stream: applyAgentTextDeltaToStream(
        state.stream as CortexRunStreamItem[],
        msg,
        state.selectedIdeaId,
      ) as StreamItem[],
    };
  }

  if (msg.type === 'run_started') {
    return {
      ...base,
      handled: true,
      shouldEnsureReconcile: true,
      stream: applyAgentActivityToStream(
        state.stream as CortexRunStreamItem[],
        { ...msg, activity: msg.label || 'Started' },
        state.selectedIdeaId,
        state.executionProfile || 'fast',
      ) as StreamItem[],
    };
  }

  if (msg.type === 'step_started') {
    return {
      ...base,
      handled: true,
      shouldEnsureReconcile: true,
      shouldRefreshBrowserSession: true,
      stream: applyAgentActivityToStream(
        state.stream as CortexRunStreamItem[],
        { ...msg, activity: msg.label || msg.activity || 'Working' },
        state.selectedIdeaId,
        state.executionProfile || 'fast',
      ) as StreamItem[],
    };
  }

  if (msg.type === 'tool_started') {
    const toolName = typeof msg.tool_name === 'string' ? msg.tool_name.trim() : '';
    return {
      ...base,
      handled: true,
      shouldEnsureReconcile: true,
      shouldRefreshBrowserSession: true,
      stream: applyAgentActivityToStream(
        state.stream as CortexRunStreamItem[],
        { ...msg, activity: toolName ? `Using ${toolName}` : 'Using a tool' },
        state.selectedIdeaId,
        state.executionProfile || 'fast',
      ) as StreamItem[],
    };
  }

  if (msg.type === 'tool_finished') {
    const toolName = typeof msg.tool_name === 'string' ? msg.tool_name.trim() : '';
    const status = msg.status === 'failed' ? 'failed' : 'completed';
    return {
      ...base,
      handled: true,
      shouldEnsureReconcile: true,
      shouldRefreshBrowserSession: true,
      stream: applyAgentActivityToStream(
        state.stream as CortexRunStreamItem[],
        { ...msg, activity: toolName ? `${toolName} ${status}` : `Tool ${status}` },
        state.selectedIdeaId,
        state.executionProfile || 'fast',
      ) as StreamItem[],
    };
  }

  if (msg.type === 'run_completed') {
    const stream = applyRunCompletedToStream(
      state.stream as CortexRunStreamItem[],
      msg,
      state.selectedIdeaId,
    ) as StreamItem[];
    const hasActiveRuns = activeThreadRunCount(stream) > 0;
    return {
      ...base,
      handled: true,
      shouldRefreshStream: true,
      shouldEnsureReconcile: hasActiveRuns,
      shouldStopReconcile: !hasActiveRuns,
      stream,
    };
  }

  return { ...base, stream: state.stream };
}
