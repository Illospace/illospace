import { expect, type Page, type Request, type Route } from '@playwright/test';

type JsonValue = Record<string, unknown> | unknown[] | string | number | boolean | null;
type ApiResponse = {
  status?: number;
  body: JsonValue;
};
type ApiMockValue =
  | JsonValue
  | ApiResponse
  | ((request: Request) => JsonValue | ApiResponse | Promise<JsonValue | ApiResponse>);

export type ApiMockRule = {
  method?: string;
  path: string | RegExp;
  response: ApiMockValue;
};

export const e2eUser = {
  id: 'user-e2e',
  name: 'Ada Lovelace',
  email: 'ada@example.test',
  role: 'owner',
  org_id: 'org-e2e',
  org_name: 'E2E Workspace',
  approved: true,
  color: '#57CFA0',
  permissions: [
    'run:manage',
    'run:approve',
    'run:cancel',
    'memory:manage',
    'system:manage',
    'vault:share',
  ],
};

export const sampleIdea = {
  id: 'idea-survivability',
  title: 'Map survivability gaps',
  display_title: 'Map survivability gaps',
  description: 'Find brittle product behavior before users do.',
  status: 'new',
  origin: 'user',
  origin_ref: null,
  salience_score: 0.82,
  position_x: 160,
  position_y: 120,
  created_at: '2026-05-01T10:00:00Z',
  updated_at: '2026-05-01T10:00:00Z',
  user_id: e2eUser.id,
  author_name: e2eUser.name,
  author_color: e2eUser.color,
  thread_count: 1,
  active_agents: 0,
  attachments: [],
  metadata: {},
};

export const sampleChatRoom = {
  id: 'room-e2e',
  type: 'room',
  stable_key: 'org-room',
  title: 'Team',
  description: null,
  visibility: 'org',
  last_message_seq: 1,
  unread_count: 1,
  participant_count: 2,
  counterpart: null,
  last_message: {
    id: 9001,
    conversation_id: 'room-e2e',
    sender_user_id: 'teammate-e2e',
    sender_kind: 'user',
    sender_name: 'Grace',
    sender_color: '#5b8def',
    body: 'Can someone check the run cancellation path?',
    body_format: 'markdown',
    client_generated_id: null,
    thread_root_message_id: null,
    reply_to_message_id: null,
    attachments: [],
    metadata: {},
    conversation_seq: 1,
    reply_count: 0,
    last_reply_at: null,
    last_reply_message_id: null,
    thread_preview_participants: [],
    created_at: '2026-05-01T10:05:00Z',
    edited_at: null,
    deleted_at: null,
  },
  created_at: '2026-05-01T10:00:00Z',
  updated_at: '2026-05-01T10:05:00Z',
};

export const runtimeSettingsReady = {
  onboarding_required: false,
  personal_openai_ready: true,
  default_provider: 'openai',
  connection_status: { openai: 'connected' },
  connection: {
    status: 'connected',
    setup_required: false,
    method: 'api_key',
    source: 'workspace',
    label: 'OpenAI API key',
    detail: 'Connected for E2E',
  },
  models: {
    low: 'gpt-5.4-mini',
    medium: 'gpt-5.4',
    high: 'gpt-5.5',
    options: [
      { key: 'gpt-5.4-mini', label: 'GPT-5.4 Mini' },
      { key: 'gpt-5.4', label: 'GPT-5.4' },
      { key: 'gpt-5.5', label: 'GPT-5.5' },
    ],
  },
  memory: {
    embedder: 'openai',
    embedding_model: 'text-embedding-3-small',
    embedding_dimensions: 1536,
    embedding_status: 'ready',
    embedding_detail: 'E2E embedding configuration is ready.',
    indexed_vectors: 42,
    api_key_statuses: { openai: true, gemini: false },
    reranker: 'weighted',
    embedder_options: [
      { key: 'openai', label: 'OpenAI' },
      { key: 'local_gpu', label: 'Local GPU' },
    ],
    embedding_model_options: [
      { key: 'text-embedding-3-small', label: 'text-embedding-3-small' },
    ],
    reranker_options: [
      { key: 'weighted', label: 'Weighted' },
    ],
  },
  permissions: { can_manage_settings: true },
};

export async function mockProductApi(
  page: Page,
  rules: ApiMockRule[] = [],
  options: { user?: Record<string, unknown> | null } = {},
) {
  await stubWebSocket(page);
  const unknownRequests: string[] = [];
  const allRules = [...rules, ...defaultRules(options.user ?? e2eUser)];

  await page.route('**/api/**', async (route, request) => {
    const url = new URL(request.url());
    const rule = allRules.find((candidate) => matchesRule(candidate, request, url.pathname));
    if (!rule) {
      unknownRequests.push(`${request.method()} ${url.pathname}`);
      await respondJson(route, { error: `Unhandled E2E API mock: ${url.pathname}` }, 404);
      return;
    }

    const result = await resolveResponse(rule.response, request);
    if (isApiResponse(result)) {
      await respondJson(route, result.body, result.status ?? 200);
      return;
    }
    await respondJson(route, result, 200);
  });

  return {
    unknownRequests: () => [...unknownRequests],
  };
}

export async function expectNoUnhandledApiRequests(api: { unknownRequests: () => string[] }) {
  expect(api.unknownRequests()).toEqual([]);
}

function defaultRules(user: Record<string, unknown> | null): ApiMockRule[] {
  return [
    { path: '/api/me', response: user },
    {
      method: 'POST',
      path: '/api/auth/ws-token',
      response: {
        token: 'ws-e2e-token',
        expires_at: '2026-05-01T11:00:00Z',
        ttl_seconds: 3600,
        session_id: 'session-e2e',
        tab_id: 'tab-e2e',
      },
    },
    { path: '/api/runtime-settings', response: runtimeSettingsReady },
    {
      path: '/api/runtime-settings/update',
      response: {
        status: 'idle',
        available: true,
        active_agent_runs: 0,
        detail: 'Ready to update.',
      },
    },
    {
      path: '/api/cortex/bootstrap',
      response: {
        ideas: [sampleIdea],
        connections: [],
        team_members: [user ?? e2eUser],
        workspace_apps: [],
        workspace_pins: [],
        selected_idea: null,
        direct_thread: null,
        auth_status: { authenticated: Boolean(user), user },
        meta: { e2e: true },
      },
    },
    { path: '/api/cortex/ideas', response: [sampleIdea] },
    { path: '/api/cortex/connections', response: [] },
    { path: '/api/cortex/similarity-matrix', response: { nodes: [], matrix: [] } },
    { path: '/api/team/members', response: [user ?? e2eUser] },
    { path: '/api/workspace-apps/', response: [] },
    { path: '/api/workspace-pins/', response: [] },
    {
      path: '/api/chat/bootstrap',
      response: {
        room: sampleChatRoom,
        dms: [],
        notifications: [],
        unread_summary: { room: 1, dms: 0, total: 1 },
        default_mode: 'room',
        default_conversation_id: sampleChatRoom.id,
      },
    },
    { path: '/api/chat/conversations', response: [sampleChatRoom] },
    { path: '/api/chat/notifications', response: [] },
    {
      path: `/api/chat/conversations/${sampleChatRoom.id}/messages`,
      response: {
        conversation: sampleChatRoom,
        messages: [sampleChatRoom.last_message],
        has_more: false,
        next_before_seq: null,
      },
    },
    {
      path: '/api/notifications/summary',
      response: {
        chat_unread_total: 1,
        workspace_attention_total: 1,
        unread_notification_total: 1,
        unread_chat_notification_total: 1,
        unread_workspace_notification_total: 0,
      },
    },
    {
      path: '/api/notifications',
      response: [
        {
          id: 77,
          source: 'chat',
          kind: 'mention',
          title: 'Run cancellation needs review',
          body: 'Grace mentioned you in the team room.',
          actor_user_id: 'teammate-e2e',
          actor_name: 'Grace',
          actor_color: '#5b8def',
          idea_id: sampleIdea.id,
          conversation_id: sampleChatRoom.id,
          thread_root_message_id: null,
          occurrence_count: 1,
          payload: {},
          created_at: '2026-05-01T10:06:00Z',
          updated_at: '2026-05-01T10:06:00Z',
          read_at: null,
        },
      ],
    },
    { path: '/api/notifications/preferences', response: { sound_enabled: true, message_notifications_enabled: true } },
    { method: 'PATCH', path: '/api/notifications/preferences', response: { sound_enabled: true, message_notifications_enabled: true } },
    {
      method: 'POST',
      path: '/api/notifications/read-all',
      response: {
        chat_unread_total: 0,
        workspace_attention_total: 0,
        unread_notification_total: 0,
        unread_chat_notification_total: 0,
        unread_workspace_notification_total: 0,
      },
    },
    {
      path: '/api/vault/pin-status',
      response: { has_pin: false, locked: false, failed_attempts: 0 },
    },
    { path: '/api/vault/', response: [] },
    { path: '/api/vault/missing', response: [] },
    { path: '/api/vault/agent-grants', response: [] },
    { path: '/api/vault/project-bindings', response: [] },
    { path: '/api/vault/org-users', response: [] },
    { path: '/api/vault/log', response: [] },
    { path: '/api/agent-connections', response: [] },
  ];
}

async function stubWebSocket(page: Page) {
  await page.addInitScript(() => {
    class E2EWebSocket extends EventTarget {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;

      readonly url: string;
      readonly protocol = '';
      readonly extensions = '';
      binaryType: BinaryType = 'blob';
      readyState = E2EWebSocket.OPEN;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;

      constructor(url: string | URL) {
        super();
        this.url = String(url);
        window.setTimeout(() => {
          const event = new Event('open');
          this.onopen?.(event);
          this.dispatchEvent(event);
        }, 0);
      }

      send(_data: string | ArrayBufferLike | Blob | ArrayBufferView) {}

      close() {
        this.readyState = E2EWebSocket.CLOSED;
        const event = new CloseEvent('close');
        this.onclose?.(event);
        this.dispatchEvent(event);
      }
    }

    window.WebSocket = E2EWebSocket as unknown as typeof WebSocket;
  });
}

function matchesRule(rule: ApiMockRule, request: Request, pathname: string) {
  if (rule.method && rule.method.toUpperCase() !== request.method()) return false;
  if (typeof rule.path === 'string') return rule.path === pathname;
  return rule.path.test(pathname);
}

async function resolveResponse(response: ApiMockValue, request: Request) {
  if (typeof response === 'function') {
    return response(request);
  }
  return response;
}

function isApiResponse(value: unknown): value is ApiResponse {
  return Boolean(
    value
      && typeof value === 'object'
      && 'status' in value
      && 'body' in value
  );
}

async function respondJson(route: Route, body: JsonValue, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}
