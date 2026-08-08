const CANVAS_OCCUPANCY_STATUSES = new Set([
  'active',
  'failed',
  'needs_input',
  'unread_reply',
  'working',
]);

export function isCanvasOccupant(idea: {
  status?: string | null;
  lifecycle_status?: string | null;
  archived_at?: string | null;
}): boolean {
  if (idea.archived_at) return false;
  const lifecycleStatus = String(idea.lifecycle_status || '').trim().toLowerCase();
  if (lifecycleStatus) return CANVAS_OCCUPANCY_STATUSES.has(lifecycleStatus);
  // A local composer idea has no backend lifecycle state until its first save.
  return String(idea.status || '').trim().toLowerCase() === 'idle';
}

export function canvasOriginCue(origin: string | null | undefined): string {
  const normalized = String(origin || 'unknown').trim().toLowerCase();
  if (normalized === 'user_created') return 'YOU';
  const source = normalized.split(/[.:]/, 1)[0] || 'unknown';
  const labels: Record<string, string> = {
    codex: 'CODEX',
    cycle_run: 'CYCLE',
    github: 'GH',
    illo_created: 'ILLO',
    inbound_signal: 'INBOUND',
    jira: 'JIRA',
    meetbot: 'MEET',
    slack: 'SLACK',
    uwear: 'UWEAR',
  };
  return labels[normalized] ?? labels[source] ?? source.replace(/_+$/g, '').slice(0, 6).toUpperCase();
}
