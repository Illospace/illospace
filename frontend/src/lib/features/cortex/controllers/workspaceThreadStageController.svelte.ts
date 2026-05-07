import type { ThreadPeripherySignal } from '$lib/features/threads/components/ThreadStageShell.svelte';
import type { Connection, Idea } from '$lib/types/cortex';
import { clamp } from '$lib/utils/math';

export type ThreadStageOrigin = { x: number | string; y: number | string };
type ThreadEdgeSide = ThreadPeripherySignal['side'];
type ThreadSignalKind = ThreadPeripherySignal['kind'];

const THREAD_TONE_BY_STATUS: Record<string, string> = {
  idle: '#57CFA0',
  working: '#E3AA54',
  done: '#57CFA0',
};

const THREAD_SIGNAL_PROFILE: Record<string, { color: string; kind: ThreadSignalKind; weight: number; pulseMs: number }> = {
  working: { color: '#E3AA54', kind: 'progress', weight: 0.72, pulseMs: 4400 },
  done: { color: '#5EA9FF', kind: 'attention', weight: 0.82, pulseMs: 3600 },
  idle: { color: '#57CFA0', kind: 'progress', weight: 0.46, pulseMs: 5600 },
};

export function hexToRgb(hex: string) {
  const clean = hex.replace('#', '');
  const normalized = clean.length === 3
    ? clean.split('').map((char) => char + char).join('')
    : clean;
  const num = Number.parseInt(normalized, 16);
  return `${(num >> 16) & 255}, ${(num >> 8) & 255}, ${num & 255}`;
}

export function buildThreadAmbientTone(status = 'idle') {
  const color = THREAD_TONE_BY_STATUS[status] ?? '#57CFA0';
  return {
    color,
    rgb: hexToRgb(color),
  };
}

export function buildThreadWorkspaceStyle(origin: ThreadStageOrigin | null) {
  const originX = origin?.x ?? '50%';
  const originY = origin?.y ?? '56%';
  return `--thread-origin-x:${originX}; --thread-origin-y:${originY};`;
}

function ideaPosition(idea: Idea | null | undefined) {
  const withLiveCoordinates = idea as (Idea & { x?: number; y?: number }) | null | undefined;
  const x = typeof withLiveCoordinates?.x === 'number' ? withLiveCoordinates.x : withLiveCoordinates?.position_x;
  const y = typeof withLiveCoordinates?.y === 'number' ? withLiveCoordinates.y : withLiveCoordinates?.position_y;
  if (typeof x !== 'number' || typeof y !== 'number') return null;
  return { x, y };
}

function edgeSideFor(dx: number, dy: number): ThreadEdgeSide {
  return Math.abs(dx) > Math.abs(dy)
    ? (dx >= 0 ? 'right' : 'left')
    : (dy >= 0 ? 'bottom' : 'top');
}

function edgeOffsetFor(side: ThreadEdgeSide, dx: number, dy: number) {
  const dominant = Math.max(Math.abs(dx), Math.abs(dy), 1);
  const ratio = side === 'left' || side === 'right' ? dy / dominant : dx / dominant;
  return clamp(50 + ratio * 28, 16, 84);
}

export function buildThreadPeripherySignals({
  directThreadActive,
  selectedIdea,
  ideas,
  connections,
}: {
  directThreadActive: boolean;
  selectedIdea: Idea | null | undefined;
  ideas: readonly Idea[];
  connections: readonly Connection[];
}): ThreadPeripherySignal[] {
  if (directThreadActive) return [];

  const selectedPos = selectedIdea ? ideaPosition(selectedIdea) : null;
  if (!selectedIdea || !selectedPos) return [];

  const relatedIds = new Set<string>();
  for (const connection of connections) {
    if (connection.source_id === selectedIdea.id) relatedIds.add(connection.target_id);
    if (connection.target_id === selectedIdea.id) relatedIds.add(connection.source_id);
  }

  const buckets = new Map<ThreadEdgeSide, Array<{
    score: number;
    offset: number;
    color: string;
    rgb: string;
    pulseMs: number;
    kind: ThreadSignalKind;
    related: boolean;
  }>>();

  for (const idea of ideas) {
    if (!idea || idea.id === selectedIdea.id) continue;

    const profile = THREAD_SIGNAL_PROFILE[idea.status];
    if (!profile) continue;

    const pos = ideaPosition(idea);
    if (!pos) continue;

    const dx = pos.x - selectedPos.x;
    const dy = pos.y - selectedPos.y;
    if (Math.abs(dx) < 8 && Math.abs(dy) < 8) continue;

    const side = edgeSideFor(dx, dy);
    const distance = Math.sqrt(dx * dx + dy * dy);
    const distanceBoost = clamp(1 - distance / 900, 0, 1) * 0.08;
    const related = relatedIds.has(idea.id);
    const score = profile.weight + distanceBoost + (related ? 0.16 : 0);
    const entry = {
      score,
      offset: edgeOffsetFor(side, dx, dy),
      color: profile.color,
      rgb: hexToRgb(profile.color),
      pulseMs: profile.pulseMs,
      kind: profile.kind,
      related,
    };
    const bucket = buckets.get(side) ?? [];
    bucket.push(entry);
    buckets.set(side, bucket);
  }

  const sides: ThreadEdgeSide[] = ['top', 'right', 'bottom', 'left'];
  return sides.flatMap((side) => {
    const bucket = buckets.get(side);
    if (!bucket?.length) return [];

    bucket.sort((a, b) => b.score - a.score);
    const strongest = bucket[0];
    const totalWeight = bucket.reduce((sum, item) => sum + item.score, 0);
    const weightedOffset = bucket.reduce((sum, item) => sum + item.offset * item.score, 0) / totalWeight;
    const count = bucket.length;
    const strength = clamp(strongest.score + Math.min((count - 1) * 0.08, 0.18), 0.28, 1);
    return [{
      side,
      offset: weightedOffset,
      strength,
      color: strongest.color,
      rgb: strongest.rgb,
      pulseMs: strongest.pulseMs,
      kind: strongest.kind,
      count,
      related: bucket.some((item) => item.related),
      span: Math.round(clamp(46 + count * 12 + strength * 18, 54, 106)),
      opacity: clamp(0.16 + strength * 0.16 + Math.min((count - 1) * 0.02, 0.05), 0.18, 0.42),
    }];
  });
}

export class WorkspaceThreadStageController {
  origin = $state<ThreadStageOrigin | null>(null);
  entering = $state(false);
  ready = $state(false);
  previewOpen = $state(false);
  dockWidth = $state(560);
  private wasPanelOpen = false;
  private revealTimer: ReturnType<typeof setTimeout> | null = null;
  private settledTimer: ReturnType<typeof setTimeout> | null = null;

  get workspaceStyle() {
    return buildThreadWorkspaceStyle(this.origin);
  }

  setCenteredOrigin(x: number | string = '50%', y: number | string = '56%') {
    this.origin = { x, y };
  }

  setOriginFromClient(workspaceEl: HTMLElement | undefined, clientX: number, clientY: number) {
    const rect = workspaceEl?.getBoundingClientRect();
    if (!rect) {
      this.origin = null;
      return;
    }
    this.origin = {
      x: `${clientX - rect.left}px`,
      y: `${clientY - rect.top}px`,
    };
  }

  syncPanelOpen(isOpen: boolean) {
    if (isOpen && !this.wasPanelOpen) {
      if (!this.origin) {
        this.setCenteredOrigin();
      }
      this.previewOpen = false;
      this.clearTimers();
      this.entering = true;
      this.ready = false;
      this.revealTimer = setTimeout(() => {
        this.ready = true;
        this.revealTimer = null;
      }, 90);
      this.settledTimer = setTimeout(() => {
        this.entering = false;
        this.settledTimer = null;
      }, 620);
    } else if (!isOpen && this.wasPanelOpen) {
      this.clearTimers();
      this.entering = false;
      this.ready = false;
      this.origin = null;
    }
    this.wasPanelOpen = isOpen;
  }

  cleanup() {
    this.clearTimers();
  }

  private clearTimers() {
    if (this.revealTimer) clearTimeout(this.revealTimer);
    if (this.settledTimer) clearTimeout(this.settledTimer);
    this.revealTimer = null;
    this.settledTimer = null;
  }
}

export function createWorkspaceThreadStageController(): WorkspaceThreadStageController {
  return new WorkspaceThreadStageController();
}
