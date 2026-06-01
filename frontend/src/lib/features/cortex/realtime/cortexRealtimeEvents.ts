import type {
  BrowserFrame,
  BrowserSessionState,
  Connection,
  Idea,
  VaultSecretPrompt,
} from '$lib/types/cortex';

export const CORTEX_RUN_REALTIME_EVENT_TYPES = [
  'run_started',
  'step_started',
  'tool_started',
  'tool_finished',
  'text_delta',
  'run_completed',
] as const;

export const CORTEX_REALTIME_EVENT_TYPES = [
  'status_change',
  'thread_message',
  'thread_discussion_comment',
  'visual_reply',
  'browser_session_state',
  'browser_session_frame',
  'browser_session_delta',
  'browser_session_closed',
  'browser_session_error',
  'cycles_changed',
  'vault_secret_prompt',
  'idea_created',
  'idea_upserted',
  'title_generated',
  'thread_read_model_updated',
  'budget_approval_needed',
  'mention',
  'thought_split',
  'idea_updated',
  'idea_archived',
  'idea_restored',
  'connection_created',
  'connection_deleted',
  'ops_update',
  'typing',
  ...CORTEX_RUN_REALTIME_EVENT_TYPES,
] as const;

export type CortexRunRealtimeEventType = typeof CORTEX_RUN_REALTIME_EVENT_TYPES[number];
export type CortexRealtimeEventType = typeof CORTEX_REALTIME_EVENT_TYPES[number];

export type CortexRealtimePayloadByType = {
  status_change: { idea_id: string; new_status: string };
  thread_message: {
    idea_id?: string;
    run_id?: string | number | null;
    message?: Record<string, unknown> & {
      id?: string | number;
      created_at?: string;
      content?: string | null;
      metadata?: Record<string, unknown> | null;
    };
  };
  thread_discussion_comment: {
    idea_id?: string;
    org_id?: string;
    comment?: {
      id?: string | number;
      thread_id?: string;
      org_id?: string;
      author_user_id?: string | null;
      author_kind?: string;
      author_name?: string | null;
      author_color?: string | null;
      body?: string;
      attachments?: unknown[];
      metadata?: Record<string, unknown> | null;
      created_at?: string | null;
    };
  };
  visual_reply: {
    idea_id?: string;
    block?: Record<string, unknown> & {
      id?: string | number;
      created_at?: string;
      content_type?: string;
      title?: string;
      content?: string;
      display_mode?: string;
      run_id?: number;
      position_after?: number;
    };
  };
  browser_session_state: { idea_id?: string; state?: BrowserSessionState | null };
  browser_session_frame: { idea_id?: string; state?: BrowserSessionState | null; frame?: BrowserFrame | null };
  browser_session_delta: Record<string, unknown>;
  browser_session_closed: { idea_id?: string; session_id?: string };
  browser_session_error: { session_id?: string; error?: string };
  cycles_changed: {
    action?: string;
    cycle_id?: string | number | null;
    idea_id?: string | null;
    target_idea_id?: string | null;
  };
  vault_secret_prompt: VaultSecretPrompt | Record<string, unknown>;
  idea_created: { idea_id?: string };
  idea_upserted: { idea?: Idea };
  title_generated: { idea_id?: string; title?: string };
  thread_read_model_updated: {
    idea_id?: string;
    thread_id?: string;
    title?: string | null;
    preview_summary?: string | null;
    preview_source?: string | null;
    preview_updated_at?: string | null;
    thread_route?: string | null;
    thread_url?: string | null;
  };
  budget_approval_needed: { summary?: string };
  mention: { idea_title?: string };
  thought_split: { children?: Idea[] };
  idea_updated: { idea_id?: string; fields?: Partial<Idea> };
  idea_archived: { idea_id?: string; idea?: Partial<Idea> };
  idea_restored: { idea_id?: string; idea?: Idea };
  connection_created: { connection?: Connection };
  connection_deleted: { connection_id?: string };
  ops_update: { runs?: unknown[] };
  typing: { user_id?: string; idea_id?: string };
  run_started: Record<string, unknown> & { idea_id?: string; run_id?: string | number; root_run_id?: string | number };
  step_started: Record<string, unknown> & { idea_id?: string; run_id?: string | number; root_run_id?: string | number };
  tool_started: Record<string, unknown> & { idea_id?: string; run_id?: string | number; root_run_id?: string | number };
  tool_finished: Record<string, unknown> & { idea_id?: string; run_id?: string | number; root_run_id?: string | number };
  text_delta: Record<string, unknown> & { idea_id?: string; run_id?: string | number; root_run_id?: string | number };
  run_completed: Record<string, unknown> & { idea_id?: string; run_id?: string | number; root_run_id?: string | number };
};

export type CortexRealtimeEvent<
  Type extends CortexRealtimeEventType = CortexRealtimeEventType,
> = {
  type: Type;
  payload: CortexRealtimePayloadByType[Type];
};

export type CortexRealtimeDispatch = <Type extends CortexRealtimeEventType>(
  event: CortexRealtimeEvent<Type>,
) => void;
