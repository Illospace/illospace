import {
  attachmentDetail,
  attachmentDownloadUrl,
  attachmentLabel,
  attachmentPreviewKind,
  attachmentUrl,
  messageLinkAttachments,
} from '$lib/utils/attachmentPreview';
import {
  runActivitySteps,
  runWorkSummarySubtitle,
  runWorkSummaryTitle,
  runWorkTimelineItems,
  isActiveRun,
  isFastRun,
  shouldShowRunInTranscript,
} from '$lib/utils/cortexRunPresentation';
import { shouldRenderLiveAgentTextItem, streamItemRunId } from '$lib/utils/cortexRunStream';
import { parseServerDate, parseServerTimeMs, relativeTimeAgo } from '$lib/utils/datetime';
import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';
import { buildRunEvidenceDebug } from '$lib/utils/runEvidenceDebug';
import { buildChronologicalRunSegments } from '$lib/utils/threadRunChronology';
import { orderQueuedThreadStreamItems } from '$lib/utils/threadStreamOrdering';
import type { Idea, StreamItem } from '$lib/types/cortex';
import { normalizeAgentRunStatus } from '../../../constants/statuses.ts';
import type {
  CortexThreadStageAttachmentItem,
  CortexThreadStageMessageItem,
  CortexThreadStageRunItem,
  CortexThreadStageRunStatus,
  CortexThreadStageRunStepStatus,
  CortexThreadStageTranscriptItem,
} from './threadTranscriptAdapter';

export {
  getCortexThreadRunStatusGlyph,
  getCortexThreadRunStatusLabel,
  getCortexThreadRunStepTone,
  getCortexThreadStepStatusGlyph,
  normalizeCortexThreadLiveLine,
  orderCortexThreadRunSteps,
  summarizeCortexThreadRunSteps,
} from './threadTranscriptAdapter';

export type {
  CortexThreadStageAttachmentItem,
  CortexThreadStageHeaderConfig,
  CortexThreadStageLiveLine,
  ThreadTranscriptProps,
  CortexThreadStageMessageItem,
  CortexThreadStageMessageRole,
  CortexThreadStageMessageSection,
  CortexThreadStageRunItem,
  CortexThreadStageRunStatus,
  CortexThreadStageRunStep,
  CortexThreadStageRunStepStatus,
  CortexThreadStageRunTelemetryItem,
  CortexThreadStageThinkingItem,
  CortexThreadStageThinkingStep,
  CortexThreadStageToolCall,
  CortexThreadStageTranscriptItem,
  CortexThreadStageVisualReplyItem,
  CortexThreadStageWorkTimelineItem,
} from './threadTranscriptAdapter';

export type ThreadThemeMode = 'light' | 'dark';

export interface ThreadCurrentUser {
  id?: string | number | null;
  color?: string | null;
}

export interface BuildThreadTranscriptOptions {
  idea?: Idea | null;
  stream: readonly StreamItem[];
  themeMode?: ThreadThemeMode;
  runInfo?: StreamItem | null;
  latestRun?: StreamItem | null;
  currentUser?: ThreadCurrentUser | null;
  nowMs?: number;
  onApproveRun?: (runId: number) => void;
  onDenyRun?: (runId: number) => void;
}

const THREAD_USER_DARK_SHELL_COLOR = '#050910';
const THREAD_USER_DARK_OWNER_TEXT = '#f0f0fa';
const THREAD_USER_LIGHT_SHELL_COLOR = '#fffdf7';
const THREAD_USER_LIGHT_OWNER_TEXT = '#18212a';

export function visibleThreadStreamItems(stream: readonly StreamItem[]): StreamItem[] {
  return stream.filter((item) => !(item.type === 'message' && item.metadata?.hidden));
}

export function timeAgo(isoStr: string | undefined, nowMs = Date.now()): string {
  return relativeTimeAgo(isoStr, nowMs);
}

export function formatDuration(sec: number | undefined): string {
  if (!sec) return '';
  if (sec < 60) return `${Math.round(sec)}s`;
  const minutes = Math.floor(sec / 60);
  const seconds = Math.round(sec % 60);
  if (minutes < 60) return `${minutes}m ${seconds}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function elapsedLabel(isoStr: string | undefined, nowMs = Date.now()): string {
  if (!isoStr) return '';
  const startMs = parseServerTimeMs(isoStr);
  if (!startMs) return '';
  const seconds = Math.max(0, Math.floor((nowMs - startMs) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function formatRunClock(isoStr: string | undefined): string {
  if (!isoStr) return '';
  try {
    return parseServerDate(isoStr)?.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) ?? '';
  } catch {
    return '';
  }
}

export function numericValue(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '') return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

export function formatTokens(value: unknown): string | undefined {
  const tokens = numericValue(value);
  if (!tokens || tokens <= 0) return undefined;
  return `${Math.round(tokens).toLocaleString()} tok`;
}

export function formatCompactTokens(value: unknown): string | undefined {
  const tokens = numericValue(value);
  if (!tokens || tokens <= 0) return undefined;
  if (tokens >= 1_000_000) {
    const precision = tokens >= 10_000_000 ? 0 : 2;
    return `${(tokens / 1_000_000).toFixed(precision).replace(/\.?0+$/, '')}M tok`;
  }
  if (tokens >= 1_000) {
    const precision = tokens >= 100_000 ? 0 : 1;
    return `${(tokens / 1_000).toFixed(precision).replace(/\.0$/, '')}k tok`;
  }
  return `${Math.round(tokens).toLocaleString()} tok`;
}

export function formatCost(value: unknown): string | undefined {
  const cost = numericValue(value);
  if (!cost || cost <= 0) return undefined;
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  if (cost < 1) return `$${cost.toFixed(3)}`;
  return `$${cost.toFixed(2)}`;
}

export function formatChars(value: unknown): string | undefined {
  const chars = numericValue(value);
  if (!chars || chars <= 0) return undefined;
  return `${Math.round(chars).toLocaleString()} chars`;
}

export function buildRunTelemetry(item: StreamItem): { label: string; value: string }[] {
  const metrics: { label: string; value: string }[] = [];
  const source = item as StreamItem & Record<string, unknown>;
  const addMetric = (label: string, value: string | undefined) => {
    if (value) metrics.push({ label, value });
  };

  addMetric('input', formatTokens(source.tokens_input));
  addMetric('output', formatTokens(source.tokens_output));
  addMetric('cache read', formatTokens(source.cache_read));
  addMetric('cache write', formatTokens(source.cache_write));
  addMetric('prompt', formatChars(source.system_prompt_chars));
  return metrics;
}

export function normalizeRunStatus(status: string | undefined): CortexThreadStageRunStatus {
  return normalizeAgentRunStatus(status);
}

export function normalizeStepStatus(status: string | undefined): CortexThreadStageRunStepStatus {
  switch (status) {
    case 'completed':
    case 'running':
    case 'pending':
    case 'failed':
    case 'skipped':
      return status;
    case 'timeout':
      return 'failed';
    default:
      return 'pending';
  }
}

export function normalizeHexColor(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(trimmed)) return null;
  if (trimmed.length === 4) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
  }
  return trimmed;
}

function hexToRgb(hex: string) {
  const normalized = normalizeHexColor(hex) ?? '#000000';
  const value = normalized.slice(1);
  return {
    r: Number.parseInt(value.slice(0, 2), 16),
    g: Number.parseInt(value.slice(2, 4), 16),
    b: Number.parseInt(value.slice(4, 6), 16),
  };
}

function rgbToHex({ r, g, b }: { r: number; g: number; b: number }) {
  return `#${[r, g, b]
    .map((part) => Math.max(0, Math.min(255, Math.round(part))).toString(16).padStart(2, '0'))
    .join('')}`;
}

export function mixHex(a: string, b: string, bWeight = 0.5): string {
  const left = hexToRgb(a);
  const right = hexToRgb(b);
  const leftWeight = 1 - bWeight;
  return rgbToHex({
    r: left.r * leftWeight + right.r * bWeight,
    g: left.g * leftWeight + right.g * bWeight,
    b: left.b * leftWeight + right.b * bWeight,
  });
}

export function accentTone(_accent: string | null): 'spectral' | 'amber' {
  return 'spectral';
}

export function resolveIdeaAccent(source: unknown): string | null {
  if (!source || typeof source !== 'object') return null;
  return normalizeHexColor((source as { user_color?: string | null }).user_color);
}

export function resolveUserAccent(
  message: StreamItem,
  idea: Idea | null | undefined,
  currentUser?: ThreadCurrentUser | null,
): string | null {
  const ownUserColor =
    message?.user_id && currentUser?.id && String(message.user_id) === String(currentUser.id)
      ? normalizeHexColor(currentUser.color)
      : null;

  return (
    ownUserColor
    ?? normalizeHexColor(message?.user_color)
    ?? normalizeHexColor(message?.author_color)
    ?? resolveIdeaAccent(idea)
    ?? normalizeHexColor(currentUser?.color)
  );
}

export function mapThreadAttachment(att: any): CortexThreadStageAttachmentItem | null {
  const url = attachmentUrl(att);
  if (!url) return null;
  const downloadUrl = attachmentDownloadUrl(att);

  const kind = att.content_type || att.type || '';
  if (['diff', 'chart', 'preview', 'diagram', 'html', 'code', 'markdown', 'screenshot'].includes(kind)) {
    return {
      kind: 'visual',
      block: {
        type: kind,
        content: kind === 'screenshot' ? (att.content || att.url || '') : (att.content || ''),
        title: att.filename || att.name || att.title || kind,
        language: att.language,
      },
    };
  }

  const previewKind = attachmentPreviewKind(att);
  if (previewKind === 'image') {
    return {
      kind: 'image',
      url,
      downloadUrl,
      alt: attachmentLabel(att),
    };
  }

  return {
    kind: 'file',
    url,
    downloadUrl,
    label: attachmentLabel(att),
    detail: attachmentDetail(att),
    previewKind,
  };
}

export function getRunActivity(source: StreamItem | null | undefined): string | null {
  const lastActivity = typeof source?.last_activity === 'string' ? source.last_activity.trim() : '';
  if (lastActivity) return lastActivity;

  const trace = Array.isArray(source?.activity_trace) ? source.activity_trace : [];
  for (const entry of trace) {
    const activity =
      typeof entry === 'string'
        ? entry
        : typeof entry?.activity === 'string'
          ? entry.activity
          : '';
    if (activity.trim()) return activity.trim();
  }

  return null;
}

interface TranscriptMappingContext {
  idea?: Idea | null;
  themeMode: ThreadThemeMode;
  currentUser?: ThreadCurrentUser | null;
  nowMs: number;
  onApproveRun?: (runId: number) => void;
  onDenyRun?: (runId: number) => void;
}

function mapThreadMessageItem(
  item: StreamItem,
  {
    idea,
    themeMode,
    currentUser,
    nowMs,
  }: TranscriptMappingContext,
  { inlineWithWork = false }: { inlineWithWork?: boolean } = {},
): CortexThreadStageMessageItem {
  const isAgent = item.role === 'assistant' || item.role === 'illo';
  const userAccent = isAgent ? null : resolveUserAccent(item, idea, currentUser);
  const rawAttachments = Array.isArray(item.attachments) ? item.attachments : [];
  const attachments = [...rawAttachments, ...messageLinkAttachments(item.content, rawAttachments)]
    .map((attachment) => mapThreadAttachment(attachment))
    .filter(Boolean) as CortexThreadStageAttachmentItem[];

  const userShellColor = themeMode === 'light'
    ? THREAD_USER_LIGHT_SHELL_COLOR
    : THREAD_USER_DARK_SHELL_COLOR;
  const userOwnerText = themeMode === 'light'
    ? THREAD_USER_LIGHT_OWNER_TEXT
    : THREAD_USER_DARK_OWNER_TEXT;

  return {
    kind: 'message',
    id: item.id,
    role: isAgent ? 'illo' : 'user',
    author: isAgent ? 'Illo' : item.user_name || 'You',
    timestamp: timeAgo(item.timestamp, nowMs),
    tag: messageTag(item),
    tone: isAgent ? 'spectral' : accentTone(userAccent),
    accentColor: userAccent ?? undefined,
    coreColor: userAccent ? mixHex(userAccent, userShellColor, themeMode === 'light' ? 0.16 : 0.68) : undefined,
    ownerColor: userAccent ? mixHex(userAccent, userOwnerText, themeMode === 'light' ? 0.22 : 0.78) : undefined,
    html: item.content ? renderReadableMarkdown(item.content) : undefined,
    attachments: attachments.length ? attachments : undefined,
    inlineWithWork: inlineWithWork || undefined,
  };
}

function runLiveLines(item: StreamItem, fallback: string | null): CortexThreadStageRunItem['liveLines'] {
  if (item.work_log?.length) return item.work_log;
  if (item.live_lines?.length) return item.live_lines;
  return fallback ? [fallback] : [];
}

function mapThreadRunItem(
  item: StreamItem,
  {
    nowMs,
    onApproveRun,
    onDenyRun,
  }: TranscriptMappingContext,
): CortexThreadStageRunItem {
  const runActivity = getRunActivity(item);
  const runStatus = normalizeRunStatus(item.status);
  const activeRun = isActiveRun(item);
  const runId = Number(item.id);
  const canApproveRun = Boolean(item.requires_approval && Number.isFinite(runId));
  const workItems = runWorkTimelineItems(item).map((workItem) => ({
    ...workItem,
    time: workItem.at ? formatRunClock(workItem.at) : undefined,
  }));
  return {
    kind: 'run',
    id: item.id,
    status: runStatus,
    skill: item.skill_name || item.title || (isFastRun(item) ? 'Fast' : 'run'),
    event: item.title || (isFastRun(item) ? 'Fast work log' : 'Latest run'),
    summaryTitle: runWorkSummaryTitle(item),
    summarySubtitle: runWorkSummarySubtitle(item),
    defaultExpanded: activeRun || Boolean(item.requires_approval),
    timestamp: formatRunClock(item.started_at || item.timestamp) || timeAgo(item.timestamp, nowMs),
    model: item.model_used || undefined,
    thinking: item.thinking_used || undefined,
    tokens: formatCompactTokens(item.tokens_total),
    cost: formatCost(item.estimated_cost),
    telemetry: buildRunTelemetry(item),
    duration: item.duration_sec ? formatDuration(item.duration_sec) : undefined,
    error: item.error || undefined,
    requiresApproval: Boolean(item.requires_approval),
    runSteps: (item.run_steps || []).map((step: any, index: number) => ({
      id: String(
        step.id ||
          `${step.node_id || step.step_id || step.skill_name || step.label || 'step'}-${step.wave ?? 0}-${index}`,
      ),
      label: step.node_id || step.step_id || step.label || step.skill_name || 'Step',
      skill: step.skill_name || undefined,
      duration: step.duration_sec ? formatDuration(step.duration_sec) : undefined,
      tokens: formatCompactTokens(step.tokens_total),
      task: step.task || undefined,
      wave: step.wave,
      status: normalizeStepStatus(step.status),
    })),
    workItems,
    liveLines: runLiveLines(item, runActivity),
    liveLinesEyebrow: activeRun ? 'Live work' : 'Work log',
    toolCalls: (item.tool_calls || []).map((toolCall: any) => ({
      tool: toolCall.tool,
      args: toolCall.args || undefined,
      at: toolCall.at || undefined,
      status: toolCall.status || undefined,
      display: toolCall.display || undefined,
    })),
    toolCallsDefaultOpen: activeRun,
    evidenceDebug: buildRunEvidenceDebug(item) ?? undefined,
    onApprove:
      canApproveRun && onApproveRun
        ? () => onApproveRun(runId)
        : undefined,
    onDeny:
      canApproveRun && onDenyRun
        ? () => onDenyRun(runId)
        : undefined,
  };
}

function groupedRenderableLiveAgentTextItems(visibleItems: readonly StreamItem[]): Map<string, StreamItem[]> {
  const visibleRunIds = new Set<string>();
  for (const item of visibleItems) {
    if (item.type !== 'run' || !shouldShowRunInTranscript(item) || !canSplitRunLiveText(item)) continue;
    const runId = streamItemRunId(item) ?? String(item.id);
    if (runId) visibleRunIds.add(runId);
  }

  const liveTextByRun = new Map<string, StreamItem[]>();
  for (const item of visibleItems) {
    if (item.type !== 'message' || !item.metadata?.live_agent_text) continue;
    if (!shouldRenderLiveAgentTextItem(item, visibleItems)) continue;
    const runId = streamItemRunId(item);
    if (!runId || !visibleRunIds.has(runId)) continue;
    const runItems = liveTextByRun.get(runId);
    if (runItems) runItems.push(item);
    else liveTextByRun.set(runId, [item]);
  }
  return liveTextByRun;
}

function liveAgentTextItemIds(liveTextByRun: Map<string, StreamItem[]>): Set<string> {
  const ids = new Set<string>();
  for (const items of liveTextByRun.values()) {
    for (const item of items) {
      ids.add(String(item.id));
    }
  }
  return ids;
}

function canSplitRunLiveText(item: StreamItem): boolean {
  return isActiveRun(item) && !item.requires_approval && item.status !== 'pending_approval';
}

function appendChronologicalRunSegments(
  transcriptItems: CortexThreadStageTranscriptItem[],
  runItem: CortexThreadStageRunItem,
  liveTextItems: readonly StreamItem[],
  context: TranscriptMappingContext,
) {
  const segments = buildChronologicalRunSegments(runItem.workItems ?? [], liveTextItems, {
    includeTrailingCue: true,
  });
  let workSegmentIndex = 0;
  for (const segment of segments) {
    if (segment.kind === 'live_text') {
      transcriptItems.push(mapThreadMessageItem(segment.item, context, { inlineWithWork: true }));
      continue;
    }
    if (segment.items.length === 0 && !segment.showLiveCue) continue;
    workSegmentIndex += 1;
    transcriptItems.push({
      ...runItem,
      id: `${runItem.id}:work:${workSegmentIndex}`,
      workItems: segment.items,
      liveLines: [],
      showLiveCue: segment.showLiveCue,
      toolCalls: [],
    });
  }
}

export function buildThreadTranscriptItems({
  idea,
  stream,
  themeMode = 'dark',
  runInfo = null,
  latestRun = null,
  currentUser = null,
  nowMs = Date.now(),
  onApproveRun,
  onDenyRun,
}: BuildThreadTranscriptOptions): CortexThreadStageTranscriptItem[] {
  const items: CortexThreadStageTranscriptItem[] = [];
  const visibleItems = orderQueuedThreadStreamItems(visibleThreadStreamItems(stream));
  const liveTextByRun = groupedRenderableLiveAgentTextItems(visibleItems);
  const consumedLiveTextIds = liveAgentTextItemIds(liveTextByRun);
  const mappingContext: TranscriptMappingContext = {
    idea,
    themeMode,
    currentUser,
    nowMs,
    onApproveRun,
    onDenyRun,
  };

  for (const item of visibleItems) {
    if (item.type === 'message') {
      if (consumedLiveTextIds.has(String(item.id))) continue;
      if (!shouldRenderLiveAgentTextItem(item, visibleItems)) continue;
      items.push(mapThreadMessageItem(item, mappingContext));
      continue;
    }

    if (item.type === 'visual_block') {
      items.push({
        kind: 'visual',
        id: item.id,
        block: {
          type: item.content_type || 'html',
          content: item.content || '',
          title: item.title,
        },
      });
      continue;
    }

    if (item.type === 'run') {
      if (!shouldShowRunInTranscript(item)) continue;
      const runItem = mapThreadRunItem(item, mappingContext);
      const runId = streamItemRunId(item) ?? String(item.id);
      const liveTextItems = liveTextByRun.get(runId) ?? [];
      if (liveTextItems.length > 0 && canSplitRunLiveText(item)) {
        appendChronologicalRunSegments(items, runItem, liveTextItems, mappingContext);
        continue;
      }
      items.push(runItem);
    }
  }

  if (idea?.status === 'working') {
    const activeStreamRun = visibleItems.find(
      (streamItem) => streamItem.type === 'run' && isActiveRun(streamItem),
    );
    const activeRun = activeStreamRun ?? runInfo ?? latestRun;
    const activeStartedAt = activeRun?.started_at || runInfo?.started_at;
    const activeActivity = getRunActivity(activeRun);
    const activeIsFast = isFastRun(activeRun);
    const activitySteps = runActivitySteps(activeRun, {
      elapsedLabel: (iso) => elapsedLabel(iso, nowMs),
      limit: activeIsFast ? 6 : 1,
    });
    if (activeStartedAt && activeActivity && activitySteps.length === 0) {
      activitySteps.push({
        time: `${elapsedLabel(activeStartedAt, nowMs)} live`,
        label: activeActivity,
      });
    }
    const hasActiveRun = Boolean(activeRun && isActiveRun(activeRun));
    if (hasActiveRun && !(activeIsFast && activeStreamRun && shouldShowRunInTranscript(activeStreamRun))) {
      items.push({
        kind: 'thinking',
        id: `thinking-${idea.id}`,
        label: activeIsFast
          ? (activitySteps[activitySteps.length - 1]?.label || 'Illo is working...')
          : 'Illo is working through the active thread.',
        steps: activitySteps.length ? activitySteps : undefined,
      });
    }
    if (!items.length) {
      items.push({
        kind: 'thinking',
        id: `thinking-${idea.id}`,
        label: 'Illo is syncing the active work...',
      });
    }
  }

  return items;
}

function messageTag(item: StreamItem): string | undefined {
  if (item.metadata?.context_submission_id || item.message_type === 'context_submission') return 'Context';
  if (item.metadata?.fast_steer) return 'Steering';
  if (item.metadata?.queued_after_run) return 'Queued';
  return undefined;
}
