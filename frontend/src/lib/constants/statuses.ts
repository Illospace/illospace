export const ACTIVE_AGENT_RUN_STATUSES = [
  'starting',
  'running',
  'paused',
  'verifying',
] as const;

export const OPEN_AGENT_RUN_STATUSES = [
  'queued',
  ...ACTIVE_AGENT_RUN_STATUSES,
] as const;

export const APPROVAL_RUN_STATUSES = ['pending_approval'] as const;

export const LIVE_RUN_STATUSES = [
  ...OPEN_AGENT_RUN_STATUSES,
  ...APPROVAL_RUN_STATUSES,
] as const;

export const FAST_TRANSCRIPT_VISIBLE_RUN_STATUSES = [
  ...LIVE_RUN_STATUSES,
  'completed',
  'failed',
  'canceled',
] as const;

export const RUN_STATUS_RANK: Record<string, number> = {
  queued: 1,
  starting: 2,
  running: 3,
  paused: 4,
  pending_approval: 5,
  verifying: 6,
};

export const WORKING_IDEA_STATUSES = [
  'queued',
  'working',
  'running',
] as const;

export const DONE_IDEA_STATUSES = [
  'completed',
  'pending_approval',
  'needs_input',
  'unread_reply',
  'failed',
  'canceled',
  'cancelled',
  'superseded',
  'timeout',
  'blocked',
  'done',
] as const;
