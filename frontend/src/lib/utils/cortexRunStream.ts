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

function streamItemIdentity(
  item: Partial<CortexRunStreamItem> | null | undefined,
): string | null {
  if (!item?.type || item.id === null || item.id === undefined || item.id === '') return null;
  return `${item.type}:${item.id}`;
}

export function isLivePartialReply(item: Partial<CortexRunStreamItem> | null | undefined): boolean {
  return Boolean(item?.metadata?.live_agent_text);
}

function timestampMs(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function livePartialItemsForRun(
  stream: readonly CortexRunStreamItem[],
  runId: unknown,
): CortexRunStreamItem[] {
  const key = runIdKey(runId);
  if (!key) return [];
  return stream.filter((item) => isLivePartialReply(item) && streamItemRunId(item) === key);
}

function lastLivePartialForRun(
  stream: readonly CortexRunStreamItem[],
  runId: unknown,
): CortexRunStreamItem | null {
  return livePartialItemsForRun(stream, runId).at(-1) ?? null;
}

function nextLivePartialId(runId: unknown, stream: readonly CortexRunStreamItem[]): string | null {
  const baseId = livePartialId(runId);
  if (!baseId) return null;
  const count = livePartialItemsForRun(stream, runId).length;
  return count === 0 ? baseId : `${baseId}-${count + 1}`;
}

function runToolTimes(run: CortexRunStreamItem): number[] {
  const times = [
    ...(run.tool_calls || []).map((call) => timestampMs(call.at)),
    ...(run.activity_trace || [])
      .filter((entry) => Boolean(entry.tool_name))
      .map((entry) => timestampMs(entry.at)),
    ...(run.work_log || [])
      .filter((entry) => String(entry.kind || '').includes('tool'))
      .map((entry) => timestampMs(entry.time)),
  ];
  return times.filter((value): value is number => value !== null);
}

function hasRunToolWork(
  stream: readonly CortexRunStreamItem[],
  runId: unknown,
  matches: (time: number) => boolean,
): boolean {
  const key = runIdKey(runId);
  if (!key) return false;
  return stream.some((item) => {
    if (item.type !== 'run' || streamItemRunId(item) !== key) return false;
    return runToolTimes(item).some(matches);
  });
}

function hasRunToolWorkSince(
  stream: readonly CortexRunStreamItem[],
  runId: unknown,
  sinceMs: number,
): boolean {
  return hasRunToolWork(stream, runId, (time) => time > sinceMs);
}

function hasRunToolWorkBefore(
  stream: readonly CortexRunStreamItem[],
  runId: unknown,
  beforeMs: number,
): boolean {
  return hasRunToolWork(stream, runId, (time) => time <= beforeMs);
}

function livePartialLastDeltaMs(item: CortexRunStreamItem): number | null {
  return timestampMs(item.metadata?.live_agent_text_last_delta_at) ?? timestampMs(item.timestamp);
}

function shouldStartNewLivePartial(
  stream: readonly CortexRunStreamItem[],
  runId: unknown,
  currentPartial: CortexRunStreamItem | null,
): boolean {
  if (!currentPartial) return true;
  const lastDeltaMs = livePartialLastDeltaMs(currentPartial);
  return lastDeltaMs !== null && hasRunToolWorkSince(stream, runId, lastDeltaMs);
}

function isSettledAgentRunReply(item: Partial<CortexRunStreamItem>, runId: string): boolean {
  if (item.type !== 'message' || isLivePartialReply(item)) return false;
  const isAgentMessage = item.role === 'assistant' || item.role === 'illo';
  return isAgentMessage && streamItemRunId(item) === runId && Boolean(String(item.content || '').trim());
}

export function shouldRenderLiveAgentTextItem(
  item: Partial<CortexRunStreamItem> | null | undefined,
  visibleItems: readonly Partial<CortexRunStreamItem>[] = [],
): boolean {
  if (!isLivePartialReply(item)) return true;
  const runId = streamItemRunId(item);
  if (!runId) return true;
  return !visibleItems.some((candidate) => {
    return candidate !== item && isSettledAgentRunReply(candidate, runId);
  });
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

const STREAM_KIND_RANK: Record<string, number> = { message: 0, run: 1, visual_block: 2 };

function compareStreamItems(left: CortexRunStreamItem, right: CortexRunStreamItem): number {
  const byTime = (timestampMs(left.timestamp) ?? 0) - (timestampMs(right.timestamp) ?? 0);
  if (byTime) return byTime;
  const leftKind = STREAM_KIND_RANK[left.type] ?? 3;
  const byKind = leftKind - (STREAM_KIND_RANK[right.type] ?? 3);
  if (byKind) return byKind;
  const leftId = String(left.id), rightId = String(right.id);
  const persistedId = left.type === 'visual_block' ? /^vb-\d+$/ : /^\d+$/;
  if (leftKind < 3 && persistedId.test(leftId) && persistedId.test(rightId)) {
    const byRowId = Number(leftId.replace('vb-', '')) - Number(rightId.replace('vb-', ''));
    if (byRowId) return byRowId;
  }
  return leftId < rightId ? -1 : leftId > rightId ? 1 : 0;
}

export function mergeThreadStreamPageItems(
  current: CortexRunStreamItem[],
  pageItems: CortexRunStreamItem[],
  mode: 'initial' | 'head' | 'older',
): CortexRunStreamItem[] {
  const byIdentity = new Map<string, CortexRunStreamItem>();
  for (const item of mode === 'older' ? [...pageItems, ...current] : [...current, ...pageItems]) {
    byIdentity.set(`${item.type}:${item.id}`, item);
  }
  const items = [...byIdentity.values()];
  const persistedRunIds = new Set(
    items
      .filter((item) => hasVisiblePersistedRunMessage(item) && item.metadata?.synthetic_from_run_artifact !== true)
      .map(streamItemRunId)
      .filter((value): value is string => Boolean(value)),
  );
  const syntheticRunIds = new Set<string>();
  return items.filter((item) => {
    if (item.metadata?.synthetic_from_run_artifact !== true) return true;
    const runId = streamItemRunId(item);
    if (!runId || persistedRunIds.has(runId) || syntheticRunIds.has(runId)) return false;
    syntheticRunIds.add(runId);
    return true;
  }).sort(compareStreamItems);
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
  const withRunIdentities = new Set(
    withRuns.map(streamItemIdentity).filter((value): value is string => Boolean(value)),
  );
  const livePartials = liveStream.filter((item) => {
    if (!isLivePartialReply(item) || !streamItemBelongsToIdea(item, ideaId)) return false;
    const runId = streamItemRunId(item);
    const identity = streamItemIdentity(item);
    return Boolean(
      runId &&
      !persistedRunIds.has(runId) &&
      (!identity || !withRunIdentities.has(identity)),
    );
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
  const partialBaseId = livePartialId(runId);
  if (!partialBaseId) return stream;

  if (msg.reset) {
    return stream.filter((item) => {
      return item.id !== partialBaseId && !String(item.id).startsWith(`${partialBaseId}-`);
    });
  }

  const delta = typeof msg.delta === 'string' ? msg.delta : '';
  if (!delta) return stream;

  const deltaAt = eventAt(msg, nowIso);
  const existing = lastLivePartialForRun(stream, runId);
  if (!shouldStartNewLivePartial(stream, runId, existing)) {
    return stream.map((item) =>
      item.id === existing?.id
        ? {
            ...item,
            content: `${item.content || ''}${delta}`,
            metadata: {
              ...(item.metadata || {}),
              live_agent_text_last_delta_at: deltaAt,
            },
          }
        : item,
    );
  }

  const partialId = nextLivePartialId(runId, stream);
  if (!partialId) return stream;
  const deltaAtMs = timestampMs(deltaAt);
  const afterTool = deltaAtMs !== null && hasRunToolWorkBefore(stream, runId, deltaAtMs);

  return [
    ...stream,
    {
      type: 'message',
      id: partialId,
      timestamp: deltaAt,
      role: 'illo',
      content: delta,
      metadata: {
        run_id: runId,
        idea_id: msg.idea_id,
        execution_profile: msg.profile || 'fast',
        live_agent_text: true,
        live_agent_text_after_tool: afterTool,
        live_agent_text_first_delta_at: deltaAt,
        live_agent_text_last_delta_at: deltaAt,
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
