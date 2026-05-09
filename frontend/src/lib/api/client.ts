const BASE = '';
const DEFAULT_API_TIMEOUT_MS = 20_000;
const PROJECT_CONTEXT_UPLOAD_TIMEOUT_MS = 30_000;

type ApiRequestInit = RequestInit & {
  timeoutMs?: number;
};

type PreloadedJson<T> = {
  ok: boolean;
  value?: T;
  error?: unknown;
};

function takePreloadedJson<T>(path: string, init?: ApiRequestInit): Promise<T> | null {
  if (typeof window === 'undefined') return null;
  const method = String(init?.method || 'GET').toUpperCase();
  if (method !== 'GET' || init?.body) return null;
  const headers = init?.headers as Record<string, string> | undefined;
  if (headers?.['X-Vault-Token']) return null;
  const registry = window.__illoPreload;
  const promise = registry?.[path];
  if (!promise) return null;
  delete registry[path];
  return (promise as Promise<PreloadedJson<T>>).then((result) => {
    if (result.ok) return result.value as T;
    throw result.error;
  });
}

async function fetchJson<T>(path: string, init?: ApiRequestInit): Promise<T> {
  const preloaded = takePreloadedJson<T>(path, init);
  if (preloaded) return preloaded;

  const {
    headers: extraHeaders,
    timeoutMs = DEFAULT_API_TIMEOUT_MS,
    signal,
    ...rest
  } = init ?? {};
  const controller = !signal && timeoutMs > 0 ? new AbortController() : null;
  const timeoutId = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const res = await fetch(`${BASE}${path}`, {
      ...rest,
      signal: signal ?? controller?.signal,
      headers: { 'Content-Type': 'application/json', ...(extraHeaders as Record<string, string>) },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }));
      throw { status: res.status, detail: err.error || err.detail || res.statusText };
    }
    return res.json();
  } catch (err: any) {
    if (err?.name === 'AbortError') {
      throw { status: 0, detail: 'Request timed out. Check the local server and try again.' };
    }
    throw err;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
}

function withQuery(
  path: string,
  params: Record<string, string | number | boolean | null | undefined>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === '') continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

function vaultHeaders(token?: string | null): Record<string, string> {
  return token ? { 'X-Vault-Token': token } : {};
}

export interface ChatParticipant {
  id: string;
  name: string;
  color: string | null;
  email: string | null;
}

export interface ChatMessage {
  id: number;
  conversation_id: string;
  sender_user_id: string | null;
  sender_kind: string;
  sender_name: string;
  sender_color: string | null;
  body: string;
  body_format: string;
  client_generated_id: string | null;
  thread_root_message_id: number | null;
  reply_to_message_id: number | null;
  attachments: any[];
  metadata: Record<string, any> | null;
  conversation_seq: number;
  reply_count: number;
  last_reply_at: string | null;
  last_reply_message_id: number | null;
  thread_preview_participants: ChatParticipant[];
  created_at: string;
  edited_at: string | null;
  deleted_at: string | null;
}

export interface ChatConversationSummary {
  id: string;
  type: string;
  stable_key: string;
  title: string | null;
  description: string | null;
  visibility: string;
  last_message_seq: number;
  unread_count: number;
  participant_count: number;
  counterpart: ChatParticipant | null;
  last_message: ChatMessage | null;
  created_at: string;
  updated_at: string;
}

export interface ChatUnreadSummary {
  room: number;
  dms: number;
  total: number;
}

export interface ChatNotification {
  id: number;
  type: 'mention' | 'dm_message' | 'room_message' | string;
  conversation_id: string | null;
  message_id: number | null;
  actor_user_id: string | null;
  actor_name: string | null;
  actor_color: string | null;
  metadata: Record<string, any> | null;
  created_at: string;
  read_at: string | null;
}

export interface AppNotification {
  id: number;
  source: string;
  kind: string;
  title: string;
  body: string | null;
  actor_user_id: string | null;
  actor_name: string | null;
  actor_color: string | null;
  idea_id: string | null;
  conversation_id: string | null;
  thread_root_message_id: number | null;
  occurrence_count: number;
  payload: Record<string, any> | null;
  created_at: string;
  updated_at: string;
  read_at: string | null;
}

export interface AppNotificationSummary {
  chat_unread_total: number;
  workspace_attention_total: number;
  unread_notification_total: number;
  unread_chat_notification_total: number;
  unread_workspace_notification_total: number;
}

export interface NotificationPreferences {
  sound_enabled: boolean;
  message_notifications_enabled: boolean;
}

export interface NotificationPreferencesUpdate {
  sound_enabled?: boolean;
  message_notifications_enabled?: boolean;
}

export interface WsTokenResponse {
  token: string;
  expires_at: string;
  ttl_seconds: number;
  session_id: string;
  tab_id: string | null;
}

export interface ChatBootstrap {
  room: ChatConversationSummary;
  dms: ChatConversationSummary[];
  notifications: ChatNotification[];
  unread_summary: ChatUnreadSummary;
  default_mode: string;
  default_conversation_id: string;
}

export interface ChatConversationPage {
  conversation: ChatConversationSummary;
  messages: ChatMessage[];
  has_more: boolean;
  next_before_seq: number | null;
}

export interface ChatThreadPage {
  conversation: ChatConversationSummary;
  root_message: ChatMessage;
  replies: ChatMessage[];
  has_more: boolean;
  next_before_seq: number | null;
}

export interface ChatSearchResult {
  message: ChatMessage;
  root_message: ChatMessage;
}

export interface ChatMessageCreateInput {
  body: string;
  body_format?: 'markdown' | 'plain';
  client_generated_id?: string | null;
  attachments?: any[];
  reply_to_message_id?: number | null;
  metadata?: Record<string, any> | null;
}

export interface ChatReadUpdateInput {
  last_read_message_id?: number | null;
  last_read_conversation_seq?: number | null;
}

export interface CycleRead {
  id: number;
  user_id: string;
  org_id: string | null;
  name: string;
  prompt: string;
  schedule_expr: string;
  schedule_human: string;
  timezone: string;
  enabled: boolean;
  model_override: string | null;
  thinking_override: string | null;
  execution_mode: 'reuse_same_idea';
  target_idea_id: string | null;
  reopen_archived: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface CycleRunRead {
  id: number;
  cycle_id: number;
  scheduled_for: string;
  started_at: string | null;
  completed_at: string | null;
  status: string;
  error: string | null;
  skip_reason: string | null;
  idea_id: string | null;
  run_id: number | null;
  prompt_snapshot: string;
  created_at: string;
}

export interface DomainFieldRead {
  id: number;
  domain_id: number;
  object_type_id: number;
  key: string;
  name: string;
  field_type: string;
  required: boolean;
  options: any[];
  default_value: any | null;
  validation: Record<string, any>;
  searchable: boolean;
  sortable: boolean;
  created_at: string;
  updated_at: string;
}

export interface DomainObjectRead {
  id: number;
  domain_id: number;
  key: string;
  name: string;
  description: string | null;
  title_field: string | null;
  sort_order: number;
  fields: DomainFieldRead[];
  created_at: string;
  updated_at: string;
}

export interface DomainRelationTypeRead {
  id: number;
  domain_id: number;
  key: string;
  name: string;
  description: string | null;
  source_object: string | null;
  target_object: string | null;
  source_object_type_id: number;
  target_object_type_id: number;
  cardinality: string;
  created_at: string;
  updated_at: string;
}

export interface DomainSummaryRead {
  id: number;
  org_id: string;
  slug: string;
  name: string;
  description: string | null;
  object_count: number;
  has_records: boolean;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DomainSchemaRead extends DomainSummaryRead {
  objects: DomainObjectRead[];
  relation_types: DomainRelationTypeRead[];
}

export interface DomainRecordRead {
  id: number;
  org_id: string;
  domain_id: number;
  object_type_id: number;
  object_key: string | null;
  title: string;
  data: Record<string, any>;
  version: number;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DomainFieldCreateInput {
  key: string;
  name?: string | null;
  field_type?: string;
  required?: boolean;
  options?: string[];
  default_value?: any;
  searchable?: boolean;
  sortable?: boolean;
}

export interface DomainObjectCreateInput {
  key: string;
  name?: string | null;
  description?: string | null;
  title_field?: string | null;
  fields?: DomainFieldCreateInput[];
}

export interface DomainCreateInput {
  name: string;
  slug?: string | null;
  description?: string | null;
  objects?: DomainObjectCreateInput[];
}

export interface WorkspaceAppVersionRead {
  id: number;
  app_id: string;
  version: number;
  renderer_key: string;
  source_kind: string;
  source_code: string;
  manifest: Record<string, any>;
  created_by_user_id: string | null;
  created_at: string;
}

export interface WorkspaceAppRead {
  id: string;
  org_id: string;
  key: string;
  name: string;
  description: string | null;
  renderer_key: string;
  visual_spec: Record<string, any>;
  metadata: Record<string, any>;
  created_by_user_id: string | null;
  anchor_user_id: string | null;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  active_version: WorkspaceAppVersionRead | null;
  contract_validation: Record<string, any>;
}

export interface WorkspaceAppStateRead {
  id: number;
  org_id: string;
  app_id: string;
  scope: string;
  key: string;
  data: Record<string, any>;
  updated_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkspacePinRead {
  id: string;
  org_id: string;
  label: string;
  color: string;
  position_x: number;
  position_y: number;
  metadata: Record<string, any>;
  created_by_user_id: string | null;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkspacePinCreateInput {
  label?: string;
  color?: string | null;
  position_x: number;
  position_y: number;
  metadata?: Record<string, any>;
}

export interface WorkspacePinUpdateInput {
  label?: string;
  color?: string;
  position_x?: number;
  position_y?: number;
  metadata?: Record<string, any>;
}

export interface WorkspaceAppCreateInput {
  key?: string | null;
  name: string;
  description?: string | null;
  renderer_key?: string;
  source_kind?: string;
  source_code?: string;
  manifest?: Record<string, any>;
  visual_spec?: Record<string, any>;
  metadata?: Record<string, any>;
  anchor_user_id?: string | null;
  initial_state?: Record<string, any> | null;
  state_key?: string;
}

export interface WorkspaceAppUpdateInput {
  name?: string | null;
  description?: string | null;
  renderer_key?: string | null;
  source_kind?: string | null;
  source_code?: string | null;
  manifest?: Record<string, any> | null;
  visual_spec?: Record<string, any> | null;
  metadata?: Record<string, any> | null;
  anchor_user_id?: string | null;
}

export interface CortexBootstrapPayload {
  ideas: any[] | null;
  connections: any[] | null;
  team_members: any[] | null;
  workspace_apps: WorkspaceAppRead[] | null;
  workspace_pins: WorkspacePinRead[] | null;
  selected_idea?: any | null;
  direct_thread?: { idea_id: string; stream: any[] } | null;
  auth_status: any | null;
  meta?: Record<string, any>;
}

export interface CortexBootstrapOptions {
  include?: string | string[];
  ideaId?: string | null;
  includeDebug?: boolean;
  provider?: string | null;
}

export const api = {
  // Auth
  getMe: () => fetchJson<any>('/api/me'),
  login: (email: string, password: string) =>
    fetchJson<any>('/api/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  logout: () => fetchJson<any>('/api/logout', { method: 'POST' }),
  register: (data: {
    name: string;
    email: string;
    password: string;
    org_name?: string;
    workspace_mode?: 'join' | 'create';
    workspace_slug?: string;
  }) =>
    fetchJson<any>('/api/register', { method: 'POST', body: JSON.stringify(data) }),
  setupCheck: (workspaceSlug?: string | null) =>
    fetchJson<{
      setup_required: boolean;
      default_org?: { id: string; name: string; slug: string } | null;
      requested_org?: { id: string; name: string; slug: string } | null;
    }>(withQuery('/api/auth/setup-check', { workspace: workspaceSlug })),
  issueWsToken: (tabId?: string) =>
    fetchJson<WsTokenResponse>('/api/auth/ws-token', {
      method: 'POST',
      body: JSON.stringify({ tab_id: tabId ?? null }),
    }),

  // Chat
  chatBootstrap: () => fetchJson<ChatBootstrap>('/api/chat/bootstrap'),
  listChatConversations: () => fetchJson<ChatConversationSummary[]>('/api/chat/conversations'),
  createChatDm: (userId: string) =>
    fetchJson<ChatConversationSummary>('/api/chat/dms', {
      method: 'POST',
      body: JSON.stringify({ user_id: userId }),
    }),
  getChatConversationMessages: (
    conversationId: string,
    options: { beforeSeq?: number | null; limit?: number } = {},
  ) =>
    fetchJson<ChatConversationPage>(
      withQuery(`/api/chat/conversations/${conversationId}/messages`, {
        before_seq: options.beforeSeq,
        limit: options.limit,
      }),
    ),
  postChatConversationMessage: (conversationId: string, data: ChatMessageCreateInput) =>
    fetchJson<ChatMessage>(`/api/chat/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  getChatThread: (
    messageId: number,
    options: { beforeSeq?: number | null; limit?: number } = {},
  ) =>
    fetchJson<ChatThreadPage>(
      withQuery(`/api/chat/messages/${messageId}/thread`, {
        before_seq: options.beforeSeq,
        limit: options.limit,
      }),
    ),
  postChatThreadReply: (messageId: number, data: ChatMessageCreateInput) =>
    fetchJson<ChatMessage>(`/api/chat/messages/${messageId}/thread`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  searchChatMessages: (
    query: string,
    options: { limit?: number } = {},
  ) =>
    fetchJson<ChatSearchResult[]>(
      withQuery('/api/chat/search', {
        query,
        limit: options.limit,
      }),
    ),
  chatMarkRead: (conversationId: string, data: ChatReadUpdateInput) =>
    fetchJson<ChatUnreadSummary>(`/api/chat/conversations/${conversationId}/read`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  listChatNotifications: (limit = 50) =>
    fetchJson<ChatNotification[]>(withQuery('/api/chat/notifications', { limit })),
  markChatNotificationRead: (notificationId: number) =>
    fetchJson<{ ok: boolean }>(`/api/chat/notifications/${notificationId}/read`, { method: 'POST' }),
  markAllChatNotificationsRead: () =>
    fetchJson<{ updated: number }>('/api/chat/notifications/read-all', { method: 'POST' }),

  // Unified notifications
  notificationSummary: () =>
    fetchJson<AppNotificationSummary>('/api/notifications/summary'),
  notificationPreferences: () =>
    fetchJson<NotificationPreferences>('/api/notifications/preferences'),
  updateNotificationPreferences: (data: NotificationPreferencesUpdate) =>
    fetchJson<NotificationPreferences>('/api/notifications/preferences', {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  listNotifications: (
    options: { status?: 'unread' | 'all'; limit?: number } = {},
  ) =>
    fetchJson<AppNotification[]>(
      withQuery('/api/notifications', {
        status: options.status ?? 'unread',
        limit: options.limit ?? 50,
      }),
    ),
  markNotificationRead: (notificationId: number) =>
    fetchJson<AppNotificationSummary>(`/api/notifications/${notificationId}/read`, {
      method: 'POST',
    }),
  markAllNotificationsRead: () =>
    fetchJson<AppNotificationSummary>('/api/notifications/read-all', {
      method: 'POST',
    }),

  // Cortex
  cortexBootstrap: (options: CortexBootstrapOptions = {}) => {
    const include = Array.isArray(options.include) ? options.include.join(',') : options.include;
    return fetchJson<CortexBootstrapPayload>(withQuery('/api/cortex/bootstrap', {
      include,
      idea_id: options.ideaId,
      include_debug: options.includeDebug,
      provider: options.provider,
    }));
  },
  listIdeas: (status?: string) =>
    fetchJson<any[]>(status ? `/api/cortex/ideas?status=${encodeURIComponent(status)}` : '/api/cortex/ideas'),
  listArchivedIdeas: (limit = 12) =>
    fetchJson<any[]>(withQuery('/api/cortex/ideas/archived', { limit })),
  getIdea: (id: string) => fetchJson<any>(`/api/cortex/ideas/${id}`),
  createIdea: (data: any) =>
    fetchJson<any>('/api/cortex/ideas', { method: 'POST', body: JSON.stringify(data) }),
  updateIdeaStatus: (id: string, status: string) =>
    fetchJson<any>(`/api/cortex/ideas/${id}/status`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  listConnections: () => fetchJson<any[]>('/api/cortex/connections'),
  ideaConnections: (ideaId: string) => fetchJson<any[]>(`/api/cortex/ideas/${ideaId}/connections`),
  listThreads: (ideaId: string) => fetchJson<any[]>(`/api/cortex/ideas/${ideaId}/threads`),
  createThread: (ideaId: string, content: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/threads`, { method: 'POST', body: JSON.stringify({ content }) }),

  // Cortex (continued)
  updateIdea: (id: string, data: any) =>
    fetchJson<any>(`/api/cortex/ideas/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  listProjectContextProfiles: () =>
    fetchJson<any[]>('/api/cortex/project-context/profiles'),
  createProjectContextProfile: (data: { slug: string; name: string; description?: string | null; project_context: any; default_environment_binding_id?: number | null; metadata?: Record<string, any> }) =>
    fetchJson<any>('/api/cortex/project-context/profiles', { method: 'POST', body: JSON.stringify(data) }),
  connectProjectContextGitHub: (data: { vault_key: string }, vaultToken?: string | null) =>
    fetchJson<any>('/api/cortex/project-context/github/connect', {
      method: 'POST',
      body: JSON.stringify(data),
      headers: vaultHeaders(vaultToken),
    }),
  searchProjectContextGitHub: (data: { query: string; vault_key?: string | null }, vaultToken?: string | null) =>
    fetchJson<any>('/api/cortex/project-context/github/search', {
      method: 'POST',
      body: JSON.stringify(data),
      headers: vaultHeaders(vaultToken),
    }),
  bindProjectContextGitHubToken: (
    data: { vault_key: string; repo: string; env_name?: string | null },
    vaultToken?: string | null,
  ) =>
    fetchJson<any>('/api/cortex/project-context/github/bind-token', {
      method: 'POST',
      body: JSON.stringify(data),
      headers: vaultHeaders(vaultToken),
    }),
  uploadProjectContextFiles: (files: File[], relativePaths?: string[]) => {
    const form = new FormData();
    files.forEach((file, index) => {
      form.append('files', file);
      form.append('relative_paths', relativePaths?.[index] || file.name);
    });
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), PROJECT_CONTEXT_UPLOAD_TIMEOUT_MS);
    return fetch('/api/cortex/project-context/local-files', {
      method: 'POST',
      body: form,
      signal: controller.signal,
    })
      .then(async (r) => {
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: 'Upload failed' }));
          throw { status: r.status, detail: err.error || err.detail || 'Upload failed' };
        }
        return r.json();
      })
      .catch((err) => {
        if (err?.name === 'AbortError') {
          throw { detail: 'Project Context upload timed out before the local server responded.' };
        }
        throw err;
      })
      .finally(() => clearTimeout(timeoutId));
  },
  listIdeaProjectContext: (ideaId: string) =>
    fetchJson<any[]>(`/api/cortex/ideas/${ideaId}/project-context`),
  attachIdeaProjectContext: (ideaId: string, data: { project_profile_id?: string; project_context?: any; environment_binding_id?: number | null; metadata?: Record<string, any> }) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/project-context`, { method: 'POST', body: JSON.stringify(data) }),
  deleteIdea: (id: string) =>
    fetchJson<any>(`/api/cortex/ideas/${id}`, { method: 'DELETE' }),
  restoreIdea: (id: string) =>
    fetchJson<any>(`/api/cortex/ideas/${id}/restore`, { method: 'POST' }),
  unifiedStream: (ideaId: string, includeDebug = false) =>
    fetchJson<any[]>(
      includeDebug
        ? `/api/cortex/ideas/${ideaId}/unified-stream?include_debug=true`
        : `/api/cortex/ideas/${ideaId}/unified-stream`
    ),
  addThreadMessage: (ideaId: string, data: { content: string; role?: string; attachments?: any[]; metadata?: Record<string, any> }) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/thread`, { method: 'POST', body: JSON.stringify(data) }),
  updatePosition: (ideaId: string, x: number, y: number) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/position`, { method: 'PATCH', body: JSON.stringify({ position_x: x, position_y: y }) }),
  batchPositions: (positions: { id: string; x: number; y: number }[]) =>
    fetchJson<any>('/api/cortex/ideas/positions', { method: 'PUT', body: JSON.stringify({ positions }) }),
  runStatus: () => fetchJson<any>('/api/cortex/run/status'),
  runHistory: (ideaId: string, includeDebug = false) =>
    fetchJson<any[]>(
      includeDebug
        ? `/api/cortex/run/history/${ideaId}?include_debug=true`
        : `/api/cortex/run/history/${ideaId}`
    ),
  approveRun: (id: number) =>
    fetchJson<any>(`/api/cortex/run/${id}/approve`, { method: 'POST' }),
  denyRun: (id: number) =>
    fetchJson<any>(`/api/cortex/run/${id}/deny`, { method: 'POST' }),
  cancelRun: (id: number) =>
    fetchJson<any>(`/api/cortex/run/${id}/cancel`, { method: 'POST' }),
  steerRun: (id: number, data: { content: string }) =>
    fetchJson<any>(`/api/cortex/run/${id}/steer`, { method: 'POST', body: JSON.stringify(data) }),
  cancelAllRuns: (ideaId: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/cancel-all`, { method: 'POST' }),
  runGraph: (id: number) => fetchJson<any>(`/api/cortex/run/${id}/graph`),
  runTools: (id: number) => fetchJson<any[]>(`/api/cortex/runs/${id}/tools`),
  skillFeedback: (id: number, data: { note: string; quality: string }) =>
    fetchJson<any>(`/api/cortex/run/${id}/skill-feedback`, { method: 'POST', body: JSON.stringify(data) }),
  markRead: (ideaId: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/mark-read`, { method: 'POST' }),
  markMentionsSeen: (ideaId: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/mentions/seen`, { method: 'POST' }),
  unreadMentions: () => fetchJson<any[]>('/api/cortex/mentions/unread'),
  postPresence: (data: { idea_id: string; action: string }) =>
    fetchJson<any>('/api/cortex/presence', { method: 'POST', body: JSON.stringify(data) }),
  cortexAnalytics: () => fetchJson<any>('/api/cortex/analytics'),
  activityTimeline: (ideaId: string) => fetchJson<any[]>(`/api/cortex/ideas/${ideaId}/activity-timeline`),
  suggestedIdeas: () => fetchJson<any[]>('/api/cortex/suggested'),
  slashCommands: () => fetchJson<any[]>('/api/cortex/slash-commands'),
  createConnection: (data: { source_id: string; target_id: string; type?: string }) =>
    fetchJson<any>('/api/cortex/connections', { method: 'POST', body: JSON.stringify(data) }),
  deleteConnection: (connId: string) =>
    fetchJson<any>(`/api/cortex/connections/${connId}`, { method: 'DELETE' }),
  delegationStats: () => fetchJson<any>('/api/cortex/delegation-stats'),
  detectBranches: (ideaId: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/detect-branches`, { method: 'POST' }),
  splitIdea: (ideaId: string, data: any) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/split`, { method: 'POST', body: JSON.stringify(data) }),
  timelineData: (limit?: number) =>
    fetchJson<any>(withQuery('/api/cortex/timeline-data', { limit })),
  uploadFile: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return fetch('/api/cortex/upload', { method: 'POST', body: form }).then(r => {
      if (!r.ok) throw { status: r.status, detail: 'Upload failed' };
      return r.json();
    });
  },
  generateTitle: (text: string) =>
    fetchJson<any>('/api/cortex/generate-title', { method: 'POST', body: JSON.stringify({ text }) }),
  notifyCortex: (data: { event: string; idea_id?: string; [key: string]: any }) =>
    fetchJson<any>('/api/cortex/notify', { method: 'POST', body: JSON.stringify(data) }),
  getBrowserSession: (ideaId: string) =>
    fetchJson<any | null>(`/api/cortex/ideas/${ideaId}/browser/session`),
  createBrowserSession: (ideaId: string, data: {
    url?: string;
    viewport_width?: number;
    viewport_height?: number;
    storage_mode?: 'ephemeral' | 'idea';
    allow_downloads?: boolean;
    allow_file_uploads?: boolean;
  }) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/browser/session`, { method: 'POST', body: JSON.stringify(data) }),
  snapshotBrowserSession: (sessionId: string, data: { persist?: boolean; title?: string } = {}) =>
    fetchJson<any>(`/api/cortex/browser/session/${sessionId}/snapshot`, { method: 'POST', body: JSON.stringify(data) }),
  closeBrowserSession: (sessionId: string) =>
    fetchJson<any>(`/api/cortex/browser/session/${sessionId}`, { method: 'DELETE' }),

  // Cycles
  listCycles: () => fetchJson<CycleRead[]>('/api/cycles/'),
  getCycle: (cycleId: number) => fetchJson<CycleRead>(`/api/cycles/${cycleId}`),
  createCycle: (data: {
    name: string;
    prompt: string;
    schedule_expr?: string | null;
    run_at?: string | null;
    timezone: string;
    enabled?: boolean;
    model_override?: string | null;
    thinking_override?: string | null;
    execution_mode?: 'reuse_same_idea';
    target_idea_id?: string | null;
    reopen_archived?: boolean | null;
  }) =>
    fetchJson<CycleRead>('/api/cycles/', { method: 'POST', body: JSON.stringify(data) }),
  updateCycle: (
    cycleId: number,
    data: Partial<{
      name: string;
      prompt: string;
      schedule_expr: string;
      run_at: string | null;
      timezone: string;
      enabled: boolean;
      model_override: string | null;
      thinking_override: string | null;
      execution_mode: 'reuse_same_idea';
      target_idea_id: string | null;
      reopen_archived: boolean | null;
    }>,
  ) =>
    fetchJson<CycleRead>(`/api/cycles/${cycleId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteCycle: (cycleId: number) =>
    fetchJson<{ ok: boolean; id: number }>(`/api/cycles/${cycleId}`, { method: 'DELETE' }),
  listCycleRuns: (cycleId: number, limit = 25) =>
    fetchJson<CycleRunRead[]>(withQuery(`/api/cycles/${cycleId}/runs`, { limit })),
  runCycle: (cycleId: number) =>
    fetchJson<CycleRunRead>(`/api/cycles/${cycleId}/run`, { method: 'POST' }),

  // Domains
  listDomains: () => fetchJson<DomainSummaryRead[]>('/api/domains/'),
  createDomain: (data: DomainCreateInput) =>
    fetchJson<DomainSchemaRead>('/api/domains/', { method: 'POST', body: JSON.stringify(data) }),
  getDomain: (domainId: number) => fetchJson<DomainSchemaRead>(`/api/domains/${domainId}`),
  removeDomain: (domainId: number, mode: 'archive' | 'delete' = 'archive') =>
    fetchJson<any>(withQuery(`/api/domains/${domainId}`, { mode }), {
      method: 'DELETE',
    }),
  listDomainRecords: (
    domainId: number,
    options: { objectKey?: string | null; search?: string | null; includeArchived?: boolean; limit?: number } = {},
  ) =>
    fetchJson<DomainRecordRead[]>(
      withQuery(`/api/domains/${domainId}/records`, {
        object_key: options.objectKey,
        search: options.search,
        include_archived: options.includeArchived,
        limit: options.limit ?? 100,
      }),
    ),
  getDomainRecord: (domainId: number, recordId: number) =>
    fetchJson<DomainRecordRead>(`/api/domains/${domainId}/records/${recordId}`),
  createDomainRecord: (domainId: number, objectKey: string, data: { data: Record<string, any>; title?: string | null }) =>
    fetchJson<DomainRecordRead>(`/api/domains/${domainId}/objects/${encodeURIComponent(objectKey)}/records`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateDomainRecord: (
    domainId: number,
    recordId: number,
    data: { data_patch?: Record<string, any>; title?: string | null; expected_version?: number | null },
  ) =>
    fetchJson<DomainRecordRead>(`/api/domains/${domainId}/records/${recordId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  removeDomainRecord: (domainId: number, recordId: number, mode: 'archive' | 'delete' = 'archive') =>
    fetchJson<any>(withQuery(`/api/domains/${domainId}/records/${recordId}`, { mode }), {
      method: 'DELETE',
    }),

  // Generated workspace apps
  listWorkspaceApps: () => fetchJson<WorkspaceAppRead[]>('/api/workspace-apps/'),
  createWorkspaceApp: (data: WorkspaceAppCreateInput) =>
    fetchJson<WorkspaceAppRead>('/api/workspace-apps/', { method: 'POST', body: JSON.stringify(data) }),
  getWorkspaceApp: (appId: string) => fetchJson<WorkspaceAppRead>(`/api/workspace-apps/${appId}`),
  listArchivedWorkspaceApps: (limit = 12) =>
    fetchJson<WorkspaceAppRead[]>(withQuery('/api/workspace-apps/archived', { limit })),
  updateWorkspaceApp: (appId: string, data: WorkspaceAppUpdateInput) =>
    fetchJson<WorkspaceAppRead>(`/api/workspace-apps/${appId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  archiveWorkspaceApp: (appId: string) =>
    fetchJson<{ archived: { id: string; key: string } }>(`/api/workspace-apps/${appId}`, { method: 'DELETE' }),
  restoreWorkspaceApp: (appId: string) =>
    fetchJson<WorkspaceAppRead>(`/api/workspace-apps/${appId}/restore`, { method: 'POST' }),
  getWorkspaceAppState: (appId: string, stateKey = 'default') =>
    fetchJson<WorkspaceAppStateRead>(`/api/workspace-apps/${appId}/state/${encodeURIComponent(stateKey)}`),
  updateWorkspaceAppState: (appId: string, stateKey: string, data: Record<string, any>) =>
    fetchJson<WorkspaceAppStateRead>(`/api/workspace-apps/${appId}/state/${encodeURIComponent(stateKey)}`, {
      method: 'PUT',
      body: JSON.stringify({ data }),
    }),

  // Workspace pins
  listWorkspacePins: () => fetchJson<WorkspacePinRead[]>('/api/workspace-pins/'),
  createWorkspacePin: (data: WorkspacePinCreateInput) =>
    fetchJson<WorkspacePinRead>('/api/workspace-pins/', { method: 'POST', body: JSON.stringify(data) }),
  updateWorkspacePin: (pinId: string, data: WorkspacePinUpdateInput) =>
    fetchJson<WorkspacePinRead>(`/api/workspace-pins/${pinId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteWorkspacePin: (pinId: string) =>
    fetchJson<{ deleted: { id: string } }>(`/api/workspace-pins/${pinId}`, { method: 'DELETE' }),
  archiveWorkspacePin: (pinId: string) =>
    fetchJson<{ deleted: { id: string } }>(`/api/workspace-pins/${pinId}`, { method: 'DELETE' }),

  // Memory
  getGraph: (limit?: number) =>
    fetchJson<any>(limit ? `/api/memory/graph?limit=${limit}` : '/api/memory/graph'),
  getGraphSimilarity: (limit?: number) =>
    fetchJson<any>(limit ? `/api/memory/graph-similarity?limit=${limit}` : '/api/memory/graph-similarity'),
  searchMemories: (q: string) => fetchJson<any[]>(`/api/memory/search?q=${encodeURIComponent(q)}`),
  getMemory: (id: number) => fetchJson<any>(`/api/memory/${id}`),
  getStale: () => fetchJson<any[]>('/api/memory/stale'),
  confirmMemory: (id: number) => fetchJson<any>(`/api/memory/${id}/confirm`, { method: 'POST' }),
  flagMemory: (id: number) => fetchJson<any>(`/api/memory/${id}/flag`, { method: 'POST' }),
  promoteMemory: (id: number, visibility: string) =>
    fetchJson<any>(`/api/memory/${id}/promote`, { method: 'POST', body: JSON.stringify({ visibility }) }),
  createMemory: (data: any) => fetchJson<any>('/api/memory/', { method: 'POST', body: JSON.stringify(data) }),
  patchMemory: (id: number, data: any) =>
    fetchJson<any>(`/api/memory/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  orgMemories: () => fetchJson<any[]>('/api/memory/org-memories'),
  memoryNeighborhood: (id: number) => fetchJson<any[]>(`/api/memory/${id}/neighborhood`),

  // Skills
  listSkills: (includeExecutions = false) =>
    fetchJson<any[]>(
      withQuery('/api/skills/', { include_executions: includeExecutions })
    ),
  listEnhancedSkills: () => fetchJson<any[]>('/api/skills/enhanced'),
  createSkill: (data: {
    name: string;
    description?: string;
    procedure?: string;
    model_tier?: string;
    thinking_tier?: string;
    pitfalls?: any[];
    refinements?: any[];
    triggers?: any[];
    guardrails?: any[];
  }) =>
    fetchJson<any>('/api/skills/new', { method: 'POST', body: JSON.stringify(data) }),
  exportSkills: () => fetchJson<any[]>('/api/skills/export'),
  importSkills: (skills: any[]) =>
    fetchJson<any[]>('/api/skills/import', { method: 'POST', body: JSON.stringify(skills) }),
  needingAttention: () => fetchJson<any[]>('/api/skills/needing-attention'),
  updateSkill: (id: number, data: any) =>
    fetchJson<any>(`/api/skills/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  archiveSkill: (id: number) =>
    fetchJson<any>(`/api/skills/${id}/archive`, { method: 'POST' }),
  deleteSkill: (id: number) =>
    fetchJson<any>(`/api/skills/${id}`, { method: 'DELETE' }),
  editSkill: (id: number, data: any) =>
    fetchJson<any>(`/api/skills/${id}/edit`, { method: 'PUT', body: JSON.stringify(data) }),
  skillPackage: (id: number) => fetchJson<any>(`/api/skills/${id}/package`),
  skillAssets: (id: number) => fetchJson<any[]>(`/api/skills/${id}/assets`),
  skillAsset: (id: number, path: string, maxChars = 12000) =>
    fetchJson<any>(`/api/skills/${id}/assets/${path.split('/').map(encodeURIComponent).join('/')}?max_chars=${maxChars}`),
  upsertSkillAsset: (id: number, data: {
    path: string;
    content: string;
    asset_kind?: string;
    mime_type?: string;
    loading_budget_tokens?: number | null;
  }) =>
    fetchJson<any>(`/api/skills/${id}/assets`, { method: 'POST', body: JSON.stringify(data) }),
  replaceSkillAsset: (id: number, path: string, data: {
    content: string;
    asset_kind?: string;
    mime_type?: string;
    loading_budget_tokens?: number | null;
  }) =>
    fetchJson<any>(`/api/skills/${id}/assets/${path.split('/').map(encodeURIComponent).join('/')}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteSkillAsset: (id: number, path: string) =>
    fetchJson<any>(`/api/skills/${id}/assets/${path.split('/').map(encodeURIComponent).join('/')}`, { method: 'DELETE' }),
  convertSkillToBundle: (id: number) =>
    fetchJson<any>(`/api/skills/${id}/convert-to-bundle`, { method: 'POST' }),
  skillVersions: (name: string) => fetchJson<any[]>(`/api/skills/${encodeURIComponent(name)}/versions`),
  restoreVersion: (name: string, version: number) =>
    fetchJson<any>(`/api/skills/${encodeURIComponent(name)}/versions/${version}/restore`, { method: 'POST' }),
  skillExecutions: (name: string, limit = 20) =>
    fetchJson<any[]>(`/api/skills/${encodeURIComponent(name)}/executions?limit=${limit}`),
  skillSparkline: (name: string) => fetchJson<any[]>(`/api/skills/${encodeURIComponent(name)}/sparkline`),
  addGuardrail: (name: string, text: string, severity: string) =>
    fetchJson<any>(`/api/skills/${encodeURIComponent(name)}/guardrail`, { method: 'POST', body: JSON.stringify({ text, severity }) }),
  addProcedureStep: (name: string, text: string, position: string) =>
    fetchJson<any>(`/api/skills/${encodeURIComponent(name)}/procedure-step`, { method: 'POST', body: JSON.stringify({ text, position }) }),
  flagExecution: (name: string, executionId: number, correction: string) =>
    fetchJson<any>(`/api/skills/${encodeURIComponent(name)}/flag-execution`, { method: 'POST', body: JSON.stringify({ execution_id: executionId, correction }) }),
  addTrigger: (name: string, direction: string, pattern: string) =>
    fetchJson<any>(`/api/skills/${encodeURIComponent(name)}/trigger`, { method: 'POST', body: JSON.stringify({ direction, pattern }) }),
  removeTrigger: (name: string, index: number) =>
    fetchJson<any>(`/api/skills/${encodeURIComponent(name)}/trigger/${index}`, { method: 'DELETE' }),

  // Vault
  listSecrets: (category?: string, vaultToken?: string | null) =>
    fetchJson<any[]>(category ? `/api/vault/?category=${encodeURIComponent(category)}` : '/api/vault/', {
      headers: vaultHeaders(vaultToken),
    }),
  createSecret: (data: any, vaultToken?: string | null) =>
    fetchJson<any>('/api/vault/', { method: 'POST', body: JSON.stringify(data), headers: vaultHeaders(vaultToken) }),
  revealSecret: (keyName: string, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/${keyName}`, { headers: vaultHeaders(vaultToken) }),
  deleteSecret: (keyName: string, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/${keyName}`, { method: 'DELETE', headers: vaultHeaders(vaultToken) }),
  updateSecret: (keyName: string, data: any, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/${keyName}`, { method: 'PUT', body: JSON.stringify(data), headers: vaultHeaders(vaultToken) }),
  pinStatus: () => fetchJson<any>('/api/vault/pin-status'),
  vaultSetupPin: (data: { new_pin: string; current_pin?: string }) =>
    fetchJson<any>('/api/vault/setup-pin', { method: 'POST', body: JSON.stringify(data) }),
  vaultUnlock: (pin: string) =>
    fetchJson<any>('/api/vault/unlock', { method: 'POST', body: JSON.stringify({ pin }) }),
  vaultLock: (vaultToken?: string | null) =>
    fetchJson<any>('/api/vault/lock', { method: 'POST', headers: vaultHeaders(vaultToken) }),
  vaultOrgUsers: (vaultToken?: string | null) =>
    fetchJson<any[]>('/api/vault/org-users', { headers: vaultHeaders(vaultToken) }),
  vaultShare: (id: number, data: any, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/${id}/share`, { method: 'POST', body: JSON.stringify(data), headers: vaultHeaders(vaultToken) }),
  vaultRevokeShare: (shareId: number, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/shares/${shareId}`, { method: 'DELETE', headers: vaultHeaders(vaultToken) }),
  vaultLog: (vaultToken?: string | null) =>
    fetchJson<any[]>('/api/vault/log', { headers: vaultHeaders(vaultToken) }),
  missingSecrets: (vaultToken?: string | null) =>
    fetchJson<any[]>('/api/vault/missing', { headers: vaultHeaders(vaultToken) }),
  vaultAgentGrants: (vaultToken?: string | null, status?: string) =>
    fetchJson<any[]>(status ? `/api/vault/agent-grants?status=${encodeURIComponent(status)}` : '/api/vault/agent-grants', {
      headers: vaultHeaders(vaultToken),
    }),
  vaultApproveGrant: (grantId: number, data: { ttl_minutes?: number; max_reads?: number }, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/agent-grants/${grantId}/approve`, {
      method: 'POST',
      body: JSON.stringify(data),
      headers: vaultHeaders(vaultToken),
    }),
  vaultDenyGrant: (grantId: number, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/agent-grants/${grantId}/deny`, { method: 'POST', headers: vaultHeaders(vaultToken) }),
  vaultProjectBindings: (vaultToken?: string | null) =>
    fetchJson<any[]>('/api/vault/project-bindings', { headers: vaultHeaders(vaultToken) }),
  vaultBindProjectSecret: (secretId: number, data: any, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/${secretId}/project-bindings`, {
      method: 'POST',
      body: JSON.stringify(data),
      headers: vaultHeaders(vaultToken),
    }),
  vaultDeleteProjectBinding: (bindingId: number, vaultToken?: string | null) =>
    fetchJson<any>(`/api/vault/project-bindings/${bindingId}`, { method: 'DELETE', headers: vaultHeaders(vaultToken) }),

  // System
  systemInfo: () => fetchJson<any>('/api/system'),
  runtimeSettings: () => fetchJson<any>('/api/runtime-settings'),
  connectRuntimeOpenAIKey: (data: { api_key: string }) =>
    fetchJson<any>('/api/runtime-settings/connection/openai/api-key', { method: 'POST', body: JSON.stringify(data) }),
  connectRuntimeOpenAIEmbeddingKey: (data: { api_key: string }) =>
    fetchJson<any>('/api/runtime-settings/connection/openai/embedding-api-key', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  connectRuntimeGeminiKey: (data: { api_key: string }) =>
    fetchJson<any>('/api/runtime-settings/connection/gemini/api-key', { method: 'POST', body: JSON.stringify(data) }),
  startRuntimeOpenAIOAuth: (data?: { callback_mode?: 'auto' | 'server' | 'local_bridge' }) =>
    fetchJson<any>('/api/runtime-settings/connection/openai/oauth/start', {
      method: 'POST',
      body: data ? JSON.stringify(data) : undefined,
    }),
  exchangeRuntimeOpenAIOAuth: (data: { callback: string }) =>
    fetchJson<any>('/api/runtime-settings/connection/openai/oauth/exchange', { method: 'POST', body: JSON.stringify(data) }),
  updateRuntimeModels: (data: { low: string; medium: string; high: string }) =>
    fetchJson<any>('/api/runtime-settings/models', { method: 'PATCH', body: JSON.stringify(data) }),
  updateRuntimeMemory: (data: { embedder: string; embedding_model?: string | null; reranker?: string }) =>
    fetchJson<any>('/api/runtime-settings/memory', { method: 'PATCH', body: JSON.stringify(data) }),
  checkRuntimeMemory: () =>
    fetchJson<any>('/api/runtime-settings/memory/check', { method: 'POST' }),

  // Onboarding
  runtimeReadyIntroDraft: () =>
    fetchJson<{
      ok: boolean;
      idea_id?: string | null;
      should_play: boolean;
      prompt: string;
      title: string;
      display_title: string;
      origin: string;
      origin_ref: string;
      run_metadata?: Record<string, any> | null;
    }>(
      '/api/onboarding/runtime-ready-intro-draft',
      { method: 'POST' },
    ),
  startRuntimeReadyIntro: () =>
    fetchJson<{ ok: boolean; idea_id: string; created: boolean; run_id?: number | null }>(
      '/api/onboarding/runtime-ready-intro',
      { method: 'POST' },
    ),
  listMetrics: () => fetchJson<any[]>('/api/metrics'),
  listConsolidations: () => fetchJson<any[]>('/api/consolidations'),
  listRetrieval: () => fetchJson<any[]>('/api/retrieval'),

  // Team
  listTeamMembers: () => fetchJson<any[]>('/api/team/members'),
  updateProfile: (data: any) =>
    fetchJson<any>('/api/users/me', { method: 'PATCH', body: JSON.stringify(data) }),
  teamActivity: (hours = 48) => fetchJson<any[]>(`/api/team/activity?hours=${hours}`),

  // Costs
  listCosts: (limit?: number) =>
    fetchJson<any>(limit ? `/api/costs/?limit=${limit}` : '/api/costs/'),

  // Health
  health: () => fetchJson<any>('/api/health'),
  healthDeep: () => fetchJson<any>('/api/health/deep'),
  gpuServerHealth: () => fetchJson<any>('/api/cortex/system/gpu-server/health'),
  restartWorker: (name: string) =>
    fetchJson<any>(`/api/cortex/system/gpu-server/workers/${name}/restart`, { method: 'POST' }),

  // Brain
  brainHealth: () => fetchJson<any>('/api/brain-health'),
  recentLearnings: (hours = 48, minSalience = 7, limit = 5) =>
    fetchJson<any[]>(`/api/recent-learnings?hours=${hours}&min_salience=${minSalience}&limit=${limit}`),
  staleIdeas: (threshold = 30) =>
    fetchJson<any[]>(`/api/stale-ideas?threshold=${threshold}`),
  brainPrompts: () => fetchJson<any[]>('/api/brain-prompts'),
  teachPrompt: (id: number) =>
    fetchJson<any>(`/api/brain-prompts/${id}/teach`, { method: 'POST' }),
  dismissPrompt: (id: number) =>
    fetchJson<any>(`/api/brain-prompts/${id}/dismiss`, { method: 'POST' }),
  resolvePrompt: (id: number) =>
    fetchJson<any>(`/api/brain-prompts/${id}/resolve`, { method: 'POST' }),

  // Admin
  pendingUsers: () => fetchJson<any[]>('/api/admin/pending'),
  approveUser: (userId: string) =>
    fetchJson<any>(`/api/admin/users/${userId}/approve`, { method: 'POST' }),
  rejectUser: (userId: string) =>
    fetchJson<any>(`/api/admin/users/${userId}/reject`, { method: 'POST' }),

  // Cron Management
  listCronJobs: () => fetchJson<any[]>('/api/system/cron-jobs'),
  cronLogs: (job: string, lines = 100, files = 5) =>
    fetchJson<any[]>(withQuery('/api/system/cron-logs', { job, lines, files })),
  createCronJob: (data: any) =>
    fetchJson<any>('/api/system/cron-jobs', { method: 'POST', body: JSON.stringify(data) }),
  toggleCronJob: (name: string, enabled: boolean) =>
    fetchJson<any>(`/api/system/cron-jobs/${encodeURIComponent(name)}`, {
      method: 'PATCH', body: JSON.stringify({ enabled }),
    }),
  deleteCronJob: (name: string) =>
    fetchJson<any>(`/api/system/cron-jobs/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  // Ops Console
  opsActive: () => fetchJson<any[]>('/api/cortex/ops/active'),
  opsRecent: (limit = 20, includeDebug = false) =>
    fetchJson<any[]>(
      `/api/cortex/ops/recent?limit=${limit}${includeDebug ? '&include_debug=true' : ''}`
    ),

  // Global search
  globalSearch: (q: string) =>
    fetchJson<any>(`/api/search?q=${encodeURIComponent(q)}`),

  // Conversation Audit
  ideaAudit: (ideaId: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/audit`),
  ideaAuditAnalyze: (ideaId: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/audit/analyze`, { method: 'POST' }),
  ideaAuditAnalysisResult: (ideaId: string) =>
    fetchJson<any>(`/api/cortex/ideas/${ideaId}/audit/analysis-result`),
  auditApply: (type: string, payload: any) =>
    fetchJson<any>('/api/cortex/audit/apply', {
      method: 'POST',
      body: JSON.stringify({ type, payload }),
    }),
  auditEval: (proposal: any) =>
    fetchJson<any>('/api/cortex/audit/eval', {
      method: 'POST',
      body: JSON.stringify({ proposal }),
    }),
};
