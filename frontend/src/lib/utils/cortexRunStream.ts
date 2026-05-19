export type CortexRunStreamItem = {
  type: 'message' | 'run' | 'visual_block' | string;
  id: string;
  timestamp?: string;
  role?: string;
  content?: string;
  status?: string;
  run_id?: string | number | null;
  idea_id?: string | null;
  thread_id?: string | null;
  metadata?: Record<string, any>;
  execution_profile?: string;
  started_at?: string;
  completed_at?: string;
  duration_sec?: number;
  last_activity?: string;
  tool_calls?: Array<{
    tool: string;
    args?: string;
    at?: string;
    status?: string;
    error?: string;
    result?: string;
    result_preview?: string;
    display?: Record<string, any>;
  }>;
  activity_trace?: Array<{
    at?: string;
    activity: string;
    kind?: string;
    tool_name?: string;
    status?: string;
    display?: Record<string, any>;
  }>;
  work_log?: Array<{ time?: string; text: string; kind?: string }>;
  work_summary?: Record<string, any>;
  live_lines?: Array<string | { time?: string; text: string }>;
};

export function runIdKey(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null;
  return String(value);
}

export function livePartialId(runId: unknown): string | null {
  const key = runIdKey(runId);
  return key ? `live-run-${key}` : null;
}

export function streamItemRunId(item: Partial<CortexRunStreamItem> | null | undefined): string | null {
  return runIdKey(item?.run_id ?? item?.metadata?.run_id);
}

export function streamItemIdeaId(item: Partial<CortexRunStreamItem> | null | undefined): string | null {
  return runIdKey(item?.idea_id ?? item?.thread_id ?? item?.metadata?.idea_id);
}

export function streamItemBelongsToIdea(
  item: Partial<CortexRunStreamItem> | null | undefined,
  ideaId: string | null,
): boolean {
  return Boolean(ideaId && streamItemIdeaId(item) === ideaId);
}

export function isLivePartialReply(item: Partial<CortexRunStreamItem> | null | undefined): boolean {
  return Boolean(item?.metadata?.live_agent_text);
}

export function runUiEventKey(msg: any): string | null {
  const eventId = msg?.run_event_id ?? msg?.event_id ?? msg?.event_cursor;
  if (eventId === null || eventId === undefined || eventId === '' || Number(eventId) <= 0) return null;
  return `${msg?.type || 'run'}:${msg?.run_id || ''}:${eventId}`;
}

export function toolArgsPreview(args: unknown): string | undefined {
  if (!args || typeof args !== 'object') return undefined;
  try {
    const text = JSON.stringify(args);
    return text.length > 180 ? `${text.slice(0, 179)}...` : text;
  } catch {
    return undefined;
  }
}

export function eventAt(msg: any, fallback: string = new Date().toISOString()): string {
  return typeof msg?.event_created_at === 'string' ? msg.event_created_at : fallback;
}

function toolDisplayFromMessage(msg: any, existingDisplay?: Record<string, any>, status?: string): Record<string, any> | undefined {
  const incomingDisplay = msg?.tool_display || msg?.display;
  const display = incomingDisplay?.target || !existingDisplay ? incomingDisplay || existingDisplay : existingDisplay;
  return display ? { ...display, ...(status ? { status } : {}) } : undefined;
}

export function appendRunToolCall(
  item: CortexRunStreamItem,
  msg: any,
  fallbackAt?: string,
): CortexRunStreamItem {
  const tool = typeof msg?.tool_name === 'string' && msg.tool_name.trim() ? msg.tool_name.trim() : 'tool';
  const status = msg?.type === 'tool_finished' ? (msg.status === 'failed' ? 'failed' : 'completed') : 'running';
  const calls = [...(item.tool_calls || [])];
  if (msg?.type === 'tool_finished') {
    const existing = [...calls].reverse().find((call) => call.tool === tool && call.status === 'running');
    if (existing) {
      existing.status = status;
      existing.error = typeof msg.error === 'string' ? msg.error : existing.error;
      existing.result = typeof msg.result === 'string' ? msg.result : existing.result;
      existing.result_preview = typeof msg.result_preview === 'string' ? msg.result_preview : existing.result_preview;
      existing.display = toolDisplayFromMessage(msg, existing.display, status);
      return { ...item, tool_calls: calls };
    }
  }
  calls.push({
    tool,
    args: toolArgsPreview(msg?.args),
    at: eventAt(msg, fallbackAt),
    status,
    error: typeof msg?.error === 'string' ? msg.error : undefined,
    result: typeof msg?.result === 'string' ? msg.result : undefined,
    result_preview: typeof msg?.result_preview === 'string' ? msg.result_preview : undefined,
    display: toolDisplayFromMessage(msg),
  });
  return { ...item, tool_calls: calls };
}

export function hasVisiblePersistedRunMessage(item: CortexRunStreamItem): boolean {
  return (
    item.type === 'message'
    && !isLivePartialReply(item)
    && item.metadata?.hidden !== true
    && Boolean(String(item.content || '').trim())
    && Boolean(streamItemRunId(item))
  );
}

export function mergeLiveStreamState(
  items: CortexRunStreamItem[],
  liveStream: CortexRunStreamItem[],
  ideaId: string | null,
  isActiveRunItem: (item: CortexRunStreamItem) => boolean,
  mergeRunProgressSnapshot: (snapshot: CortexRunStreamItem, live: CortexRunStreamItem) => CortexRunStreamItem,
): CortexRunStreamItem[] {
  const loadedRunIds = new Set(
    items
      .filter((item) => item.type === 'run')
      .map((item) => streamItemRunId(item) ?? String(item.id)),
  );
  const liveRunsById = new Map(
    liveStream
      .filter((item) => item.type === 'run' && streamItemBelongsToIdea(item, ideaId))
      .map((item) => [streamItemRunId(item) ?? String(item.id), item]),
  );
  const withMergedRuns = items.map((item) => {
    if (item.type !== 'run') return item;
    const live = liveRunsById.get(streamItemRunId(item) ?? String(item.id));
    return live ? mergeRunProgressSnapshot(item, live) as CortexRunStreamItem : item;
  });
  const missingLiveRuns = liveStream.filter((item) => {
    if (item.type !== 'run') return false;
    const key = streamItemRunId(item) ?? String(item.id);
    return streamItemBelongsToIdea(item, ideaId) && isActiveRunItem(item) && !loadedRunIds.has(key);
  });
  const withRuns = missingLiveRuns.length ? [...withMergedRuns, ...missingLiveRuns] : withMergedRuns;
  const persistedRunIds = new Set(
    withRuns
      .filter((item) => hasVisiblePersistedRunMessage(item))
      .map((item) => streamItemRunId(item))
      .filter((value): value is string => Boolean(value)),
  );
  const livePartials = liveStream.filter((item) => {
    if (!isLivePartialReply(item) || !streamItemBelongsToIdea(item, ideaId)) return false;
    const runId = streamItemRunId(item);
    return Boolean(runId && !persistedRunIds.has(runId));
  });
  return livePartials.length ? [...withRuns, ...livePartials] : withRuns;
}

export function applyAgentTextDeltaToStream(
  stream: CortexRunStreamItem[],
  msg: any,
  selectedIdeaId: string | null,
  nowIso: string = new Date().toISOString(),
): CortexRunStreamItem[] {
  if (!msg || msg.idea_id !== selectedIdeaId) return stream;
  const runId = msg.run_id;
  const partialId = livePartialId(runId);
  if (!partialId) return stream;

  if (msg.reset) {
    return stream.filter((item) => item.id !== partialId);
  }

  const delta = typeof msg.delta === 'string' ? msg.delta : '';
  if (!delta) return stream;

  const existing = stream.find((item) => item.id === partialId);
  if (existing) {
    return stream.map((item) =>
      item.id === partialId
        ? { ...item, content: `${item.content || ''}${delta}` }
        : item,
    );
  }

  return [
    ...stream,
    {
      type: 'message',
      id: partialId,
      timestamp: nowIso,
      role: 'illo',
      content: delta,
      metadata: {
        run_id: runId,
        idea_id: msg.idea_id,
        execution_profile: msg.profile || 'fast',
        live_agent_text: true,
      },
    },
  ];
}

export function applyAgentActivityToStream(
  stream: CortexRunStreamItem[],
  msg: any,
  selectedIdeaId: string | null,
  executionProfile: string,
): CortexRunStreamItem[] {
  if (!msg || msg.idea_id !== selectedIdeaId) return stream;
  const activity = typeof msg.activity === 'string' ? msg.activity.trim() : '';
  if (!activity) return stream;
  const runId = msg.run_id ?? msg.id;
  if (runId == null) return stream;
  const now = eventAt(msg);
  let matched = false;
  const activityEntry = {
    at: now,
    activity,
    kind: typeof msg.source_event_type === 'string' ? msg.source_event_type : msg.type,
    tool_name: typeof msg.tool_name === 'string' ? msg.tool_name : undefined,
    status: typeof msg.status === 'string' ? msg.status : undefined,
    display: msg.tool_display || msg.display || undefined,
  };
  const workEntry = { time: now, text: activity, kind: activityEntry.kind };
  const appendLine = (lines: CortexRunStreamItem['live_lines']) => [...(lines || []), activity].slice(-16);
  const appendTrace = (trace: CortexRunStreamItem['activity_trace']) => [...(trace || []), activityEntry].slice(-40);
  const appendWorkLog = (log: CortexRunStreamItem['work_log']) => [...(log || []), workEntry].slice(-40);

  const nextStream = stream.map((item) => {
    if (item.type !== 'run') return item;
    if (String(item.id) !== String(runId)) return item;
    matched = true;
    let next: CortexRunStreamItem = {
      ...item,
      status: item.status && item.status !== 'queued' ? item.status : 'running',
      last_activity: activity,
      live_lines: appendLine(item.live_lines),
      activity_trace: appendTrace(item.activity_trace),
      work_log: appendWorkLog(item.work_log),
    };
    if (msg.type === 'tool_started' || msg.type === 'tool_finished') {
      next = appendRunToolCall(next, msg, now);
    }
    return next;
  });

  if (matched) return nextStream;

  let runItem: CortexRunStreamItem = {
    type: 'run',
    id: String(runId),
    run_id: Number(runId),
    idea_id: msg.idea_id,
    thread_id: msg.idea_id,
    timestamp: now,
    started_at: now,
    status: 'running',
    last_activity: activity,
    live_lines: [activity],
    activity_trace: [activityEntry],
    work_log: [workEntry],
    execution_profile:
      msg.strategy === 'fast' || msg.profile === 'fast' || executionProfile === 'fast'
        ? 'fast'
        : undefined,
  };
  if (msg.type === 'tool_started' || msg.type === 'tool_finished') {
    runItem = appendRunToolCall(runItem, msg, now);
  }
  return [...nextStream, runItem];
}

export function applyRunCompletedToStream(
  stream: CortexRunStreamItem[],
  msg: any,
  selectedIdeaId: string | null,
): CortexRunStreamItem[] {
  if (!msg || msg.idea_id !== selectedIdeaId) return stream;
  const runId = msg.run_id ?? msg.id;
  if (runId == null) return stream;
  const completedAt = eventAt(msg);
  const status = msg.status === 'failed' || msg.status === 'canceled' ? msg.status : 'completed';
  return stream.map((item) => {
    if (item.type !== 'run' || String(item.id) !== String(runId)) return item;
    const toolCount = item.tool_calls?.length || item.work_summary?.tool_count || 0;
    const startedMs = item.started_at ? new Date(item.started_at).getTime() : Number.NaN;
    const completedMs = new Date(completedAt).getTime();
    const durationSec = Number.isFinite(startedMs) && Number.isFinite(completedMs)
      ? Math.max(0, Math.round((completedMs - startedMs) / 1000))
      : item.duration_sec;
    return {
      ...item,
      status,
      completed_at: completedAt,
      duration_sec: durationSec,
      work_summary: {
        ...(item.work_summary || {}),
        status,
        duration_sec: durationSec,
        tool_count: toolCount,
        activity_count: item.activity_trace?.length || item.work_summary?.activity_count || 0,
      },
    };
  });
}
