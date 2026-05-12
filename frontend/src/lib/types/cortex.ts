export interface Idea {
  id: string;
  title: string;
  display_title?: string;
  description: string | null;
  status: string;
  origin: string;
  origin_ref?: string | null;
  salience_score: number;
  position_x: number | null;
  position_y: number | null;
  orbit_anchor_type?: string | null;
  orbit_anchor_id?: string | null;
  created_at: string;
  updated_at: string;
  user_id: string;
  author_name?: string;
  author_color?: string;
  thread_count?: number;
  active_agents?: number;
  attachments?: any[];
  archived_at?: string | null;
  _agents?: number;
  project_context?: Record<string, any> | null;
  agent_details?: Record<string, any> | null;
  metadata?: Record<string, any> | null;
}

export interface StreamItem {
  type: 'message' | 'run' | 'visual_block';
  timestamp: string;
  id: string;
  // message fields
  role?: string;
  content?: string;
  attachments?: any[];
  metadata?: Record<string, any>;
  user_id?: string;
  user_name?: string;
  user_color?: string;
  author_color?: string;
  message_type?: string;
  // run fields
  status?: string;
  skill_name?: string;
  model_used?: string;
  thinking_used?: string;
  tokens_total?: number;
  duration_sec?: number;
  outcome?: string;
  error?: string;
  requires_approval?: boolean;
  interactive_mode?: string;
  last_activity?: string;
  tool_calls?: { tool: string; args?: string; at?: string; status?: string; error?: string; result?: string }[];
  activity_trace?: { at?: string; activity: string; kind?: string; tool_name?: string; status?: string }[];
  work_log?: { time?: string; text: string; kind?: string }[];
  work_summary?: Record<string, any>;
  run_steps?: any[];
  worker_lanes?: any[];
  live_lines?: (string | { time?: string; text: string })[];
  started_at?: string;
  completed_at?: string;
  estimated_cost?: number;
  execution_profile?: CortexExecutionProfile | string;
  requested_run_profile?: CortexExecutionProfile | string | null;
  // visual_block fields
  content_type?: string;
  title?: string;
  display_mode?: string;
  run_id?: number;
  idea_id?: string;
  thread_id?: string;
  position_after?: number;
}

export interface Connection {
  id: string;
  source_id: string;
  target_id: string;
  type: string;
  weight: number;
}

export interface BrowserSessionState {
  id: string;
  idea_id: string;
  run_id?: string | number | null;
  status: string;
  current_url?: string | null;
  page_title?: string | null;
  viewport_width: number;
  viewport_height: number;
  storage_mode?: 'ephemeral' | 'idea';
  allow_downloads?: boolean;
  allow_file_uploads?: boolean;
  last_error?: string | null;
  watchers?: number;
  tabs?: { index: number; url?: string | null; title?: string | null; active: boolean }[];
  current_tab_index?: number;
  actions?: { at: string; action: string; detail?: string | null }[];
  downloads?: { at: string; filename: string; url: string; download_url?: string | null; size?: number | null }[];
  artifacts?: { at: string; kind: string; filename: string; url: string; download_url?: string | null; size?: number | null }[];
  console_messages?: { at: string; level: string; text: string; location?: string | null }[];
  request_failures?: {
    at: string;
    method: string;
    url: string;
    error_text?: string | null;
    resource_type?: string | null;
  }[];
}

export interface BrowserFrame {
  image_url: string;
  sha1: string;
  width: number;
  height: number;
  captured_at: string;
  focus?: {
    x?: number | null;
    y?: number | null;
    width?: number | null;
    height?: number | null;
    editable?: boolean;
    caret_x?: number | null;
    caret_y?: number | null;
    caret_height?: number | null;
  } | null;
}

export interface BrowserDiscoveryResult {
  session_id: string;
  url?: string | null;
  title?: string | null;
  selector: string;
  count: number;
  elements: Array<{
    index: number;
    tag: string;
    type?: string | null;
    role?: string | null;
    text?: string | null;
    aria_label?: string | null;
    name?: string | null;
    href?: string | null;
    suggested_selector?: string | null;
    bounds?: { x: number; y: number; width: number; height: number };
  }>;
}

export interface BrowserExtractResult {
  session_id: string;
  url?: string | null;
  title?: string | null;
  mode: string;
  selector?: string | null;
  content: string;
  truncated?: boolean;
}

export interface VaultSecretPrompt {
  id: string;
  idea_id?: string | null;
  key_name: string;
  description?: string | null;
  category?: string | null;
  reason?: string | null;
  requested_by?: string | null;
  created_at?: string | null;
}

export type CortexExecutionProfile = 'fast' | 'deep';
export type CortexIntelligenceTier = 'low' | 'medium' | 'high';
export type CortexEffortLevel = 'low' | 'medium' | 'high' | 'xhigh';

export interface AgentRunOptions {
  executionProfile?: CortexExecutionProfile;
  intelligenceTier?: CortexIntelligenceTier;
  effortLevel?: CortexEffortLevel;
  metadata?: Record<string, any>;
  skipRun?: boolean;
}
