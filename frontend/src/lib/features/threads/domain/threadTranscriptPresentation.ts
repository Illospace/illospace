import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
import { LIVE_RUN_STATUSES, OPEN_AGENT_RUN_STATUSES } from '$lib/constants/statuses';
import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';

import {
  getCortexThreadRunStepTone,
  type CortexThreadStageAttachmentItem,
  type CortexThreadStageFileAttachment,
  type CortexThreadStageHeaderConfig,
  type CortexThreadStageImageAttachment,
  type CortexThreadStageMessageItem,
  type CortexThreadStageRunItem,
  type CortexThreadStageThinkingItem,
  type CortexThreadStageTone,
  type CortexThreadStageWorkTimelineItem,
} from './threadTranscriptAdapter';

export const RUN_INLINE_SECTIONS = ['graph', 'tools', 'evidence'] as const;
export type RunInlineSection = (typeof RUN_INLINE_SECTIONS)[number];

const THINKING_STATUS_LABEL = 'Thinking';

type TimelineToolItem = Extract<CortexThreadStageWorkTimelineItem, { kind: 'tool' }>;
type ToolCallItem = NonNullable<CortexThreadStageRunItem['toolCalls']>[number];

export function getMessageTone(item: CortexThreadStageMessageItem): CortexThreadStageTone {
  return item.tone ?? 'spectral';
}

export function getStepToneClass(item: CortexThreadStageRunItem) {
  return getCortexThreadRunStepTone(item.runSteps ?? []);
}

export function getRunClass(item: CortexThreadStageRunItem) {
  return [
    'run-insert',
    `run-${item.status}`,
    item.workItems?.length ? 'run-with-work-timeline' : '',
  ].filter(Boolean).join(' ');
}

export function getRunKey(item: CortexThreadStageRunItem, index: number) {
  return String(item.id ?? `run-${index}`);
}

export function isRunActiveStatus(status: string | undefined) {
  return Boolean(status && (LIVE_RUN_STATUSES as readonly string[]).includes(status));
}

export function getRunDefaultExpanded(item: CortexThreadStageRunItem) {
  return item.defaultExpanded ?? Boolean(item.requiresApproval || isRunActiveStatus(item.status));
}

export function isRunLiveWorkStream(item: CortexThreadStageRunItem) {
  if (item.requiresApproval || item.status === 'pending_approval') return false;
  return (OPEN_AGENT_RUN_STATUSES as readonly string[]).includes(item.status);
}

export function getThreadHeaderStatusState(header: CortexThreadStageHeaderConfig | null | undefined) {
  if (header?.statusState) return header.statusState;

  const status = header?.statusLabel?.toLowerCase() ?? '';
  const runStatus = header?.runStatus?.toLowerCase() ?? '';
  if (status.includes('working') || OPEN_AGENT_RUN_STATUSES.some((value) => runStatus.includes(value))) {
    return 'working';
  }
  if (status.includes('unread') || status.includes('done')) return 'unread';
  return 'idle';
}

export function getThreadHeaderStatusLabel(header: CortexThreadStageHeaderConfig | null | undefined) {
  const state = getThreadHeaderStatusState(header);
  if (state === 'working') return 'Illo is working';
  if (state === 'unread') return 'Unread thread';
  return 'Idle thread';
}

export function getRunSectionKey(runKey: string, section: RunInlineSection) {
  return `${runKey}:${section}`;
}

export function parseTimelineToolArgs(args: string | undefined): Record<string, unknown> | null {
  if (!args) return null;
  try {
    const parsed = JSON.parse(args);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null;
  } catch {
    return null;
  }
}

function compactTimelineTarget(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const text = value.trim();
  if (!text) return undefined;
  return text.length > 96 ? `...${text.slice(-93)}` : text;
}

export function getTimelineToolTarget(item: TimelineToolItem) {
  const displayTarget = typeof item.display?.target === 'string' ? item.display.target.trim() : '';
  if (displayTarget) return compactTimelineTarget(displayTarget);
  const args = parseTimelineToolArgs(item.args);
  if (!args) return undefined;
  return compactTimelineTarget(
    args.path ??
      args.file_path ??
      args.filename ??
      args.cwd ??
      args.url ??
      args.query ??
      args.command ??
      args.cmd,
  );
}

export function getTimelineToolLabel(item: TimelineToolItem) {
  const displayLabel = typeof item.display?.label === 'string' ? item.display.label.trim() : '';
  if (displayLabel) return displayLabel;
  const tool = item.tool.trim() || 'tool';
  const target = getTimelineToolTarget(item);
  const normalized = tool.toLowerCase();

  if (/(edit|patch|update)/.test(normalized) && target) return `Editing ${target}`;
  if (/(write|create|save)/.test(normalized) && target) return `Writing ${target}`;
  if (/(read|view|open|load)/.test(normalized) && target) return `Reading ${target}`;
  if (/(exec|command|shell|terminal|bash)/.test(normalized)) return target ? `Ran ${target}` : 'Ran command';
  if (target) return `${tool} ${target}`;
  return item.status === 'running' ? `Using ${tool}` : `Used ${tool}`;
}

export function getRunLiveCueWorkIndex(item: CortexThreadStageRunItem) {
  const workItems = item.workItems ?? [];
  for (let index = workItems.length - 1; index >= 0; index -= 1) {
    const workItem = workItems[index];
    if (workItem.kind === 'tool' && workItem.status === 'running') return index;
  }
  return -1;
}

export function getRunLiveCueLabel(item: CortexThreadStageRunItem, cueIndex = getRunLiveCueWorkIndex(item)) {
  if (cueIndex >= 0) {
    const workItem = item.workItems?.[cueIndex];
    if (workItem?.kind === 'tool') return getTimelineToolLabel(workItem);
  }

  return THINKING_STATUS_LABEL;
}

export function shouldRenderLiveWorkItem(workIndex: number, cueIndex: number) {
  return workIndex !== cueIndex;
}

export function hasVisibleLiveWorkItems(item: CortexThreadStageRunItem, cueIndex: number) {
  return Boolean(item.workItems?.some((_workItem, workIndex) => shouldRenderLiveWorkItem(workIndex, cueIndex)));
}

export function getTimelineToolTitle(item: TimelineToolItem) {
  const parts = [getTimelineToolLabel(item), item.tool, item.status, item.time].filter(Boolean);
  return parts.join(' · ');
}

export function getTimelineToolDetail(item: TimelineToolItem) {
  return typeof item.display?.detail === 'string' ? item.display.detail.trim() : '';
}

export function shouldShowTimelineToolArgs(item: TimelineToolItem) {
  return Boolean(item.args && !item.display?.label && !getTimelineToolTarget(item));
}

function stripReflectionPrefix(text: string) {
  return text.trim().replace(/^Reflecting:\s*/i, '').trim();
}

export function getWorkThoughtText(text: string) {
  const cleaned = stripReflectionPrefix(text) || text.trim();
  const boldMatch = cleaned.match(/^\*\*([^*]+)\*\*\s*([\s\S]*)$/);
  if (!boldMatch) return cleaned;

  const title = boldMatch[1]?.trim();
  const tail = (boldMatch[2] ?? '').trim();
  if (!title) return cleaned;
  if (!tail || tail.length < 12 || /^[A-Za-z][.,;:]?$/.test(tail)) return `**${title}**`;
  return cleaned;
}

export function getToolCallDisplay(call: ToolCallItem) {
  return call.display && typeof call.display === 'object' ? call.display : null;
}

export function getToolCallLabel(call: ToolCallItem) {
  const display = getToolCallDisplay(call);
  const label = typeof display?.label === 'string' ? display.label.trim() : '';
  if (label) return label;
  return call.tool;
}

export function getToolCallDetail(call: ToolCallItem) {
  const display = getToolCallDisplay(call);
  const detail = typeof display?.detail === 'string' ? display.detail.trim() : '';
  if (detail) return detail;
  return call.args;
}

export function getWorkThoughtHtml(text: string) {
  return renderReadableMarkdown(getWorkThoughtText(text));
}

export function getWorkThoughtClass(text: string) {
  return [
    'run-work-item',
    'run-work-thought',
    /^Reflecting:/i.test(text.trim()) ? 'run-work-reflection' : '',
  ].filter(Boolean).join(' ');
}

export function getThinkingStatusLabel(item: CortexThreadStageThinkingItem) {
  const latestStep = item.steps?.at(-1)?.label?.trim();
  return latestStep || item.label || THINKING_STATUS_LABEL;
}

export function getThinkingSteps(item: CortexThreadStageThinkingItem) {
  const steps = [...(item.steps ?? [])];
  const latestStep = steps.at(-1);
  if (latestStep?.label?.trim() && latestStep.label.trim() === getThinkingStatusLabel(item)) {
    return steps.slice(0, -1);
  }
  return steps;
}

export function getMessageClass(item: CortexThreadStageMessageItem) {
  const role = item.role ?? 'illo';
  const tone = getMessageTone(item);

  return [
    'thread-message',
    role === 'user' ? 'thread-message-user' : 'thread-message-illo',
    role === 'user' ? `thread-message-${tone}` : '',
    item.inlineWithWork ? 'thread-message-inline-work' : '',
  ]
    .filter(Boolean)
    .join(' ');
}

export function isIlloMessage(item: CortexThreadStageMessageItem) {
  return (item.role ?? 'illo') === 'illo';
}

export function hasMessageSupplementalMeta(item: CortexThreadStageMessageItem) {
  return Boolean(item.timestamp || item.tag);
}

function normalizeHexColor(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(trimmed)) return null;
  if (trimmed.length === 4) {
    return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
  }
  return trimmed;
}

export function getUserPresenceStyle(item: CortexThreadStageMessageItem) {
  if ((item.role ?? 'illo') !== 'user') return undefined;
  const accent = normalizeHexColor(item.accentColor);
  const core = normalizeHexColor(item.coreColor);
  const owner = normalizeHexColor(item.ownerColor);
  if (!accent) return undefined;

  const seedCore =
    core ??
    `color-mix(in srgb, ${accent} var(--constellation-presence-seed-user-core-accent-strength, 52%), var(--constellation-presence-seed-user-core-base, #050910))`;
  const seedOwner =
    owner ??
    `color-mix(in srgb, ${accent} var(--constellation-presence-seed-user-owner-accent-strength, 18%), var(--constellation-presence-seed-user-owner-base, #f0f0fa))`;

  return [
    `--seed-accent:${accent}`,
    `--seed-core:${seedCore}`,
    `--seed-owner:${seedOwner}`,
  ].join('; ');
}

export function getAttachmentKey(attachment: CortexThreadStageAttachmentItem, index: number) {
  if (attachment.kind === 'visual') {
    return attachment.block.title || `${attachment.block.type}-${index}`;
  }

  return attachment.url || `${attachment.kind}-${index}`;
}

export function attachmentPreviewLabel(attachment: CortexThreadStageImageAttachment | CortexThreadStageFileAttachment) {
  return attachment.kind === 'image' ? attachment.alt : attachment.label;
}

export function attachmentPreviewType(
  attachment: CortexThreadStageImageAttachment | CortexThreadStageFileAttachment,
) {
  return attachment.kind === 'image' ? 'image' : (attachment.previewKind ?? 'file');
}

export function attachmentIconName(attachment: CortexThreadStageImageAttachment | CortexThreadStageFileAttachment): ConstellationIconName {
  const kind = attachmentPreviewType(attachment);
  if (kind === 'image') return 'image';
  if (kind === 'video') return 'video';
  if (kind === 'pdf') return 'pdf';
  if (kind === 'link') return 'link';
  if (kind === 'archive') return 'archive';
  if (kind === 'text') return 'code';
  if (kind === 'file') return 'file';
  return 'document';
}
