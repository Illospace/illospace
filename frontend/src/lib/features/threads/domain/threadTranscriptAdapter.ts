import type { Snippet } from 'svelte';

import type { AttachmentPreviewKind } from '$lib/utils/attachmentPreview';
import type { RunEvidenceDebug } from '$lib/utils/runEvidenceDebug';

export const CORTEX_THREAD_STAGE_TONES = ['spectral', 'amber'] as const;
export type CortexThreadStageTone = (typeof CORTEX_THREAD_STAGE_TONES)[number];

export const CORTEX_THREAD_STAGE_MESSAGE_ROLES = ['user', 'illo'] as const;
export type CortexThreadStageMessageRole = (typeof CORTEX_THREAD_STAGE_MESSAGE_ROLES)[number];

export const CORTEX_THREAD_STAGE_RUN_STATUSES = [
  'queued',
  'starting',
  'running',
  'completed',
  'failed',
  'canceled',
  'pending_approval',
] as const;
export type CortexThreadStageRunStatus =
  (typeof CORTEX_THREAD_STAGE_RUN_STATUSES)[number];
export type CortexThreadStageHeaderStatusState = 'idle' | 'working' | 'unread';

export const CORTEX_THREAD_STAGE_RUN_STEP_STATUSES = [
  'pending',
  'running',
  'completed',
  'failed',
  'skipped',
] as const;
export type CortexThreadStageRunStepStatus =
  (typeof CORTEX_THREAD_STAGE_RUN_STEP_STATUSES)[number];

export interface CortexThreadStageHeaderConfig {
  title: string;
  statusLabel: string;
  statusState?: CortexThreadStageHeaderStatusState;
  titleActionLabel?: string;
  titleActionLoading?: boolean;
  onTitleAction?: () => void;
  archiveActionLabel?: string;
  archiveActionLoading?: boolean;
  onArchiveAction?: () => void;
  panelOpen?: boolean;
  onTogglePanel?: () => void;
  secondaryPanelOpen?: boolean;
  onToggleSecondaryPanel?: () => void;
  runLabel?: string;
  runStatus?: string;
  runEvent?: string;
  runTime?: string;
  panelLabel?: string;
  secondaryPanelLabel?: string;
}

export interface CortexThreadStageMessageSection {
  heading?: string;
  paragraphs?: readonly string[];
  points?: readonly string[];
  ordered?: boolean;
}

export interface CortexThreadStageMessageItem {
  kind: 'message';
  id?: string | number;
  role?: CortexThreadStageMessageRole;
  author: string;
  timestamp?: string;
  tag?: string;
  tone?: CortexThreadStageTone;
  accentColor?: string;
  coreColor?: string;
  ownerColor?: string;
  html?: string;
  paragraphs?: readonly string[];
  sections?: readonly CortexThreadStageMessageSection[];
  attachments?: readonly CortexThreadStageAttachmentItem[];
}

export interface CortexThreadStageRunStep {
  id: string;
  label: string;
  skill?: string;
  duration?: string;
  tokens?: string;
  task?: string;
  wave?: number;
  status: CortexThreadStageRunStepStatus;
}

export interface CortexThreadStageWorkerLane {
  skill: string;
  task: string;
  tokens?: string;
  agent?: string;
}

export type CortexThreadStageLiveLine =
  | string
  | {
      time?: string;
      text: string;
    };

export interface CortexThreadStageToolCall {
  tool: string;
  args?: string;
  at?: string;
  status?: string;
  display?: Record<string, any>;
}

export type CortexThreadStageWorkTimelineItem =
  | {
      kind: 'thought';
      time?: string;
      at?: string;
      text: string;
    }
  | {
      kind: 'tool';
      time?: string;
      at?: string;
      tool: string;
      args?: string;
      status?: string;
      error?: string;
      result?: string;
      finishedAt?: string;
      display?: Record<string, any>;
    };

export interface CortexThreadStageRunTelemetryItem {
  label: string;
  value: string;
}

export interface CortexThreadStageRunItem {
  kind: 'run';
  id?: string | number;
  status: CortexThreadStageRunStatus;
  skill: string;
  summaryTitle?: string;
  summarySubtitle?: string;
  defaultExpanded?: boolean;
  event?: string;
  timestamp: string;
  model?: string;
  thinking?: string;
  tokens?: string;
  cost?: string;
  duration?: string;
  error?: string;
  telemetry?: readonly CortexThreadStageRunTelemetryItem[];
  requiresApproval?: boolean;
  runSteps?: readonly CortexThreadStageRunStep[];
  graphEyebrow?: string;
  graphDefaultExpanded?: boolean;
  workerLanes?: readonly CortexThreadStageWorkerLane[];
  workerEyebrow?: string;
  workItems?: readonly CortexThreadStageWorkTimelineItem[];
  showLiveCue?: boolean;
  liveLines?: readonly CortexThreadStageLiveLine[];
  liveLinesEyebrow?: string;
  toolCalls?: readonly CortexThreadStageToolCall[];
  toolCallsTitle?: string;
  toolCallsDefaultOpen?: boolean;
  evidenceDebug?: RunEvidenceDebug;
  onApprove?: () => void;
  onDeny?: () => void;
}

export interface CortexThreadStageThinkingStep {
  time?: string;
  label: string;
}

export interface CortexThreadStageThinkingItem {
  kind: 'thinking';
  id?: string | number;
  label?: string;
  toolCount?: number;
  steps?: readonly CortexThreadStageThinkingStep[];
}

export interface CortexThreadStageVisualAttachment {
  kind: 'visual';
  block: {
    type: string;
    content: string;
    title?: string;
    language?: string;
  };
}

export interface CortexThreadStageImageAttachment {
  kind: 'image';
  url: string;
  downloadUrl?: string;
  alt: string;
}

export interface CortexThreadStageFileAttachment {
  kind: 'file';
  url: string;
  downloadUrl?: string;
  label: string;
  detail?: string;
  previewKind?: AttachmentPreviewKind;
}

export type CortexThreadStageAttachmentItem =
  | CortexThreadStageVisualAttachment
  | CortexThreadStageImageAttachment
  | CortexThreadStageFileAttachment;

export interface CortexThreadStageVisualReplyItem {
  kind: 'visual';
  id?: string | number;
  block: {
    type: string;
    content: string;
    title?: string;
    language?: string;
  };
}

export type CortexThreadStageTranscriptItem =
  | CortexThreadStageMessageItem
  | CortexThreadStageRunItem
  | CortexThreadStageThinkingItem
  | CortexThreadStageVisualReplyItem;

export interface ThreadTranscriptProps {
  header?: CortexThreadStageHeaderConfig | null;
  transcriptItems?: readonly CortexThreadStageTranscriptItem[];
  loading?: boolean;
  loadingLabel?: string;
  emptyLabel?: string;
  showReplyDock?: boolean;
  showScrollCue?: boolean;
  replyPlaceholder?: string;
  replyHint?: string;
  className?: string;
  headerSlot?: Snippet;
  transcriptSlot?: Snippet;
  renderTranscriptItem?: Snippet<[CortexThreadStageTranscriptItem]>;
  replyDock?: Snippet;
  onTranscriptScroll?: (event: Event) => void;
  onTranscriptReady?: (element: HTMLDivElement | undefined) => void;
  onScrollToBottom?: () => void;
  onPreviewAttachment?: (attachment: CortexThreadStageImageAttachment | CortexThreadStageFileAttachment) => void;
}

export function getCortexThreadRunStatusLabel(status: CortexThreadStageRunStatus): string {
  switch (status) {
    case 'queued':
      return 'Queued';
    case 'starting':
      return 'Starting';
    case 'running':
      return 'Running';
    case 'completed':
      return 'Completed';
    case 'failed':
      return 'Failed';
    case 'canceled':
      return 'Canceled';
    case 'pending_approval':
      return 'Approval';
    default:
      return status;
  }
}

export function getCortexThreadRunStatusGlyph(status: CortexThreadStageRunStatus): string {
  switch (status) {
    case 'starting':
      return '◌';
    case 'running':
      return '◎';
    case 'completed':
      return '✓';
    case 'failed':
      return '×';
    case 'canceled':
      return '×';
    case 'pending_approval':
      return '◔';
    default:
      return '○';
  }
}

export function getCortexThreadStepStatusGlyph(status: CortexThreadStageRunStepStatus): string {
  switch (status) {
    case 'completed':
      return '✓';
    case 'running':
      return '◎';
    case 'failed':
      return '×';
    case 'skipped':
      return '—';
    default:
      return '○';
  }
}

export function orderCortexThreadRunSteps(
  steps: readonly CortexThreadStageRunStep[],
): CortexThreadStageRunStep[] {
  return [...steps]
    .map((statusStep, index) => ({ statusStep, index }))
    .sort((left, right) => {
      const waveDiff = (left.statusStep.wave ?? 0) - (right.statusStep.wave ?? 0);
      if (waveDiff !== 0) return waveDiff;
      return left.index - right.index;
    })
    .map(({ statusStep }) => statusStep);
}

export function summarizeCortexThreadRunSteps(
  steps: readonly CortexThreadStageRunStep[],
): string {
  if (steps.length === 1) {
    const [statusStep] = steps;

    switch (statusStep.status) {
      case 'completed':
        return `${statusStep.label} complete`;
      case 'running':
        return `${statusStep.label} running`;
      case 'failed':
        return `${statusStep.label} failed`;
      case 'skipped':
        return `${statusStep.label} skipped`;
      default:
        return `${statusStep.label} queued`;
    }
  }

  const counts = steps.reduce(
    (summary, statusStep) => {
      summary[statusStep.status] += 1;
      return summary;
    },
    { completed: 0, running: 0, failed: 0, pending: 0, skipped: 0 },
  );

  if (counts.failed > 0) {
    const tail = counts.running > 0 ? `${counts.running} running` : `${counts.completed} complete`;
    return tail !== '0 complete' ? `${counts.failed} failed, ${tail}` : `${counts.failed} failed`;
  }

  if (counts.running > 0) {
    const tail = counts.completed > 0 ? `${counts.completed} complete` : `${counts.pending} queued`;
    return tail !== '0 queued' ? `${counts.running} running, ${tail}` : `${counts.running} running`;
  }

  if (counts.completed === steps.length) return 'All complete';
  if (counts.pending === steps.length) return 'Queued';

  const parts = [
    counts.completed > 0 ? `${counts.completed} complete` : '',
    counts.pending > 0 ? `${counts.pending} queued` : '',
    counts.skipped > 0 ? `${counts.skipped} skipped` : '',
  ].filter(Boolean);

  return parts.join(', ');
}

export function getCortexThreadRunStepTone(
  steps: readonly CortexThreadStageRunStep[],
): 'default' | 'completed' | 'running' | 'failed' {
  if (steps.some((statusStep) => statusStep.status === 'failed')) return 'failed';
  if (steps.some((statusStep) => statusStep.status === 'running')) return 'running';
  if (steps.length > 0 && steps.every((statusStep) => statusStep.status === 'completed')) return 'completed';
  return 'default';
}

export function normalizeCortexThreadLiveLine(entry: CortexThreadStageLiveLine): {
  time?: string;
  text: string;
} {
  if (typeof entry === 'string') {
    return { text: entry };
  }

  return entry;
}
