import type { api as cortexApi } from '$lib/api/client';
import type { WsClient } from '$lib/api/ws';
import type { ui as cortexUi } from '$lib/stores/ui.svelte';
import {
  normalizeBrowserSessionRealtimeEvent,
  reduceBrowserSessionRealtimeEvent,
} from '$lib/features/browser-sessions/realtime/browserSessionRealtime';
import { activeRootRunCountsByIdea } from '$lib/utils/cortexRunActivity';
import { livePartialId } from '$lib/utils/cortexRunStream';
import type {
  BrowserDiscoveryResult,
  BrowserExtractResult,
  BrowserFrame,
  BrowserSessionState,
  Connection,
  Idea,
  StreamItem,
  VaultSecretPrompt,
} from '$lib/types/cortex';

export interface CortexRealtimeStoreBindings {
  ideas: Idea[];
  connections: Connection[];
  selectedIdeaId: string | null;
  panelOpen: boolean;
  stream: StreamItem[];
  browserSession: BrowserSessionState | null;
  browserFrame: BrowserFrame | null;
  browserDiscovery: BrowserDiscoveryResult | null;
  browserExtraction: BrowserExtractResult | null;
  vaultSecretPrompt: VaultSecretPrompt | null;
  archivedIdeas: Idea[];
  typingUsers: Map<string, { user_id: string; idea_id: string; timeout: ReturnType<typeof setTimeout> }>;

  load(): unknown;
  _refreshSelectedStream(): unknown;
  _normalizeIdeaStatus(status: string | null | undefined): string;
  _markIdeaSeen(ideaId: string, revision: string): void;
  _ideaRevision(idea: Pick<Idea, 'updated_at' | 'created_at'> | null | undefined): string;
  _normalizeIdea(idea: Idea): Idea;
  _hasWorkingIdeas(): boolean;
  _ensureIdeasSnapshotReconcile(): void;
  _stopIdeasSnapshotReconcile(): void;
  _ensureSelectedIdeaReconcile(): void;
  _stopSelectedIdeaReconcile(): void;
  _scheduleSelectedStreamRefresh(delayMs?: number): void;
  _handleRunUiEvent(msg: any): void;
  _handleBrowserSessionEvent(msg: any, frame?: BrowserFrame | null): void;
  _handleVaultSecretPrompt(msg: any): void;
  _upsertIdea(idea: Idea): void;
  _registerArchivedIdea(idea: Pick<Idea, 'id' | 'user_id'> | null | undefined): void;
  _rememberArchivedIdea(idea: Idea | null | undefined): void;
}

export function setupCortexRealtimeBindings(options: {
  store: CortexRealtimeStoreBindings;
  wsClient: WsClient;
  api: Pick<typeof cortexApi, 'getIdea'>;
  ui: Pick<typeof cortexUi, 'toast'>;
  statusChime: (status: string) => void;
  ding: () => void;
}): (() => void)[] {
  const { store, wsClient, api, ui, statusChime, ding } = options;
  const unsubs: (() => void)[] = [];

  function applyBrowserRealtimeResult(
    result: ReturnType<typeof reduceBrowserSessionRealtimeEvent>,
    options: { toastErrors?: boolean } = {},
  ) {
    store.browserSession = result.state.session;
    store.browserFrame = result.state.frame;
    store.browserDiscovery = result.state.discovery;
    store.browserExtraction = result.state.extraction;
    if (result.shouldSubscribeSessionId) {
      wsClient.send('browser_subscribe', { session_id: result.shouldSubscribeSessionId });
    }
    if (options.toastErrors && result.state.session?.status === 'error') {
      ui.toast(result.state.session.last_error || 'Browser session error', 'error');
    }
  }

  // Re-fetch state on reconnect (server restart, network blip)
  unsubs.push(
    wsClient.onReconnect(() => {
      store.load();
      // Re-fetch stream for selected idea
      store._refreshSelectedStream();
      if (store.browserSession?.id) {
        wsClient.send('browser_subscribe', { session_id: store.browserSession.id });
      }
    }),
  );

  unsubs.push(
    wsClient.on('status_change', (msg) => {
      const { idea_id, new_status } = msg;
      const normalizedStatus = store._normalizeIdeaStatus(new_status);
      const nextStatus = idea_id === store.selectedIdeaId && normalizedStatus === 'done'
        ? 'idle'
        : normalizedStatus;
      if (idea_id === store.selectedIdeaId && normalizedStatus === 'done') {
        const current = store.ideas.find((i) => i.id === idea_id);
        store._markIdeaSeen(idea_id, store._ideaRevision(current));
      }
      const statusPatch: Partial<Idea> = { status: nextStatus };
      if (normalizedStatus !== 'working') {
        statusPatch.active_agents = 0;
        statusPatch._agents = 0;
      }
      store.ideas = store.ideas.map((i) =>
        i.id === idea_id ? { ...i, ...statusPatch } : i,
      );
      statusChime(nextStatus);
      if (store._hasWorkingIdeas()) store._ensureIdeasSnapshotReconcile();
      else store._stopIdeasSnapshotReconcile();
      // Re-fetch full idea to get updated thread_count, active_agents, etc.
      api.getIdea(idea_id).then((fresh) => {
        const normalizedFresh = store._normalizeIdea({ ...fresh });
        if (idea_id === store.selectedIdeaId) {
          store._markIdeaSeen(idea_id, store._ideaRevision(fresh));
        }
        store.ideas = store.ideas.map((i) =>
          i.id === idea_id
            ? { ...i, ...normalizedFresh, status: idea_id === store.selectedIdeaId && normalizedFresh.status === 'done' ? 'idle' : normalizedFresh.status }
            : i,
        );
        if (store._hasWorkingIdeas()) store._ensureIdeasSnapshotReconcile();
        else store._stopIdeasSnapshotReconcile();
      }).catch((e) => console.warn('[cortex] failed to refresh idea', e));
      // Refresh stream when selected idea's status changes (run completed/failed)
      if (idea_id === store.selectedIdeaId) {
        if (normalizedStatus === 'working') {
          store._ensureSelectedIdeaReconcile();
        } else {
          store._stopSelectedIdeaReconcile();
        }
        store._scheduleSelectedStreamRefresh(normalizedStatus === 'done' ? 350 : 120);
      }
    }),
  );

  unsubs.push(
    wsClient.on('thread_message', (msg) => {
      if (msg.idea_id === store.selectedIdeaId) {
        if (msg.message) {
          const m = msg.message;
          const msgId = String(m.id);
          const visibleRunMessage = m.metadata?.hidden !== true && String(m.content || '').trim();
          if (visibleRunMessage) {
            const partialId = livePartialId(m.metadata?.run_id ?? msg.run_id);
            if (partialId) store.stream = store.stream.filter((item) => item.id !== partialId);
          }
          // Dedup: skip if message already present in stream
          if (store.stream.some((item) => item.type === 'message' && item.id === msgId)) return;
          store.stream = [
            ...store.stream,
            { type: 'message', timestamp: m.created_at, id: msgId, ...m },
          ];
          // Reconcile against the server shortly after append so delayed
          // run writes still appear without a manual refresh.
          store._scheduleSelectedStreamRefresh(200);
        } else {
          // Message payload missing expected shape — full refresh
          store._scheduleSelectedStreamRefresh(120);
        }
      }
    }),
  );

  for (const eventType of [
    'run_started',
    'step_started',
    'tool_started',
    'tool_finished',
    'text_delta',
    'run_completed',
  ]) {
    unsubs.push(
      wsClient.on(eventType, (msg) => {
        store._handleRunUiEvent({ ...msg, type: eventType });
      }),
    );
  }

  // Visual blocks — render inline in conversation stream
  unsubs.push(
    wsClient.on('visual_reply', (msg) => {
      if (msg.idea_id === store.selectedIdeaId && msg.block) {
        const b = msg.block;
        const blockId = `vb-${b.id}`;
        // Dedup: skip if already present
        if (store.stream.some((item) => item.id === blockId)) return;
        store.stream = [
          ...store.stream,
          {
            type: 'visual_block',
            timestamp: b.created_at || new Date().toISOString(),
            id: blockId,
            content_type: b.content_type,
            title: b.title,
            content: b.content,
            display_mode: b.display_mode || 'inline',
            run_id: b.run_id,
            position_after: b.position_after,
          },
        ];
      }
    }),
  );

  unsubs.push(
    wsClient.on('browser_session_state', (msg) => {
      store._handleBrowserSessionEvent(msg);
    }),
  );

  unsubs.push(
    wsClient.on('browser_session_frame', (msg) => {
      if (msg.frame) {
        store._handleBrowserSessionEvent(msg, msg.frame as BrowserFrame);
      }
    }),
  );

  unsubs.push(
    wsClient.on('browser_session_delta', (msg) => {
      applyBrowserRealtimeResult(reduceBrowserSessionRealtimeEvent({
        current: {
          session: store.browserSession,
          frame: store.browserFrame,
          discovery: store.browserDiscovery,
          extraction: store.browserExtraction,
        },
        selectedIdeaId: store.selectedIdeaId,
        event: normalizeBrowserSessionRealtimeEvent('browser_session_delta', msg),
      }));
    }),
  );

  unsubs.push(
    wsClient.on('browser_session_closed', (msg) => {
      applyBrowserRealtimeResult(reduceBrowserSessionRealtimeEvent({
        current: {
          session: store.browserSession,
          frame: store.browserFrame,
          discovery: store.browserDiscovery,
          extraction: store.browserExtraction,
        },
        selectedIdeaId: store.selectedIdeaId,
        event: normalizeBrowserSessionRealtimeEvent('browser_session_closed', msg),
      }));
    }),
  );

  unsubs.push(
    wsClient.on('browser_session_error', (msg) => {
      if (msg.session_id !== store.browserSession?.id) return;
      applyBrowserRealtimeResult(
        reduceBrowserSessionRealtimeEvent({
          current: {
            session: store.browserSession,
            frame: store.browserFrame,
            discovery: store.browserDiscovery,
            extraction: store.browserExtraction,
          },
          selectedIdeaId: store.selectedIdeaId,
          event: normalizeBrowserSessionRealtimeEvent('browser_session_error', msg),
        }),
        { toastErrors: true },
      );
    }),
  );

  unsubs.push(
    wsClient.on('vault_secret_prompt', (msg) => {
      store._handleVaultSecretPrompt(msg);
    }),
  );

  unsubs.push(
    wsClient.on('idea_created', async (msg) => {
      // Another client created an idea — fetch and add it
      if (msg.idea_id && !store.ideas.some((i) => i.id === msg.idea_id)) {
        try {
          const idea = await api.getIdea(msg.idea_id);
          store._upsertIdea(idea);
        } catch (e) { console.warn('[cortex] failed to fetch idea from peer', e); }
      }
    }),
  );

  unsubs.push(
    wsClient.on('idea_upserted', (msg) => {
      if (msg.idea) {
        store._upsertIdea(msg.idea);
        if (msg.idea.id === store.selectedIdeaId) {
          store._scheduleSelectedStreamRefresh(120);
        }
        if (store._hasWorkingIdeas()) store._ensureIdeasSnapshotReconcile();
        else store._stopIdeasSnapshotReconcile();
      }
    }),
  );

  unsubs.push(
    wsClient.on('title_generated', (msg) => {
      if (msg.idea_id && msg.title) {
        store.ideas = store.ideas.map((i) =>
          i.id === msg.idea_id ? { ...i, display_title: msg.title } : i,
        );
      }
    }),
  );

  unsubs.push(
    wsClient.on('budget_approval_needed', (msg) => {
      ding();
      ui.toast(`Approval needed: ${msg.summary || 'run'}`, 'info');
    }),
  );

  // Mentions — notify user
  unsubs.push(
    wsClient.on('mention', (msg) => {
      ding();
      ui.toast(`You were mentioned in: ${msg.idea_title || 'an idea'}`, 'info');
    }),
  );

  // Thought split — add child ideas to the list
  unsubs.push(
    wsClient.on('thought_split', (msg) => {
      if (msg.children) {
        for (const child of msg.children) {
          store._upsertIdea(child);
        }
      }
    }),
  );

  // Idea updated — another user changed idea fields
  unsubs.push(
    wsClient.on('idea_updated', (msg) => {
      if (msg.idea_id && msg.fields) {
        store.ideas = store.ideas.map((i) =>
          i.id === msg.idea_id ? store._normalizeIdea({ ...i, ...msg.fields }) : i,
        );
        // Refresh stream if the selected idea was updated
        if (msg.idea_id === store.selectedIdeaId) {
          store._scheduleSelectedStreamRefresh(120);
        }
      }
    }),
  );

  // Idea archived — another user archived/deleted an idea
  unsubs.push(
    wsClient.on('idea_archived', (msg) => {
      if (msg.idea_id) {
        const existing = store.ideas.find((i) => i.id === msg.idea_id);
        const archived = msg.idea ? { ...(existing || {}), ...msg.idea } as Idea : existing;
        store._registerArchivedIdea(archived);
        store._rememberArchivedIdea(archived);
        store.ideas = store.ideas.filter((i) => i.id !== msg.idea_id);
        if (store.selectedIdeaId === msg.idea_id) {
          store.selectedIdeaId = null;
          store.panelOpen = false;
          store.stream = [];
        }
      }
    }),
  );

  unsubs.push(
    wsClient.on('idea_restored', (msg) => {
      if (!msg.idea_id) return;
      store.archivedIdeas = store.archivedIdeas.filter((idea) => idea.id !== msg.idea_id);
      if (msg.idea) {
        store._upsertIdea(msg.idea as Idea);
      }
    }),
  );

  // Connection created — a new connection between ideas
  unsubs.push(
    wsClient.on('connection_created', (msg) => {
      if (msg.connection) {
        const c = msg.connection;
        if (!store.connections.some((conn) => conn.id === c.id)) {
          store.connections = [...store.connections, c];
        }
      }
    }),
  );

  // Connection deleted — a connection was removed
  unsubs.push(
    wsClient.on('connection_deleted', (msg) => {
      if (msg.connection_id) {
        store.connections = store.connections.filter((c) => c.id !== msg.connection_id);
      }
    }),
  );

  // Ops update — live run activity
  unsubs.push(
    wsClient.on('ops_update', (msg) => {
      const runs = Array.isArray(msg.runs) ? msg.runs : [];
      const counts = activeRootRunCountsByIdea(runs);
      store.ideas = store.ideas.map((idea) => {
        const hasActiveRootRun = (counts.get(idea.id) || 0) > 0;
        return {
          ...idea,
          ...(hasActiveRootRun ? { status: 'working' } : {}),
          active_agents: hasActiveRootRun ? 1 : 0,
          _agents: hasActiveRootRun ? 1 : 0,
        };
      });
      if (store._hasWorkingIdeas()) store._ensureIdeasSnapshotReconcile();
      else store._stopIdeasSnapshotReconcile();
    }),
  );

  // Typing indicators — show when other users are typing
  unsubs.push(
    wsClient.on('typing', (msg) => {
      if (msg.user_id && msg.idea_id) {
        // Clear existing timeout for this user
        const existing = store.typingUsers.get(msg.user_id);
        if (existing) clearTimeout(existing.timeout);
        // Set typing indicator with auto-clear after 3s
        const timeout = setTimeout(() => {
          store.typingUsers.delete(msg.user_id);
          store.typingUsers = new Map(store.typingUsers);
        }, 3000);
        store.typingUsers.set(msg.user_id, { user_id: msg.user_id, idea_id: msg.idea_id, timeout });
        store.typingUsers = new Map(store.typingUsers);
      }
    }),
  );

  return unsubs;
}
