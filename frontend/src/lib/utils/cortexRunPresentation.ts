const ACTIVE_RUN_STATUSES = new Set(['queued', 'starting', 'running', 'paused', 'verifying', 'pending_approval']);
const FAST_TRANSCRIPT_VISIBLE_STATUSES = new Set(['queued', 'starting', 'running', 'paused', 'verifying', 'completed', 'failed', 'canceled', 'pending_approval']);
const RUN_STATUS_RANK: Record<string, number> = {
  queued: 1,
  starting: 2,
  running: 3,
  paused: 4,
  pending_approval: 5,
  verifying: 6,
};

export function runExecutionProfile(source: any): string {
  return String(
    source?.execution_profile
      ?? source?.requested_run_profile
      ?? source?.metadata?.execution_profile
      ?? source?.metadata?.executionProfile
      ?? '',
  ).toLowerCase();
}

export function isFastRun(source: any): boolean {
  return runExecutionProfile(source) === 'fast';
}

export function isActiveRun(source: any): boolean {
  return ACTIVE_RUN_STATUSES.has(String(source?.status || ''));
}

export function shouldShowRunInTranscript(source: any): boolean {
  if (!isFastRun(source)) return true;
  if (source?.requires_approval) return true;
  if (source?.error) return true;
  return FAST_TRANSCRIPT_VISIBLE_STATUSES.has(String(source?.status || ''));
}

export function isLiveFastReplyItem(item: any): boolean {
  return (
    item?.type === 'message'
    && item?.metadata?.live_agent_text
    && String(item.metadata?.execution_profile || '').toLowerCase() === 'fast'
  );
}

export function hasLiveFastReply(items: any[] | undefined | null): boolean {
  return (items || []).some(isLiveFastReplyItem);
}

function runStatusRank(status: unknown): number {
  return RUN_STATUS_RANK[String(status || '')] || 0;
}

function chooseRunStatus(snapshotStatus: unknown, liveStatus: unknown): string {
  const snapshot = String(snapshotStatus || '');
  const live = String(liveStatus || '');
  if (snapshot && !ACTIVE_RUN_STATUSES.has(snapshot)) return snapshot;
  if (live && !ACTIVE_RUN_STATUSES.has(live)) return live;
  return runStatusRank(live) > runStatusRank(snapshot) ? live : (snapshot || live || 'queued');
}

function entryKey(entry: any, labelFields: string[]): string {
  if (typeof entry === 'string') return `text:${entry}`;
  const at = entry?.at ?? entry?.time ?? '';
  const kind = entry?.kind ?? '';
  const label = labelFields.map((field) => entry?.[field]).find((value) => value != null) ?? '';
  return `${kind}:${at}:${label}`;
}

function entryTimeMs(entry: any): number | null {
  if (typeof entry === 'string') return null;
  const value = entry?.at ?? entry?.time;
  if (typeof value !== 'string') return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function sortEntriesByTime(entries: any[]): any[] {
  if (entries.filter((entry) => entryTimeMs(entry) !== null).length < 2) return entries;
  return [...entries].sort((a, b) => {
    const left = entryTimeMs(a);
    const right = entryTimeMs(b);
    if (left === null && right === null) return 0;
    if (left === null) return 1;
    if (right === null) return -1;
    return left - right;
  });
}

function mergeEntries(
  snapshotEntries: any[] | undefined,
  liveEntries: any[] | undefined,
  labelFields: string[],
  limit: number,
): any[] {
  const merged: any[] = [];
  const seen = new Set<string>();
  for (const entry of [...(snapshotEntries || []), ...(liveEntries || [])]) {
    const key = entryKey(entry, labelFields);
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(entry);
  }
  return sortEntriesByTime(merged).slice(-limit);
}

function lineText(line: any): string {
  if (typeof line === 'string') return line;
  return String(line?.text ?? line?.activity ?? '');
}

function mergeLiveLines(snapshotLines: any[] | undefined, liveLines: any[] | undefined): any[] {
  const merged: any[] = [];
  const seen = new Set<string>();
  for (const line of [...(snapshotLines || []), ...(liveLines || [])]) {
    const text = lineText(line).trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    merged.push(line);
  }
  return merged.slice(-32);
}

function toolCallKey(call: any): string {
  return [call?.tool || 'tool', call?.at || '', call?.args || ''].join(':');
}

function toolStatusRank(status: unknown): number {
  switch (String(status || '')) {
    case 'failed':
      return 3;
    case 'completed':
      return 2;
    case 'running':
      return 1;
    default:
      return 0;
  }
}

function chooseToolStatus(left: unknown, right: unknown): string | undefined {
  const leftStatus = String(left || '');
  const rightStatus = String(right || '');
  return toolStatusRank(rightStatus) > toolStatusRank(leftStatus)
    ? rightStatus || undefined
    : leftStatus || rightStatus || undefined;
}

function mergeToolDisplay(existingDisplay: any, nextDisplay: any, status: string | undefined): Record<string, any> {
  const display = nextDisplay?.target || !existingDisplay ? nextDisplay ?? existingDisplay : existingDisplay;
  return {
    ...display,
    ...(status ? { status } : {}),
  };
}

function mergeToolCalls(snapshotCalls: any[] | undefined, liveCalls: any[] | undefined): any[] {
  const merged: any[] = [];
  const indexes = new Map<string, number>();
  for (const call of [...(snapshotCalls || []), ...(liveCalls || [])]) {
    const key = toolCallKey(call);
    const existingIndex = indexes.get(key);
    if (existingIndex == null) {
      indexes.set(key, merged.length);
      merged.push(call);
      continue;
    }
    const existing = merged[existingIndex];
    const status = chooseToolStatus(existing?.status, call?.status);
    merged[existingIndex] = {
      ...existing,
      ...call,
      status,
      error: existing?.error ?? call?.error,
      result: existing?.result ?? call?.result,
      result_preview: existing?.result_preview ?? call?.result_preview,
      display: mergeToolDisplay(existing?.display, call?.display, status),
      finished_at: existing?.finished_at ?? call?.finished_at,
    };
  }
  return merged.slice(-40);
}

function latestActivityLabel(activityTrace: any[], workLog: any[], fallback: unknown): string | undefined {
  const lastActivity = activityTrace.at(-1);
  if (lastActivity?.activity) return String(lastActivity.activity);
  const lastWork = workLog.at(-1);
  if (lastWork?.text) return String(lastWork.text);
  const text = String(fallback || '').trim();
  return text || undefined;
}

export function mergeRunProgressSnapshot(snapshot: any, live: any): any {
  if (!snapshot) return live;
  if (!live) return snapshot;
  const activityTrace = mergeEntries(snapshot.activity_trace, live.activity_trace, ['activity', 'label'], 80);
  const workLog = mergeEntries(snapshot.work_log, live.work_log, ['text', 'activity', 'label'], 80);
  const liveLines = mergeLiveLines(snapshot.live_lines, live.live_lines);
  const toolCalls = mergeToolCalls(snapshot.tool_calls, live.tool_calls);
  const status = chooseRunStatus(snapshot.status, live.status);
  const workSummary = {
    ...(live.work_summary || {}),
    ...(snapshot.work_summary || {}),
    status,
    activity_count: Math.max(
      Number(live.work_summary?.activity_count || 0),
      Number(snapshot.work_summary?.activity_count || 0),
      activityTrace.length,
      workLog.length,
      liveLines.length,
    ),
    tool_count: Math.max(
      Number(live.work_summary?.tool_count || 0),
      Number(snapshot.work_summary?.tool_count || 0),
      toolCalls.length,
    ),
  };
  return {
    ...live,
    ...snapshot,
    status,
    started_at: snapshot.started_at ?? live.started_at,
    completed_at: snapshot.completed_at ?? live.completed_at,
    failed_at: snapshot.failed_at ?? live.failed_at,
    canceled_at: snapshot.canceled_at ?? live.canceled_at,
    execution_profile: snapshot.execution_profile ?? snapshot.profile ?? live.execution_profile ?? live.profile,
    requested_run_profile: snapshot.requested_run_profile ?? live.requested_run_profile,
    last_activity: latestActivityLabel(activityTrace, workLog, snapshot.last_activity ?? live.last_activity),
    live_lines: liveLines,
    activity_trace: activityTrace,
    work_log: workLog,
    tool_calls: toolCalls,
    work_summary: workSummary,
  };
}

export function findActiveFastRun(runInfo: any, items: any[] | undefined | null): any | null {
  if (runInfo && isActiveRun(runInfo) && isFastRun(runInfo)) return runInfo;
  return (items || []).find((item: any) => item?.type === 'run' && isActiveRun(item) && isFastRun(item)) ?? null;
}


export interface CodeReviewFile {
  path: string;
  operation?: string;
  status?: string;
  source: 'artifact' | 'tool_call';
  tool?: string;
  runId?: string | number;
  at?: string;
}

const CODE_REVIEW_FILE_TOOLS = new Set(['write_file', 'edit_file', 'apply_patch']);
const NON_REVIEW_FILE_OPERATIONS = new Set(['read', 'observe', 'observed', 'search', 'list', 'summary']);

function normalizeReviewPath(value: unknown): string {
  return String(value ?? '').trim();
}

function normalizeReviewOperation(value: unknown): string {
  return String(value ?? '').trim().toLowerCase();
}

function parseToolArgs(args: unknown): Record<string, any> {
  if (!args) return {};
  if (typeof args === 'object') return args as Record<string, any>;
  if (typeof args !== 'string') return {};
  const trimmed = args.trim();
  if (!trimmedLooksLikeJson(trimmed)) return {};
  try {
    const parsed = JSON.parse(trimmed);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function trimmedLooksLikeJson(value: string): boolean {
  return value.startsWith('{') && value.endsWith('}');
}

function reviewOperationForTool(tool: string): string {
  if (tool === 'write_file') return 'created or updated';
  if (tool === 'edit_file' || tool === 'apply_patch') return 'changed';
  return 'changed';
}

function artifactPayload(artifact: any): Record<string, any> {
  const payload = artifact?.payload;
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) return payload;
  if (artifact && typeof artifact === 'object' && !Array.isArray(artifact)) return artifact;
  return {};
}

function artifactReviewFile(artifact: any, runId: string | number | undefined): CodeReviewFile | null {
  const payload = artifactPayload(artifact);
  const type = String(payload.type ?? artifact?.artifact_type ?? '').trim();
  if (type !== 'file' && type !== 'file_observation' && artifact?.artifact_type !== 'file_edit') return null;

  const operation = normalizeReviewOperation(payload.operation ?? payload.status ?? artifact?.artifact_type);
  if (operation && NON_REVIEW_FILE_OPERATIONS.has(operation)) return null;

  const path = normalizeReviewPath(payload.relative_path ?? payload.path ?? artifact?.uri);
  if (!path) return null;

  return {
    path,
    operation: operation || 'changed',
    status: normalizeReviewPath(payload.status ?? artifact?.visibility) || undefined,
    source: 'artifact',
    tool: normalizeReviewPath(payload.provenance?.operation ?? payload.provenance?.source) || undefined,
    runId: runId ?? payload.run_id,
    at: normalizeReviewPath(artifact?.created_at ?? payload.observed_at) || undefined,
  };
}

function toolCallReviewFile(call: any, runId: string | number | undefined): CodeReviewFile | null {
  const tool = normalizeReviewPath(call?.tool ?? call?.tool_name);
  if (!CODE_REVIEW_FILE_TOOLS.has(tool)) return null;
  const args = parseToolArgs(call?.args);
  const path = normalizeReviewPath(args.path ?? args.file ?? args.file_path);
  if (!path) return null;

  return {
    path,
    operation: reviewOperationForTool(tool),
    status: normalizeReviewPath(call?.status) || undefined,
    source: 'tool_call',
    tool,
    runId,
    at: normalizeReviewPath(call?.finished_at ?? call?.at) || undefined,
  };
}

function reviewFileRank(source: CodeReviewFile['source']): number {
  return source === 'artifact' ? 2 : 1;
}

export function deriveCodeReviewFilesFromRun(source: any): CodeReviewFile[] {
  if (!source) return [];
  const runId = source?.run_id ?? source?.id;
  const byPath = new Map<string, CodeReviewFile>();

  const add = (file: CodeReviewFile | null) => {
    if (!file?.path) return;
    const key = file.path;
    const existing = byPath.get(key);
    if (!existing || reviewFileRank(file.source) >= reviewFileRank(existing.source)) {
      byPath.set(key, { ...existing, ...file });
    }
  };

  for (const artifact of Array.isArray(source?.artifacts) ? source.artifacts : []) {
    add(artifactReviewFile(artifact, runId));
  }
  for (const call of Array.isArray(source?.tool_calls) ? source.tool_calls : []) {
    add(toolCallReviewFile(call, runId));
  }

  return Array.from(byPath.values()).sort((left, right) => left.path.localeCompare(right.path));
}

export function deriveCodeReviewFilesFromRuns(sources: Array<any | null | undefined>): CodeReviewFile[] {
  const byPath = new Map<string, CodeReviewFile>();
  for (const source of sources) {
    for (const file of deriveCodeReviewFilesFromRun(source)) {
      const existing = byPath.get(file.path);
      if (!existing || reviewFileRank(file.source) >= reviewFileRank(existing.source)) {
        byPath.set(file.path, { ...existing, ...file });
      }
    }
  }
  return Array.from(byPath.values()).sort((left, right) => left.path.localeCompare(right.path));
}

export interface AgentRunActivityStep {
  time?: string;
  label: string;
}

export interface AgentRunWorkThoughtItem {
  kind: 'thought';
  at?: string;
  text: string;
}

export interface AgentRunWorkToolItem {
  kind: 'tool';
  at?: string;
  tool: string;
  args?: string;
  status?: string;
  error?: string;
  result?: string;
  finishedAt?: string;
  display?: Record<string, any>;
}

export type AgentRunWorkTimelineItem = AgentRunWorkThoughtItem | AgentRunWorkToolItem;

function activityLabel(entry: any): string {
  if (typeof entry === 'string') return entry.trim();
  if (typeof entry?.activity === 'string') return entry.activity.trim();
  if (typeof entry?.label === 'string') return entry.label.trim();
  return '';
}

function activityTime(entry: any): number | null {
  if (typeof entry?.at !== 'string') return null;
  const parsed = new Date(entry.at).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function orderedActivityTrace(source: any): any[] {
  const trace = Array.isArray(source?.activity_trace) ? [...source.activity_trace] : [];
  if (trace.length < 2) return trace;
  const timed = trace.filter((entry) => activityTime(entry) !== null);
  if (timed.length < 2) return trace;
  return trace.sort((a, b) => (activityTime(a) ?? 0) - (activityTime(b) ?? 0));
}

const RUN_LIFECYCLE_WORK_KINDS = new Set([
  'run.started',
  'run.completed',
  'run.failed',
  'run.canceled',
  'run_started',
  'run_completed',
]);

const RUN_TOOL_WORK_KINDS = new Set([
  'run.tool_started',
  'run.tool_completed',
  'run.tool_failed',
  'tool_started',
  'tool_finished',
]);

function workEntryKind(entry: any): string {
  return String(entry?.kind ?? entry?.source_event_type ?? entry?.type ?? '').trim();
}

function workEntryTime(entry: any): string | undefined {
  if (typeof entry?.at === 'string' && entry.at.trim()) return entry.at;
  if (typeof entry?.time === 'string' && entry.time.trim()) return entry.time;
  return undefined;
}

function workEntryText(entry: any): string {
  if (typeof entry === 'string') return entry.trim();
  if (typeof entry?.text === 'string') return entry.text.trim();
  return activityLabel(entry);
}

function displayObject(value: any): Record<string, any> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : undefined;
}

function workEntryTool(entry: any): string {
  const tool = String(entry?.tool_name ?? entry?.tool ?? '').trim();
  if (tool) return tool;
  const text = workEntryText(entry);
  const usingMatch = text.match(/^Using\s+(.+)$/i);
  if (usingMatch?.[1]) return usingMatch[1].trim();
  const finishedMatch = text.match(/^(.+)\s+(completed|failed)$/i);
  if (finishedMatch?.[1]) return finishedMatch[1].trim();
  return 'tool';
}

function toolNameFromActivityText(text: string): string {
  const match = text.match(/^Using\s+([^:\s]+)(?::|\s|$)/i);
  return match?.[1]?.trim() ?? '';
}

function isDuplicateToolActivity(
  entry: any,
  kind: string,
  text: string,
  structuredToolNames: ReadonlySet<string>,
): boolean {
  if (kind !== 'run.activity' && kind !== 'step_started') return false;
  if (!/^Using\s+/i.test(text)) return false;
  const toolName = String(entry?.tool_name || toolNameFromActivityText(text)).trim();
  return Boolean(toolName && (entry?.tool_name || structuredToolNames.has(toolName)));
}

function workTimelineTimeMs(at: string | undefined): number | null {
  if (!at) return null;
  const parsed = new Date(at).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

function runWorkLogEntries(source: any): any[] {
  if (Array.isArray(source?.work_log) && source.work_log.length > 0) return source.work_log;
  if (Array.isArray(source?.activity_trace) && source.activity_trace.length > 0) return orderedActivityTrace(source);
  return [];
}

function timelineToolKey(tool: string, at: string | undefined): string {
  return `${tool}:${at ?? ''}`;
}

function normalizedTimelineText(text: string): string {
  return text
    .toLowerCase()
    .replace(/[`*_#>\[\]()]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function volatileThoughtKey(text: string): string {
  const normalized = normalizedTimelineText(text);
  if (normalized.startsWith('thinking through the request')) return 'thinking';
  if (normalized.startsWith('writing response')) return 'writing';
  if (normalized.startsWith('streaming')) return 'streaming';
  return '';
}

function stableWorkThoughtText(text: string): string {
  const cleaned = text.trim();
  const boldMatch = cleaned.match(/^\*\*([^*]+)\*\*\s*([\s\S]*)$/);
  if (!boldMatch) return cleaned;
  const title = boldMatch[1]?.trim();
  const tail = (boldMatch[2] ?? '').trim();
  if (!title) return cleaned;
  if (!tail || tail.length < 12 || /^[A-Za-z][.,;:]?$/.test(tail)) return `**${title}**`;
  return cleaned;
}

function thoughtsAreProgressive(left: AgentRunWorkThoughtItem, right: AgentRunWorkThoughtItem): boolean {
  const leftText = normalizedTimelineText(left.text).replace(/[.…]+$/g, '');
  const rightText = normalizedTimelineText(right.text).replace(/[.…]+$/g, '');
  if (!leftText || !rightText) return false;
  const volatileLeft = volatileThoughtKey(left.text);
  if (volatileLeft && volatileLeft === volatileThoughtKey(right.text)) return true;
  const shortest = Math.min(leftText.length, rightText.length);
  return shortest >= 24 && (leftText.startsWith(rightText) || rightText.startsWith(leftText));
}

function toolDisplayKey(item: AgentRunWorkToolItem): string {
  return [
    item.tool.toLowerCase(),
    item.args || '',
  ].join(':');
}

function timelineItemsAreRedundant(
  left: AgentRunWorkTimelineItem,
  right: AgentRunWorkTimelineItem,
): boolean {
  if (left.kind !== right.kind) return false;
  if (left.kind === 'tool' && right.kind === 'tool') {
    return toolDisplayKey(left) === toolDisplayKey(right);
  }
  if (left.kind === 'thought' && right.kind === 'thought') {
    return thoughtsAreProgressive(left, right);
  }
  return false;
}

function mergeRedundantTimelineItems<T extends AgentRunWorkTimelineItem & { order: number; key: string }>(
  records: T[],
): T[] {
  const compacted: T[] = [];
  for (const record of records) {
    const previous = compacted[compacted.length - 1];
    if (previous && timelineItemsAreRedundant(previous, record)) {
      compacted[compacted.length - 1] =
        previous.kind === 'thought'
          && record.kind === 'thought'
          && previous.text.length > record.text.length
          ? previous
          : record;
      continue;
    }
    compacted.push(record);
  }
  return compacted;
}

export function runWorkTimelineItems(
  source: any,
  {
    limit = 80,
  }: {
    limit?: number;
  } = {},
): AgentRunWorkTimelineItem[] {
  type TimelineRecord = AgentRunWorkTimelineItem & { order: number; key: string };

  const records: TimelineRecord[] = [];
  const seen = new Set<string>();
  const workEntries = runWorkLogEntries(source);
  const toolStartOrder = new Map<string, number>();
  const toolCallKeys = new Set<string>();
  const structuredToolNames = new Set<string>(
    (source?.tool_calls || [])
      .map((call: any) => String(call?.tool || call?.tool_name || '').trim())
      .filter(Boolean),
  );

  workEntries.forEach((entry, index) => {
    const kind = workEntryKind(entry);
    if (kind === 'run.tool_started' || kind === 'tool_started') {
      const at = workEntryTime(entry);
      toolStartOrder.set(timelineToolKey(workEntryTool(entry), at), index);
    }
  });

  const pushRecord = (record: TimelineRecord) => {
    if (seen.has(record.key)) return;
    seen.add(record.key);
    records.push(record);
  };

  for (const [index, entry] of workEntries.entries()) {
    const kind = workEntryKind(entry);
    if (RUN_LIFECYCLE_WORK_KINDS.has(kind)) continue;
    if (RUN_TOOL_WORK_KINDS.has(kind)) continue;

    const text = stableWorkThoughtText(workEntryText(entry));
    if (!text) continue;
    if (isDuplicateToolActivity(entry, kind, text, structuredToolNames)) continue;
    const at = workEntryTime(entry);
    pushRecord({
      kind: 'thought',
      at,
      text,
      order: index,
      key: `thought:${kind}:${at ?? ''}:${text}`,
    });
  }

  for (const [index, call] of (source?.tool_calls || []).entries()) {
    const tool = String(call?.tool || call?.tool_name || 'tool').trim() || 'tool';
    const at = typeof call?.at === 'string' && call.at.trim() ? call.at : undefined;
    const key = timelineToolKey(tool, at);
    toolCallKeys.add(key);
    pushRecord({
      kind: 'tool',
      at,
      tool,
      args: typeof call?.args === 'string' && call.args.trim() ? call.args : undefined,
      status: typeof call?.status === 'string' && call.status.trim() ? call.status : undefined,
      error: typeof call?.error === 'string' && call.error.trim() ? call.error : undefined,
      result: typeof call?.result === 'string' && call.result.trim() ? call.result : undefined,
      finishedAt: typeof call?.finished_at === 'string' && call.finished_at.trim() ? call.finished_at : undefined,
      display: displayObject(call?.display),
      order: toolStartOrder.get(key) ?? workEntries.length + index,
      key: `tool:${key}:${call?.args ?? ''}`,
    });
  }

  for (const [index, entry] of workEntries.entries()) {
    const kind = workEntryKind(entry);
    if (kind !== 'run.tool_started' && kind !== 'tool_started') continue;
    const at = workEntryTime(entry);
    const tool = workEntryTool(entry);
    const key = timelineToolKey(tool, at);
    if (toolCallKeys.has(key)) continue;
    pushRecord({
      kind: 'tool',
      at,
      tool,
      status: 'running',
      display: displayObject(entry?.display),
      order: index,
      key: `tool:${key}`,
    });
  }

  if (records.length === 0) {
    for (const [index, line] of (source?.live_lines || []).entries()) {
      const text = stableWorkThoughtText(workEntryText(line));
      if (!text) continue;
      const at = workEntryTime(line);
      pushRecord({
        kind: 'thought',
        at,
        text,
        order: index,
        key: `live:${at ?? ''}:${text}`,
      });
    }
  }

  const orderedRecords = records
    .sort((left, right) => {
      const leftMs = workTimelineTimeMs(left.at);
      const rightMs = workTimelineTimeMs(right.at);
      if (leftMs !== null && rightMs !== null && leftMs !== rightMs) return leftMs - rightMs;
      if (leftMs !== null && rightMs === null) return -1;
      if (leftMs === null && rightMs !== null) return 1;
      return left.order - right.order;
    });

  return mergeRedundantTimelineItems(orderedRecords)
    .slice(-limit)
    .map(({ order: _order, key: _key, ...item }) => item);
}

export function runActivitySteps(
  source: any,
  {
    elapsedLabel,
    limit = 6,
  }: {
    elapsedLabel?: (iso: string) => string;
    limit?: number;
  } = {},
): AgentRunActivityStep[] {
  const steps: AgentRunActivityStep[] = [];
  const seen = new Set<string>();

  const push = (entry: any, fallbackTime?: string) => {
    const label = activityLabel(entry);
    if (!label || seen.has(label)) return;
    seen.add(label);
    const at = typeof entry?.at === 'string' ? entry.at : undefined;
    steps.push({
      time: at && elapsedLabel ? `${elapsedLabel(at)} live` : fallbackTime,
      label,
    });
  };

  for (const entry of orderedActivityTrace(source)) push(entry);
  for (const line of source?.live_lines || []) push(line);
  if (source?.last_activity) push({ activity: source.last_activity });

  return steps.slice(-limit);
}


function defaultDurationLabel(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function numericSeconds(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export function runWorkSummaryTitle(source: any): string {
  if (isActiveRun(source)) return 'Working';
  const duration = numericSeconds(source?.work_summary?.duration_sec ?? source?.duration_sec);
  return duration === null ? 'Worked' : `Worked for ${defaultDurationLabel(duration)}`;
}

export function runWorkSummarySubtitle(source: any): string {
  const toolCount = Number(source?.work_summary?.tool_count ?? source?.tool_calls?.length ?? 0) || 0;
  const activityCount = Number(source?.work_summary?.activity_count ?? source?.activity_trace?.length ?? source?.live_lines?.length ?? 0) || 0;
  const parts = [];
  if (toolCount > 0) parts.push(`${toolCount} ${toolCount === 1 ? 'tool' : 'tools'}`);
  if (activityCount > 0) parts.push(`${activityCount} ${activityCount === 1 ? 'event' : 'events'}`);
  return parts.join(' · ') || String(source?.status || 'run');
}
