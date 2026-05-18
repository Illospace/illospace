import {
  api,
  type ChatBootstrap,
  type ChatConversationPage as ChatConversationPageResponse,
  type ChatConversationSummary,
  type ChatMessage as ChatMessageRecord,
  type ChatMessageCreateInput,
  type ChatNotification,
  type ChatReadUpdateInput,
  type ChatThreadPage as ChatThreadPageResponse,
  type ChatUnreadThread,
  type ChatUnreadSummary,
} from '$lib/api/client';
import { auth } from '$lib/stores/auth.svelte';
import { ui } from '$lib/stores/ui.svelte';
import { wsClient } from '$lib/stores/ws.svelte';
import { parseServerTimeMs } from '$lib/utils/datetime';

const CHAT_PAGE_LIMIT = 50;
const TYPING_TTL_MS = 3000;
const TYPING_THROTTLE_MS = 1500;
const DEFAULT_UNREAD_SUMMARY: ChatUnreadSummary = { room: 0, dms: 0, total: 0 };

export type ChatMode = 'room' | 'dms' | 'unread';
export type ChatRoomSubview = 'timeline' | 'thread';
export type ChatConnectionState = 'idle' | 'connecting' | 'connected';

export interface ChatMessage extends ChatMessageRecord {
  optimistic?: boolean;
  failed?: boolean;
  error?: string | null;
}

export interface ChatConversationPageState {
  conversationId: string;
  messages: ChatMessage[];
  hasMore: boolean;
  nextBeforeSeq: number | null;
  loaded: boolean;
  loading: boolean;
  loadingOlder: boolean;
  sending: boolean;
  error: string | null;
}

export interface ChatThreadPageState {
  rootMessageId: number;
  conversationId: string | null;
  rootMessage: ChatMessage | null;
  replies: ChatMessage[];
  hasMore: boolean;
  nextBeforeSeq: number | null;
  loaded: boolean;
  loading: boolean;
  loadingOlder: boolean;
  sending: boolean;
  error: string | null;
}

function draftKeyForConversation(kind: 'room' | 'dm', id?: string | null): string {
  return kind === 'room' ? 'room' : `dm:${id ?? ''}`;
}

function draftKeyForThread(rootMessageId: number): string {
  return `thread:${rootMessageId}`;
}

function createConversationPageState(conversationId: string): ChatConversationPageState {
  return {
    conversationId,
    messages: [],
    hasMore: false,
    nextBeforeSeq: null,
    loaded: false,
    loading: false,
    loadingOlder: false,
    sending: false,
    error: null,
  };
}

function createThreadPageState(
  rootMessageId: number,
  conversationId: string | null = null,
): ChatThreadPageState {
  return {
    rootMessageId,
    conversationId,
    rootMessage: null,
    replies: [],
    hasMore: false,
    nextBeforeSeq: null,
    loaded: false,
    loading: false,
    loadingOlder: false,
    sending: false,
    error: null,
  };
}

function cloneUnreadSummary(summary?: Partial<ChatUnreadSummary> | null): ChatUnreadSummary {
  return {
    room: summary?.room ?? 0,
    dms: summary?.dms ?? 0,
    total: summary?.total ?? 0,
  };
}

function normalizeMessage(message: ChatMessageRecord): ChatMessage {
  return {
    ...message,
    optimistic: false,
    failed: false,
    error: null,
  };
}

function compareMessagesAsc(a: ChatMessage, b: ChatMessage): number {
  if (a.conversation_seq !== b.conversation_seq) return a.conversation_seq - b.conversation_seq;
  const aTime = parseServerTimeMs(a.created_at);
  const bTime = parseServerTimeMs(b.created_at);
  if (aTime !== bTime) return aTime - bTime;
  return a.id - b.id;
}

function sortMessages(messages: ChatMessage[]): ChatMessage[] {
  return [...messages].sort(compareMessagesAsc);
}

function sortDms(conversations: ChatConversationSummary[]): ChatConversationSummary[] {
  return [...conversations].sort((a, b) => {
    if (a.last_message_seq !== b.last_message_seq) return b.last_message_seq - a.last_message_seq;
    return parseServerTimeMs(b.updated_at) - parseServerTimeMs(a.updated_at);
  });
}

function sortNotifications(notifications: ChatNotification[]): ChatNotification[] {
  return [...notifications].sort((a, b) => {
    const createdDiff = parseServerTimeMs(b.created_at) - parseServerTimeMs(a.created_at);
    if (createdDiff !== 0) return createdDiff;
    return b.id - a.id;
  });
}

function messageMatches(a: Pick<ChatMessage, 'id' | 'client_generated_id'>, b: Pick<ChatMessage, 'id' | 'client_generated_id'>): boolean {
  return a.id === b.id || !!(a.client_generated_id && a.client_generated_id === b.client_generated_id);
}

function messageDetail(err: any, fallback: string): string {
  return err?.detail || err?.message || fallback;
}

class ChatStore {
  bootstrapped = $state(false);
  bootstrapping = $state(false);
  bootstrapError = $state<string | null>(null);
  refreshingConversations = $state(false);
  notificationsLoading = $state(false);
  notificationsError = $state<string | null>(null);
  lastError = $state<string | null>(null);

  isOpen = $state(false);
  mode = $state<ChatMode>('room');
  roomSubview = $state<ChatRoomSubview>('timeline');
  connectionState = $state<ChatConnectionState>('idle');

  room = $state<ChatConversationSummary | null>(null);
  dms = $state<ChatConversationSummary[]>([]);
  selectedDmId = $state<string | null>(null);
  activeThreadRootId = $state<number | null>(null);

  notifications = $state<ChatNotification[]>([]);
  unreadThreads = $state<ChatUnreadThread[]>([]);
  unreadSummary = $state<ChatUnreadSummary>(cloneUnreadSummary(DEFAULT_UNREAD_SUMMARY));
  unreadThreadsLoading = $state(false);
  unreadThreadsError = $state<string | null>(null);

  conversationPages = $state<Record<string, ChatConversationPageState>>({});
  threadPages = $state<Record<string, ChatThreadPageState>>({});
  conversationPresence = $state<Record<string, string[]>>({});
  threadPresence = $state<Record<string, string[]>>({});
  conversationTyping = $state<Record<string, string[]>>({});
  threadTyping = $state<Record<string, string[]>>({});
  threadAgentStates = $state<Record<string, Record<string, any>>>({});
  draftByContext = $state<Record<string, string>>({});

  private _initialized = false;
  private _wsUnsubs: (() => void)[] = [];
  private _conversationLoadPromises = new Map<string, Promise<void>>();
  private _threadLoadPromises = new Map<number, Promise<void>>();
  private _typingTimers = new Map<string, Map<string, ReturnType<typeof setTimeout>>>();
  private _lastTypingSentAt = new Map<string, number>();
  private _refreshConversationsTimer: ReturnType<typeof setTimeout> | null = null;
  private _bootstrapPromise: Promise<void> | null = null;
  private _unreadThreadsPromise: Promise<void> | null = null;
  private _subscribedConversationId: string | null = null;
  private _subscribedThreadRootId: number | null = null;
  private _reconnectPending = false;
  private _lastReadSignatureByConversation = new Map<string, string>();
  private _nextTempMessageId = -1;
  private _persistKey = 'illo:chat:state';

  constructor() {
    this._loadPersistedState();
  }

  get roomId(): string | null {
    return this.room?.id ?? null;
  }

  get conversations(): ChatConversationSummary[] {
    return this.room ? [this.room, ...this.dms] : [...this.dms];
  }

  get activeConversationId(): string | null {
    if (this.mode === 'room') return this.roomId;
    if (this.mode === 'unread') return null;
    return this.selectedDmId ?? this.dms[0]?.id ?? null;
  }

  get activeConversation(): ChatConversationSummary | null {
    return this.conversationById(this.activeConversationId);
  }

  get activeConversationPage(): ChatConversationPageState | null {
    return this.conversationPageFor(this.activeConversationId);
  }

  get activeThreadPage(): ChatThreadPageState | null {
    if (this.mode !== 'room' || this.roomSubview !== 'thread' || this.activeThreadRootId == null) {
      return null;
    }
    return this.threadPageFor(this.activeThreadRootId);
  }

  get activeMessages(): ChatMessage[] {
    return this.activeConversationPage?.messages ?? [];
  }

  get activeThreadMessages(): ChatMessage[] {
    const page = this.activeThreadPage;
    if (!page) return [];
    return page.rootMessage ? [page.rootMessage, ...page.replies] : [...page.replies];
  }

  get activeDraftKey(): string | null {
    if (this.mode === 'room') {
      if (this.roomSubview === 'thread' && this.activeThreadRootId != null) {
        return draftKeyForThread(this.activeThreadRootId);
      }
      return draftKeyForConversation('room', this.roomId);
    }

    const conversationId = this.activeConversationId;
    return conversationId ? draftKeyForConversation('dm', conversationId) : null;
  }

  get activeDraft(): string {
    const key = this.activeDraftKey;
    return key ? this.draftByContext[key] ?? '' : '';
  }

  get unreadNotifications(): number {
    return this.notifications.filter((notification) => !notification.read_at).length;
  }

  conversationById(conversationId: string | null | undefined): ChatConversationSummary | null {
    if (!conversationId) return null;
    if (this.room?.id === conversationId) return this.room;
    return this.dms.find((dm) => dm.id === conversationId) ?? null;
  }

  conversationPageFor(conversationId: string | null | undefined): ChatConversationPageState | null {
    if (!conversationId) return null;
    return this.conversationPages[conversationId] ?? createConversationPageState(conversationId);
  }

  threadPageFor(rootMessageId: number | null | undefined): ChatThreadPageState | null {
    if (rootMessageId == null) return null;
    return this.threadPages[String(rootMessageId)] ?? createThreadPageState(rootMessageId);
  }

  typingUsersForConversation(conversationId: string | null | undefined): string[] {
    if (!conversationId) return [];
    return this.conversationTyping[conversationId] ?? [];
  }

  typingUsersForThread(rootMessageId: number | null | undefined): string[] {
    if (rootMessageId == null) return [];
    return this.threadTyping[String(rootMessageId)] ?? [];
  }

  presenceForConversation(conversationId: string | null | undefined): string[] {
    if (!conversationId) return [];
    return this.conversationPresence[conversationId] ?? [];
  }

  presenceForThread(rootMessageId: number | null | undefined): string[] {
    if (rootMessageId == null) return [];
    return this.threadPresence[String(rootMessageId)] ?? [];
  }

  agentStateForThread(rootMessageId: number | null | undefined): Record<string, any> | null {
    if (rootMessageId == null) return null;
    return this.threadAgentStates[String(rootMessageId)] ?? null;
  }

  async setup() {
    if (!this._initialized) {
      this._initialized = true;
      this.connectionState = 'connecting';
      this._registerWsHandlers();
    }

    if (!this.bootstrapped) {
      try {
        await this.bootstrap();
      } catch {
        // Error state is already recorded in the store.
      }
    } else {
      this._syncSubscriptions();
    }
  }

  teardown() {
    if (!this._initialized) return;
    this._initialized = false;
    this.connectionState = 'idle';
    this._wsUnsubs.forEach((fn) => fn());
    this._wsUnsubs = [];
    this._subscribedConversationId = null;
    this._subscribedThreadRootId = null;
    if (this.isOpen) wsClient.send('chat_close');
    this._clearRealtimeEphemeral();
  }

  reset() {
    this.teardown();
    this.bootstrapped = false;
    this.bootstrapping = false;
    this.bootstrapError = null;
    this.refreshingConversations = false;
    this.notificationsLoading = false;
    this.notificationsError = null;
    this.lastError = null;
    this.room = null;
    this.dms = [];
    this.selectedDmId = null;
    this.activeThreadRootId = null;
    this.notifications = [];
    this.unreadThreads = [];
    this.unreadSummary = cloneUnreadSummary(DEFAULT_UNREAD_SUMMARY);
    this.unreadThreadsLoading = false;
    this.unreadThreadsError = null;
    this.conversationPages = {};
    this.threadPages = {};
    this.threadAgentStates = {};
    this.draftByContext = {};
    this._lastReadSignatureByConversation.clear();
  }

  async bootstrap(force = false) {
    if (this._bootstrapPromise && !force) return this._bootstrapPromise;

    const run = async () => {
      this.bootstrapping = true;
      this.bootstrapError = null;
      try {
        const payload = await api.chatBootstrap();
        this._applyBootstrap(payload);
        this.lastError = null;
        await this._loadVisibleSurfaces({ force });
        this._syncSubscriptions();
      } catch (err: any) {
        const detail = messageDetail(err, 'Failed to load chat');
        this.bootstrapError = detail;
        this.lastError = detail;
        ui.toast(detail, 'error');
        throw err;
      } finally {
        this.bootstrapping = false;
        this._bootstrapPromise = null;
      }
    };

    const promise = run();
    this._bootstrapPromise = promise;
    return promise;
  }

  async refreshConversations(silent = true) {
    this.refreshingConversations = true;
    try {
      const conversations = await api.listChatConversations();
      const room = conversations.find(
        (conversation) => conversation.type === 'room' || conversation.stable_key === 'org-room',
      );
      if (room) this.room = room;
      this.dms = sortDms(conversations.filter((conversation) => conversation.type === 'dm'));
      this._reconcileSelectionAfterConversationUpdate();
      this._recalculateUnreadSummary();
      this._persistState();
    } catch (err: any) {
      const detail = messageDetail(err, 'Failed to refresh conversations');
      this.lastError = detail;
      if (!silent) ui.toast(detail, 'error');
    } finally {
      this.refreshingConversations = false;
    }
  }

  async refreshNotifications(silent = true) {
    this.notificationsLoading = true;
    this.notificationsError = null;
    try {
      this.notifications = sortNotifications(await api.listChatNotifications());
    } catch (err: any) {
      const detail = messageDetail(err, 'Failed to load notifications');
      this.notificationsError = detail;
      this.lastError = detail;
      if (!silent) ui.toast(detail, 'error');
    } finally {
      this.notificationsLoading = false;
    }
  }

  async refreshUnreadThreads(silent = true) {
    if (this._unreadThreadsPromise) return this._unreadThreadsPromise;

    this.unreadThreadsLoading = true;
    this.unreadThreadsError = null;
    const promise = (async () => {
      try {
        this.unreadThreads = await api.listChatUnreadThreads();
      } catch (err: any) {
        const detail = messageDetail(err, 'Failed to load unread threads');
        this.unreadThreads = [];
        this.unreadThreadsError = detail;
        this.lastError = detail;
        if (!silent) ui.toast(detail, 'error');
      } finally {
        this.unreadThreadsLoading = false;
        this._unreadThreadsPromise = null;
      }
    })();

    this._unreadThreadsPromise = promise;
    return promise;
  }

  async ensureDm(userId: string, options: { select?: boolean } = {}) {
    const dm = await api.createChatDm(userId);
    this._upsertConversationSummary(dm);
    if (options.select !== false) {
      await this.selectDm(dm.id);
    }
    return dm;
  }

  async openDock() {
    this.isOpen = true;
    this._persistState();
    if (!this.bootstrapped) {
      try {
        await this.bootstrap();
      } catch {
        return;
      }
    }
    this._syncSubscriptions();
  }

  closeDock() {
    this.isOpen = false;
    this._persistState();
    if (this._initialized) wsClient.send('chat_close');
    this._subscribedConversationId = null;
    this._subscribedThreadRootId = null;
    this._clearRealtimeEphemeral();
  }

  toggleDock() {
    if (this.isOpen) this.closeDock();
    else void this.openDock();
  }

  async selectRoom() {
    const roomId = this.roomId;
    const roomLoaded = roomId ? !!this.conversationPages[roomId]?.loaded : false;
    const activeThreadLoaded = this.activeThreadRootId != null
      ? !!this.threadPages[String(this.activeThreadRootId)]?.loaded
      : true;
    const shouldEnsureVisible =
      this.mode !== 'room' ||
      !roomLoaded ||
      (this.roomSubview === 'thread' && !activeThreadLoaded);

    this.mode = 'room';
    this._persistState();
    if (shouldEnsureVisible) {
      await this._ensureRoomVisibleLoaded();
    }
    this._syncSubscriptions();
  }

  async selectUnread() {
    this.mode = 'unread';
    this.roomSubview = 'timeline';
    this.activeThreadRootId = null;
    this._persistState();
    await this.refreshUnreadThreads();
    if (this.unreadSummary.total <= 0 || this.unreadThreads.length === 0) {
      await this.selectRoom();
      return;
    }
    this._syncSubscriptions();
  }

  async selectDm(conversationId: string | null) {
    if (!conversationId) {
      await this.selectRoom();
      return;
    }

    this.mode = 'dms';
    this.selectedDmId = conversationId;
    this._persistState();
    await this.loadConversation(conversationId);
    this._syncSubscriptions();
  }

  async setMode(mode: ChatMode) {
    if (mode === 'room') {
      await this.selectRoom();
      return;
    }
    if (mode === 'unread') {
      await this.selectUnread();
      return;
    }

    this.mode = 'dms';
    if (!this.selectedDmId) this.selectedDmId = this.dms[0]?.id ?? null;
    this._persistState();
    if (this.selectedDmId) {
      await this.loadConversation(this.selectedDmId);
    }
    this._syncSubscriptions();
  }

  async openThread(rootMessageId: number) {
    const roomId = this.roomId;
    const roomLoaded = roomId ? !!this.conversationPages[roomId]?.loaded : false;
    const threadLoaded = !!this.threadPages[String(rootMessageId)]?.loaded;
    const alreadyOpenSameThread =
      this.mode === 'room' &&
      this.roomSubview === 'thread' &&
      this.activeThreadRootId === rootMessageId;

    this.mode = 'room';
    this.roomSubview = 'thread';
    this.activeThreadRootId = rootMessageId;
    this._persistState();
    this._syncSubscriptions();
    if (roomId && !roomLoaded) {
      await this.loadConversation(roomId, { silent: true });
    }
    if (!alreadyOpenSameThread || !threadLoaded) {
      await this.loadThread(rootMessageId, { silent: true });
    }
    this._syncSubscriptions();
  }

  closeThread() {
    this.roomSubview = 'timeline';
    this.activeThreadRootId = null;
    this._persistState();
    this._syncSubscriptions();
    void this.markActiveConversationRead();
  }

  setDraft(key: string, value: string) {
    this.draftByContext = {
      ...this.draftByContext,
      [key]: value,
    };
  }

  setActiveDraft(value: string) {
    const key = this.activeDraftKey;
    if (!key) return;
    this.setDraft(key, value);
  }

  async loadConversation(
    conversationId: string,
    options: { force?: boolean; loadOlder?: boolean; silent?: boolean; limit?: number } = {},
  ) {
    if (!conversationId) return;
    const current = this.conversationPages[conversationId] ?? createConversationPageState(conversationId);
    if (!options.force && !options.loadOlder && current.loaded) {
      return;
    }
    if (this._conversationLoadPromises.has(conversationId) && !options.force) {
      return this._conversationLoadPromises.get(conversationId);
    }

    if (options.loadOlder && (!current.loaded || !current.hasMore || current.nextBeforeSeq == null)) return;

    const beforeSeq = options.loadOlder ? current.nextBeforeSeq : undefined;
    const promise = (async () => {
      this._setConversationPage(conversationId, (page) => ({
        ...page,
        loading: !options.loadOlder,
        loadingOlder: !!options.loadOlder,
        error: null,
      }));

      try {
        const response = await api.getChatConversationMessages(conversationId, {
          beforeSeq,
          limit: options.limit ?? CHAT_PAGE_LIMIT,
        });
        this._applyConversationPageResponse(response, { appendOlder: !!options.loadOlder });

        if (
          this.activeConversationId === conversationId &&
          (this.mode === 'dms' || this.roomSubview === 'timeline')
        ) {
          await this.markActiveConversationRead();
        }
      } catch (err: any) {
        const detail = messageDetail(err, 'Failed to load conversation');
        this._setConversationPage(conversationId, (page) => ({
          ...page,
          loading: false,
          loadingOlder: false,
          error: detail,
        }));
        this.lastError = detail;
        if (!options.silent) ui.toast(detail, 'error');
      } finally {
        this._conversationLoadPromises.delete(conversationId);
      }
    })();

    this._conversationLoadPromises.set(conversationId, promise);
    return promise;
  }

  async loadThread(
    rootMessageId: number,
    options: { force?: boolean; loadOlder?: boolean; silent?: boolean; limit?: number } = {},
  ) {
    const key = String(rootMessageId);
    const current = this.threadPages[key] ?? createThreadPageState(rootMessageId, this.roomId);
    if (!options.force && !options.loadOlder && current.loaded) {
      return;
    }
    if (this._threadLoadPromises.has(rootMessageId) && !options.force) {
      return this._threadLoadPromises.get(rootMessageId);
    }

    if (options.loadOlder && (!current.loaded || !current.hasMore || current.nextBeforeSeq == null)) return;

    const beforeSeq = options.loadOlder ? current.nextBeforeSeq : undefined;
    const promise = (async () => {
      this._setThreadPage(rootMessageId, (page) => ({
        ...page,
        loading: !options.loadOlder,
        loadingOlder: !!options.loadOlder,
        error: null,
      }));

      try {
        const response = await api.getChatThread(rootMessageId, {
          beforeSeq,
          limit: options.limit ?? CHAT_PAGE_LIMIT,
        });
        this._applyThreadPageResponse(response, { appendOlder: !!options.loadOlder });

        if (
          this.mode === 'room' &&
          this.roomSubview === 'thread' &&
          this.activeThreadRootId === rootMessageId
        ) {
          await this.markActiveConversationRead();
        }
      } catch (err: any) {
        const detail = messageDetail(err, 'Failed to load thread');
        this._setThreadPage(rootMessageId, (page) => ({
          ...page,
          loading: false,
          loadingOlder: false,
          error: detail,
        }));
        this.lastError = detail;
        if (!options.silent) ui.toast(detail, 'error');
        if (this.activeThreadRootId === rootMessageId) this.closeThread();
      } finally {
        this._threadLoadPromises.delete(rootMessageId);
      }
    })();

    this._threadLoadPromises.set(rootMessageId, promise);
    return promise;
  }

  async loadOlderMessages(conversationId = this.activeConversationId) {
    if (!conversationId) return;
    await this.loadConversation(conversationId, { loadOlder: true });
  }

  async loadOlderThreadReplies(rootMessageId = this.activeThreadRootId) {
    if (rootMessageId == null) return;
    await this.loadThread(rootMessageId, { loadOlder: true });
  }

  async sendActiveMessage(
    body?: string,
    options: {
      bodyFormat?: 'markdown' | 'plain';
      attachments?: any[];
      replyToMessageId?: number | null;
      metadata?: Record<string, any> | null;
    } = {},
  ) {
    if (this.mode === 'room' && this.roomSubview === 'thread' && this.activeThreadRootId != null) {
      return this.sendThreadReply(this.activeThreadRootId, body, options);
    }
    const conversationId = this.activeConversationId;
    if (!conversationId) return null;
    return this.sendConversationMessage(conversationId, body, options);
  }

  async sendConversationMessage(
    conversationId: string,
    body?: string,
    options: {
      bodyFormat?: 'markdown' | 'plain';
      attachments?: any[];
      metadata?: Record<string, any> | null;
    } = {},
  ) {
    const draftKey = this.room?.id === conversationId
      ? draftKeyForConversation('room', conversationId)
      : draftKeyForConversation('dm', conversationId);
    const bodyText = (body ?? this.draftByContext[draftKey] ?? '').trim();
    const attachments = options.attachments ?? [];
    if (!bodyText && attachments.length === 0) return null;

    const clientGeneratedId = this._createClientGeneratedId();
    const optimisticMessage = this._buildOptimisticMessage({
      conversationId,
      body: bodyText,
      bodyFormat: options.bodyFormat ?? 'markdown',
      attachments,
      metadata: options.metadata ?? null,
      clientGeneratedId,
      threadRootMessageId: null,
      replyToMessageId: null,
    });

    const clearDraftOnSuccess = (this.draftByContext[draftKey] ?? '').trim() === bodyText;
    this._setConversationPage(conversationId, (page) => ({
      ...page,
      sending: true,
      messages: this._upsertMessage(page.messages, optimisticMessage),
      loaded: true,
    }));
    this._updateConversationFromMessage(conversationId, optimisticMessage, { unreadCount: 0 });

    try {
      const created = normalizeMessage(
        await api.postChatConversationMessage(
          conversationId,
          this._buildMessageCreateInput(optimisticMessage),
        ),
      );
      this._replaceMessageEverywhere(optimisticMessage, created);
      this._updateConversationFromMessage(conversationId, created, { unreadCount: 0 });
      this._setConversationPage(conversationId, (page) => ({ ...page, sending: false }));
      if (clearDraftOnSuccess) this.setDraft(draftKey, '');
      return created;
    } catch (err: any) {
      const detail = messageDetail(err, 'Failed to send message');
      this._markMessageFailed(optimisticMessage, detail);
      this._setConversationPage(conversationId, (page) => ({ ...page, sending: false }));
      this.lastError = detail;
      ui.toast(detail, 'error');
      return null;
    }
  }

  async sendThreadReply(
    rootMessageId: number,
    body?: string,
    options: {
      bodyFormat?: 'markdown' | 'plain';
      attachments?: any[];
      replyToMessageId?: number | null;
      metadata?: Record<string, any> | null;
    } = {},
  ) {
    const conversationId = this.roomId;
    if (!conversationId) return null;

    const draftKey = draftKeyForThread(rootMessageId);
    const bodyText = (body ?? this.draftByContext[draftKey] ?? '').trim();
    const attachments = options.attachments ?? [];
    if (!bodyText && attachments.length === 0) return null;

    const clientGeneratedId = this._createClientGeneratedId();
    const optimisticReply = this._buildOptimisticMessage({
      conversationId,
      body: bodyText,
      bodyFormat: options.bodyFormat ?? 'markdown',
      attachments,
      metadata: options.metadata ?? null,
      clientGeneratedId,
      threadRootMessageId: rootMessageId,
      replyToMessageId: options.replyToMessageId ?? null,
    });

    const clearDraftOnSuccess = (this.draftByContext[draftKey] ?? '').trim() === bodyText;
    this._setThreadPage(rootMessageId, (page) => ({
      ...page,
      conversationId,
      sending: true,
      replies: this._upsertMessage(page.replies, optimisticReply),
      loaded: true,
    }));
    this._updateConversationFromMessage(conversationId, optimisticReply, { unreadCount: 0 });

    try {
      const created = normalizeMessage(
        await api.postChatThreadReply(
          rootMessageId,
          this._buildMessageCreateInput(optimisticReply),
        ),
      );
      this._replaceMessageEverywhere(optimisticReply, created);
      this._updateConversationFromMessage(conversationId, created, { unreadCount: 0 });
      this._setThreadPage(rootMessageId, (page) => ({ ...page, sending: false }));
      if (clearDraftOnSuccess) this.setDraft(draftKey, '');
      return created;
    } catch (err: any) {
      const detail = messageDetail(err, 'Failed to reply in thread');
      this._markMessageFailed(optimisticReply, detail);
      this._setThreadPage(rootMessageId, (page) => ({ ...page, sending: false }));
      this.lastError = detail;
      ui.toast(detail, 'error');
      return null;
    }
  }

  async retryFailedMessage(message: ChatMessage) {
    if (!message.failed) return null;
    this._removeMessageEverywhere(message);

    if (message.thread_root_message_id != null && message.thread_root_message_id !== message.id) {
      return this.sendThreadReply(message.thread_root_message_id, message.body, {
        bodyFormat: message.body_format === 'plain' ? 'plain' : 'markdown',
        attachments: message.attachments,
        replyToMessageId: message.reply_to_message_id,
        metadata: message.metadata,
      });
    }

    return this.sendConversationMessage(message.conversation_id, message.body, {
      bodyFormat: message.body_format === 'plain' ? 'plain' : 'markdown',
      attachments: message.attachments,
      metadata: message.metadata,
    });
  }

  async markConversationRead(
    conversationId: string,
    body: ChatReadUpdateInput = {},
  ) {
    if (!conversationId) return this.unreadSummary;

    const conversation = this.conversationById(conversationId);
    const targetSeq = body.last_read_conversation_seq ?? conversation?.last_message_seq ?? 0;
    const targetMessageId = body.last_read_message_id ?? null;
    const nextConversationUnread = Math.max((conversation?.last_message_seq ?? targetSeq) - targetSeq, 0);
    const nextReadSignature = `${targetSeq}:${targetMessageId ?? 'none'}`;
    if (
      this._lastReadSignatureByConversation.get(conversationId) === nextReadSignature &&
      nextConversationUnread === 0
    ) {
      return this.unreadSummary;
    }

    this._setConversationUnreadCount(conversationId, nextConversationUnread);
    this._recalculateUnreadSummary();

    try {
      const unreadSummary = await api.chatMarkRead(conversationId, body);
      this._lastReadSignatureByConversation.set(conversationId, nextReadSignature);
      this.unreadSummary = cloneUnreadSummary(unreadSummary);
      if (this.mode === 'unread') void this.refreshUnreadThreads(true);
      return this.unreadSummary;
    } catch (err: any) {
      const detail = messageDetail(err, 'Failed to mark chat as read');
      this.lastError = detail;
      ui.toast(detail, 'error');
      await this.refreshConversations();
      throw err;
    }
  }

  async markActiveConversationRead() {
    const conversationId = this.activeConversationId;
    if (!conversationId) return this.unreadSummary;

    const target = this._readTargetForActiveContext();
    if (!target) return this.unreadSummary;
    return this.markConversationRead(conversationId, target);
  }

  async markNotificationRead(notificationId: number) {
    try {
      await api.markChatNotificationRead(notificationId);
      this.notifications = sortNotifications(
        this.notifications.map((notification) =>
          notification.id === notificationId
            ? { ...notification, read_at: notification.read_at || new Date().toISOString() }
            : notification,
        ),
      );
      if (this.mode === 'unread') void this.refreshUnreadThreads(true);
    } catch (err: any) {
      const detail = messageDetail(err, 'Failed to mark notification read');
      this.lastError = detail;
      ui.toast(detail, 'error');
      throw err;
    }
  }

  async markAllNotificationsRead() {
    try {
      await api.markAllChatNotificationsRead();
      const now = new Date().toISOString();
      this.notifications = sortNotifications(
        this.notifications.map((notification) => ({
          ...notification,
          read_at: notification.read_at || now,
        })),
      );
      if (this.mode === 'unread') void this.refreshUnreadThreads(true);
    } catch (err: any) {
      const detail = messageDetail(err, 'Failed to mark notifications read');
      this.lastError = detail;
      ui.toast(detail, 'error');
      throw err;
    }
  }

  sendTyping(
    context: { conversationId?: string | null; threadRootMessageId?: number | null } = {},
  ) {
    if (!this._initialized) return;
    const conversationId = context.conversationId ?? this.activeConversationId;
    if (!conversationId) return;

    const threadRootMessageId = context.threadRootMessageId === undefined
      ? (this.mode === 'room' && this.roomSubview === 'thread' ? this.activeThreadRootId : null)
      : context.threadRootMessageId;
    const scopeKey = `${conversationId}:${threadRootMessageId ?? 'conversation'}`;
    const now = Date.now();
    if ((this._lastTypingSentAt.get(scopeKey) ?? 0) + TYPING_THROTTLE_MS > now) return;
    this._lastTypingSentAt.set(scopeKey, now);

    wsClient.send('chat_typing', {
      conversation_id: conversationId,
      ...(threadRootMessageId != null ? { thread_root_message_id: threadRootMessageId } : {}),
    });
  }

  private _registerWsHandlers() {
    this._wsUnsubs.push(
      wsClient.on('connected', () => {
        this.connectionState = 'connecting';
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('authenticated', () => {
        this.connectionState = 'connected';
        this._syncSubscriptions();
        if (this._reconnectPending) {
          this._reconnectPending = false;
          void this._rehydrateAfterReconnect();
        }
      }),
    );

    this._wsUnsubs.push(
      wsClient.onReconnect(() => {
        this.connectionState = 'connecting';
        this._reconnectPending = true;
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_bootstrap', (msg) => {
        const payload = this._extractChatBootstrap(msg);
        if (!payload) return;
        this._applyBootstrap(payload);
        this._syncSubscriptions();
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_message_created', (msg) => {
        const conversationId = typeof msg.conversation_id === 'string' ? msg.conversation_id : null;
        if (!conversationId || !msg.message) return;
        const message = normalizeMessage(msg.message as ChatMessageRecord);
        this._upsertConversationMessage(conversationId, message);
        this._updateConversationFromMessage(conversationId, message);
        if (
          conversationId === this.activeConversationId &&
          (this.mode === 'dms' || this.roomSubview === 'timeline')
        ) {
          void this.markActiveConversationRead();
        }
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_thread_reply_created', (msg) => {
        const conversationId = typeof msg.conversation_id === 'string' ? msg.conversation_id : null;
        const threadRootMessageId = this._coerceRootMessageId(msg.thread_root_message_id ?? msg.message?.thread_root_message_id);
        if (!conversationId || threadRootMessageId == null || !msg.message) return;
        const message = normalizeMessage(msg.message as ChatMessageRecord);
        this._upsertThreadReply(threadRootMessageId, conversationId, message);
        this._updateConversationFromMessage(conversationId, message);
        if (
          this.mode === 'room' &&
          this.roomSubview === 'thread' &&
          this.activeThreadRootId === threadRootMessageId
        ) {
          void this.markActiveConversationRead();
        }
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_thread_summary_updated', (msg) => {
        const threadRootMessageId = this._coerceRootMessageId(msg.thread_root_message_id ?? msg.root_message?.id);
        if (threadRootMessageId == null || !msg.root_message) return;
        const rootMessage = normalizeMessage(msg.root_message as ChatMessageRecord);
        this._upsertRootMessage(threadRootMessageId, rootMessage);
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_unread_updated', (msg) => {
        const conversationId = typeof msg.conversation_id === 'string' ? msg.conversation_id : null;
        if (msg.unread_summary) {
          this.unreadSummary = cloneUnreadSummary(msg.unread_summary as Partial<ChatUnreadSummary>);
        }
        if (conversationId && typeof msg.unread_count === 'number') {
          this._setConversationUnreadCount(conversationId, msg.unread_count);
        } else if (
          conversationId &&
          typeof msg.last_read_conversation_seq === 'number'
        ) {
          const conversation = this.conversationById(conversationId);
          if (conversation) {
            this._setConversationUnreadCount(
              conversationId,
              Math.max(conversation.last_message_seq - msg.last_read_conversation_seq, 0),
            );
          }
        }
        if (conversationId && !this.conversationById(conversationId)) {
          this._scheduleConversationsRefresh();
        } else if (!msg.unread_summary) {
          this._recalculateUnreadSummary();
        }
        if (this.mode === 'unread') void this.refreshUnreadThreads(true);
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_notification_created', (msg) => {
        if (!msg.notification) return;
        this.notifications = sortNotifications(
          this._upsertNotification(this.notifications, msg.notification as ChatNotification),
        );
        if (this.mode === 'unread') void this.refreshUnreadThreads(true);
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_read_updated', (msg) => {
        const conversationId = typeof msg.conversation_id === 'string' ? msg.conversation_id : null;
        if (!conversationId) return;
        if (typeof msg.last_read_conversation_seq === 'number') {
          const conversation = this.conversationById(conversationId);
          if (conversation) {
            this._setConversationUnreadCount(
              conversationId,
              Math.max(conversation.last_message_seq - msg.last_read_conversation_seq, 0),
            );
            this._recalculateUnreadSummary();
          }
        }
        if (this.mode === 'unread') void this.refreshUnreadThreads(true);
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_presence', (msg) => {
        const userIds = Array.isArray(msg.user_ids)
          ? (msg.user_ids as unknown[]).map((value) => String(value))
          : [];
        if (msg.scope === 'thread') {
          const rootMessageId = this._coerceRootMessageId(msg.thread_root_message_id);
          if (rootMessageId == null) return;
          this.threadPresence = {
            ...this.threadPresence,
            [String(rootMessageId)]: userIds,
          };
          return;
        }

        const conversationId = typeof msg.conversation_id === 'string' ? msg.conversation_id : null;
        if (!conversationId) return;
        this.conversationPresence = {
          ...this.conversationPresence,
          [conversationId]: userIds,
        };
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_typing', (msg) => {
        const conversationId = typeof msg.conversation_id === 'string' ? msg.conversation_id : null;
        const userId = typeof msg.user_id === 'string' ? msg.user_id : null;
        if (!conversationId || !userId) return;

        if (msg.scope === 'thread') {
          const rootMessageId = this._coerceRootMessageId(msg.thread_root_message_id);
          if (rootMessageId == null) return;
          this._registerTyping('thread', String(rootMessageId), userId);
          return;
        }

        this._registerTyping('conversation', conversationId, userId);
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_agent_state', (msg) => {
        const rootMessageId = this._coerceRootMessageId(msg.thread_root_message_id ?? msg.message_id);
        if (rootMessageId == null) return;
        const { type: _type, ...payload } = msg as Record<string, any>;
        this.threadAgentStates = {
          ...this.threadAgentStates,
          [String(rootMessageId)]: payload,
        };
      }),
    );

    this._wsUnsubs.push(
      wsClient.on('chat_error', (msg) => {
        const code = typeof msg.code === 'string' ? msg.code : 'CHAT_ERROR';
        this.lastError = code;
        ui.toast(code, 'error');
      }),
    );
  }

  private _applyBootstrap(payload: ChatBootstrap) {
    this.room = payload.room;
    this.dms = sortDms(payload.dms);
    this.notifications = sortNotifications(payload.notifications);
    this.unreadSummary = cloneUnreadSummary(payload.unread_summary);
    this.bootstrapped = true;
    this._reconcileSelectionAfterConversationUpdate();
    this._persistState();
  }

  private _reconcileSelectionAfterConversationUpdate() {
    if (this.mode === 'dms' && this.selectedDmId && !this.dms.some((dm) => dm.id === this.selectedDmId)) {
      this.selectedDmId = this.dms[0]?.id ?? null;
    }
    if (this.mode === 'dms' && !this.selectedDmId) {
      this.selectedDmId = this.dms[0]?.id ?? null;
    }
    if (this.roomSubview === 'thread' && this.activeThreadRootId == null) {
      this.roomSubview = 'timeline';
    }
  }

  private async _loadVisibleSurfaces(options: { force?: boolean } = {}) {
    if (this.mode === 'unread') {
      await this.refreshUnreadThreads();
      return;
    }

    const activeConversationId = this.activeConversationId;
    if (activeConversationId) {
      await this.loadConversation(activeConversationId, {
        force: options.force,
        silent: true,
      });
    }

    if (
      this.mode === 'room' &&
      this.roomSubview === 'thread' &&
      this.activeThreadRootId != null
    ) {
      await this.loadThread(this.activeThreadRootId, {
        force: options.force,
        silent: true,
      });
    }
  }

  private async _ensureRoomVisibleLoaded() {
    const roomId = this.roomId;
    if (!roomId) return;
    await this.loadConversation(roomId, { silent: true });
    if (this.roomSubview === 'thread' && this.activeThreadRootId != null) {
      await this.loadThread(this.activeThreadRootId, { silent: true });
    }
  }

  private async _rehydrateAfterReconnect() {
    try {
      await this.bootstrap(true);
    } catch {
      // Bootstrap already set the error state.
    }
  }

  private _applyConversationPageResponse(
    response: ChatConversationPageResponse,
    options: { appendOlder: boolean },
  ) {
    const conversationId = response.conversation.id;
    const incomingMessages = response.messages.map(normalizeMessage);
    this._upsertConversationSummary(response.conversation);
    this._setConversationPage(conversationId, (page) => {
      const baseMessages = options.appendOlder
        ? this._mergeMessages(page.messages, incomingMessages)
        : this._replaceMessagesWithServerSnapshot(page.messages, incomingMessages);
      return {
        ...page,
        conversationId,
        messages: baseMessages,
        hasMore: response.has_more,
        nextBeforeSeq: response.next_before_seq,
        loaded: true,
        loading: false,
        loadingOlder: false,
        error: null,
      };
    });
  }

  private _applyThreadPageResponse(
    response: ChatThreadPageResponse,
    options: { appendOlder: boolean },
  ) {
    const rootMessageId = response.root_message.id;
    const normalizedRoot = normalizeMessage(response.root_message);
    const incomingReplies = response.replies.map(normalizeMessage);
    this._upsertConversationSummary(response.conversation);
    this._upsertRootMessage(rootMessageId, normalizedRoot);
    this._setThreadPage(rootMessageId, (page) => {
      const replies = options.appendOlder
        ? this._mergeMessages(page.replies, incomingReplies)
        : this._replaceMessagesWithServerSnapshot(page.replies, incomingReplies);
      return {
        ...page,
        rootMessageId,
        conversationId: response.conversation.id,
        rootMessage: this._preferMessage(page.rootMessage, normalizedRoot),
        replies,
        hasMore: response.has_more,
        nextBeforeSeq: response.next_before_seq,
        loaded: true,
        loading: false,
        loadingOlder: false,
        error: null,
      };
    });
  }

  private _buildMessageCreateInput(message: ChatMessage): ChatMessageCreateInput {
    return {
      body: message.body,
      body_format: message.body_format === 'plain' ? 'plain' : 'markdown',
      client_generated_id: message.client_generated_id,
      attachments: message.attachments,
      reply_to_message_id: message.reply_to_message_id,
      metadata: message.metadata,
    };
  }

  private _buildOptimisticMessage(input: {
    conversationId: string;
    body: string;
    bodyFormat: 'markdown' | 'plain';
    attachments: any[];
    metadata: Record<string, any> | null;
    clientGeneratedId: string;
    threadRootMessageId: number | null;
    replyToMessageId: number | null;
  }): ChatMessage {
    const user = auth.user;
    const lastSequence = this.conversationById(input.conversationId)?.last_message_seq ?? 0;
    return {
      id: this._nextTempMessageId--,
      conversation_id: input.conversationId,
      sender_user_id: user?.id ?? null,
      sender_kind: 'user',
      sender_name: user?.name || 'You',
      sender_color: user?.color ?? null,
      body: input.body,
      body_format: input.bodyFormat,
      client_generated_id: input.clientGeneratedId,
      thread_root_message_id: input.threadRootMessageId,
      reply_to_message_id: input.replyToMessageId,
      attachments: input.attachments,
      metadata: input.metadata,
      conversation_seq: lastSequence + 1,
      reply_count: 0,
      last_reply_at: null,
      last_reply_message_id: null,
      thread_preview_participants: [],
      created_at: new Date().toISOString(),
      edited_at: null,
      deleted_at: null,
      optimistic: true,
      failed: false,
      error: null,
    };
  }

  private _createClientGeneratedId(): string {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    return `chat-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }

  private _syncSubscriptions() {
    if (!this._initialized) return;

    if (!this.isOpen) {
      this._subscribedConversationId = null;
      this._subscribedThreadRootId = null;
      return;
    }

    wsClient.send('chat_open');

    const nextConversationId = this.activeConversationId;
    const previousConversationId = this._subscribedConversationId;
    if (previousConversationId && previousConversationId !== nextConversationId) {
      wsClient.send('chat_unsubscribe_conversation', { conversation_id: previousConversationId });
      this.conversationPresence = {
        ...this.conversationPresence,
        [previousConversationId]: [],
      };
      this.conversationTyping = {
        ...this.conversationTyping,
        [previousConversationId]: [],
      };
    }

    if (nextConversationId && previousConversationId !== nextConversationId) {
      wsClient.send('chat_subscribe_conversation', { conversation_id: nextConversationId });
      this._subscribedConversationId = nextConversationId;
    } else if (!nextConversationId) {
      this._subscribedConversationId = null;
    }

    const nextThreadRootId = this.mode === 'room' && this.roomSubview === 'thread'
      ? this.activeThreadRootId
      : null;
    const previousThreadRootId = this._subscribedThreadRootId;

    if (previousThreadRootId != null && previousThreadRootId !== nextThreadRootId) {
      wsClient.send('chat_unsubscribe_thread', {
        thread_root_message_id: previousThreadRootId,
      });
      this.threadPresence = {
        ...this.threadPresence,
        [String(previousThreadRootId)]: [],
      };
      this.threadTyping = {
        ...this.threadTyping,
        [String(previousThreadRootId)]: [],
      };
    }

    if (
      nextThreadRootId != null &&
      nextThreadRootId !== previousThreadRootId &&
      this.roomId
    ) {
      wsClient.send('chat_subscribe_thread', {
        conversation_id: this.roomId,
        thread_root_message_id: nextThreadRootId,
      });
      this._subscribedThreadRootId = nextThreadRootId;
    } else if (nextThreadRootId == null) {
      this._subscribedThreadRootId = null;
    }
  }

  private _upsertConversationSummary(conversation: ChatConversationSummary) {
    if (conversation.type === 'room' || conversation.stable_key === 'org-room') {
      this.room = conversation;
      return;
    }

    const index = this.dms.findIndex((dm) => dm.id === conversation.id);
    if (index === -1) {
      this.dms = sortDms([...this.dms, conversation]);
      return;
    }

    const next = [...this.dms];
    next[index] = conversation;
    this.dms = sortDms(next);
  }

  private _updateConversationFromMessage(
    conversationId: string,
    message: ChatMessage,
    options: { unreadCount?: number } = {},
  ) {
    const conversation = this.conversationById(conversationId);
    if (!conversation) {
      this._scheduleConversationsRefresh();
      return;
    }

    this._upsertConversationSummary({
      ...conversation,
      last_message: message,
      last_message_seq: Math.max(conversation.last_message_seq, message.conversation_seq),
      unread_count: options.unreadCount ?? conversation.unread_count,
      updated_at: message.created_at,
    });
  }

  private _setConversationUnreadCount(conversationId: string, unreadCount: number) {
    const conversation = this.conversationById(conversationId);
    if (!conversation) {
      this._scheduleConversationsRefresh();
      return;
    }
    this._upsertConversationSummary({
      ...conversation,
      unread_count: Math.max(unreadCount, 0),
    });
  }

  private _recalculateUnreadSummary() {
    const roomUnread = this.room?.unread_count ?? 0;
    const dmsUnread = this.dms.reduce((total, dm) => total + (dm.unread_count ?? 0), 0);
    this.unreadSummary = {
      room: roomUnread,
      dms: dmsUnread,
      total: roomUnread + dmsUnread,
    };
  }

  private _upsertConversationMessage(conversationId: string, message: ChatMessage) {
    const existing = this.conversationPages[conversationId];
    if (!existing && this.activeConversationId !== conversationId) return;

    if (message.thread_root_message_id != null) return;
    this._setConversationPage(conversationId, (page) => ({
      ...page,
      messages: this._upsertMessage(page.messages, message),
      loaded: page.loaded || this.activeConversationId === conversationId,
    }));
  }

  private _upsertThreadReply(rootMessageId: number, conversationId: string, message: ChatMessage) {
    const current = this.threadPages[String(rootMessageId)];
    if (!current && this.activeThreadRootId !== rootMessageId) return;

    this._setThreadPage(rootMessageId, (page) => ({
      ...page,
      conversationId,
      replies: this._upsertMessage(page.replies, message),
      loaded: page.loaded || this.activeThreadRootId === rootMessageId,
    }));
  }

  private _upsertRootMessage(rootMessageId: number, rootMessage: ChatMessage) {
    if (this.roomId) {
      this._setConversationPage(this.roomId, (page) => ({
        ...page,
        messages: this._upsertMessage(page.messages, rootMessage),
      }));
    }
    this._setThreadPage(rootMessageId, (page) => ({
      ...page,
      conversationId: rootMessage.conversation_id,
      rootMessage: this._preferMessage(page.rootMessage, rootMessage),
    }));
  }

  private _replaceMessageEverywhere(previous: ChatMessage, next: ChatMessage) {
    this._replaceConversationPageMessage(previous, next);
    this._replaceThreadPageMessage(previous, next);
  }

  private _replaceConversationPageMessage(previous: ChatMessage, next: ChatMessage) {
    for (const [conversationId, page] of Object.entries(this.conversationPages)) {
      const replaced = this._replaceMessage(page.messages, previous, next);
      if (replaced === page.messages) continue;
      this._setConversationPage(conversationId, (current) => ({
        ...current,
        messages: replaced,
      }));
    }
  }

  private _replaceThreadPageMessage(previous: ChatMessage, next: ChatMessage) {
    for (const [key, page] of Object.entries(this.threadPages)) {
      const nextPage: ChatThreadPageState = {
        ...page,
        rootMessage: page.rootMessage && messageMatches(page.rootMessage, previous)
          ? this._preferMessage(page.rootMessage, next)
          : page.rootMessage,
        replies: this._replaceMessage(page.replies, previous, next),
      };
      if (nextPage.rootMessage === page.rootMessage && nextPage.replies === page.replies) continue;
      this.threadPages = {
        ...this.threadPages,
        [key]: nextPage,
      };
    }
  }

  private _removeMessageEverywhere(message: ChatMessage) {
    for (const [conversationId, page] of Object.entries(this.conversationPages)) {
      const nextMessages = page.messages.filter((entry) => !messageMatches(entry, message));
      if (nextMessages.length === page.messages.length) continue;
      this._setConversationPage(conversationId, (current) => ({
        ...current,
        messages: nextMessages,
      }));
    }

    for (const [key, page] of Object.entries(this.threadPages)) {
      const nextReplies = page.replies.filter((entry) => !messageMatches(entry, message));
      const nextRoot = page.rootMessage && messageMatches(page.rootMessage, message) ? null : page.rootMessage;
      if (nextReplies.length === page.replies.length && nextRoot === page.rootMessage) continue;
      this.threadPages = {
        ...this.threadPages,
        [key]: {
          ...page,
          rootMessage: nextRoot,
          replies: nextReplies,
        },
      };
    }
  }

  private _markMessageFailed(message: ChatMessage, detail: string) {
    const failedMessage: ChatMessage = {
      ...message,
      optimistic: true,
      failed: true,
      error: detail,
    };
    this._replaceMessageEverywhere(message, failedMessage);
    this._updateConversationFromMessage(failedMessage.conversation_id, failedMessage);
  }

  private _replaceMessage(
    messages: ChatMessage[],
    previous: ChatMessage,
    next: ChatMessage,
  ): ChatMessage[] {
    let changed = false;
    const mapped = messages.map((message) => {
      if (!messageMatches(message, previous)) return message;
      changed = true;
      return this._preferMessage(message, next);
    });
    return changed ? sortMessages(mapped) : messages;
  }

  private _upsertMessage(messages: ChatMessage[], incoming: ChatMessage): ChatMessage[] {
    let replaced = false;
    const nextMessages = messages.map((message) => {
      if (!messageMatches(message, incoming)) return message;
      replaced = true;
      return this._preferMessage(message, incoming);
    });
    return sortMessages(replaced ? nextMessages : [...messages, incoming]);
  }

  private _mergeMessages(existing: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
    let merged = [...existing];
    for (const message of incoming) {
      merged = this._upsertMessage(merged, message);
    }
    return merged;
  }

  private _replaceMessagesWithServerSnapshot(existing: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
    const highestIncomingSeq = incoming.reduce(
      (max, message) => Math.max(max, message.conversation_seq ?? 0),
      0,
    );
    let merged = [...incoming];
    for (const message of existing) {
      if (merged.some((entry) => messageMatches(entry, message))) continue;
      if (!message.optimistic && !message.failed && message.conversation_seq <= highestIncomingSeq) continue;
      merged = this._upsertMessage(merged, message);
    }
    return merged;
  }

  private _preferMessage(current: ChatMessage | null, incoming: ChatMessage): ChatMessage {
    if (!current) return incoming;
    if (incoming.optimistic && !current.optimistic) return current;
    if (!incoming.optimistic && current.optimistic) return incoming;
    return {
      ...current,
      ...incoming,
      optimistic: incoming.optimistic ?? current.optimistic,
      failed: incoming.failed ?? current.failed,
      error: incoming.error ?? current.error,
    };
  }

  private _upsertNotification(notifications: ChatNotification[], incoming: ChatNotification): ChatNotification[] {
    const index = notifications.findIndex((notification) => notification.id === incoming.id);
    if (index === -1) return [...notifications, incoming];
    const next = [...notifications];
    next[index] = incoming;
    return next;
  }

  private _readTargetForActiveContext(): ChatReadUpdateInput | null {
    const conversationId = this.activeConversationId;
    if (!conversationId) return null;

    if (this.mode === 'room' && this.roomSubview === 'thread' && this.activeThreadRootId != null) {
      const page = this.activeThreadPage;
      if (!page) return null;
      const latest = page.replies[page.replies.length - 1] ?? page.rootMessage;
      if (!latest) return null;
      return {
        last_read_message_id: latest.id > 0 ? latest.id : undefined,
        last_read_conversation_seq: latest.conversation_seq,
      };
    }

    const page = this.activeConversationPage;
    const latest = page?.messages[page.messages.length - 1];
    if (!latest) return null;
    return {
      last_read_message_id: latest.id > 0 ? latest.id : undefined,
      last_read_conversation_seq: latest.conversation_seq,
    };
  }

  private _setConversationPage(
    conversationId: string,
    updater: (page: ChatConversationPageState) => ChatConversationPageState,
  ) {
    const current = this.conversationPages[conversationId] ?? createConversationPageState(conversationId);
    this.conversationPages = {
      ...this.conversationPages,
      [conversationId]: updater(current),
    };
  }

  private _setThreadPage(
    rootMessageId: number,
    updater: (page: ChatThreadPageState) => ChatThreadPageState,
  ) {
    const key = String(rootMessageId);
    const current = this.threadPages[key] ?? createThreadPageState(rootMessageId, this.roomId);
    this.threadPages = {
      ...this.threadPages,
      [key]: updater(current),
    };
  }

  private _registerTyping(scope: 'conversation' | 'thread', scopeId: string, userId: string) {
    const timerKey = `${scope}:${scopeId}`;
    const timers = this._typingTimers.get(timerKey) ?? new Map<string, ReturnType<typeof setTimeout>>();
    const existingTimeout = timers.get(userId);
    if (existingTimeout) clearTimeout(existingTimeout);

    const timeout = setTimeout(() => {
      const scopedTimers = this._typingTimers.get(timerKey);
      scopedTimers?.delete(userId);
      if (scopedTimers && scopedTimers.size === 0) this._typingTimers.delete(timerKey);
      if (scope === 'conversation') {
        const next = (this.conversationTyping[scopeId] ?? []).filter((entry) => entry !== userId);
        this.conversationTyping = {
          ...this.conversationTyping,
          [scopeId]: next,
        };
      } else {
        const next = (this.threadTyping[scopeId] ?? []).filter((entry) => entry !== userId);
        this.threadTyping = {
          ...this.threadTyping,
          [scopeId]: next,
        };
      }
    }, TYPING_TTL_MS);

    timers.set(userId, timeout);
    this._typingTimers.set(timerKey, timers);

    if (scope === 'conversation') {
      const next = new Set(this.conversationTyping[scopeId] ?? []);
      next.add(userId);
      this.conversationTyping = {
        ...this.conversationTyping,
        [scopeId]: [...next].sort(),
      };
      return;
    }

    const next = new Set(this.threadTyping[scopeId] ?? []);
    next.add(userId);
    this.threadTyping = {
      ...this.threadTyping,
      [scopeId]: [...next].sort(),
    };
  }

  private _clearRealtimeEphemeral() {
    for (const timers of this._typingTimers.values()) {
      for (const timeout of timers.values()) {
        clearTimeout(timeout);
      }
    }
    this._typingTimers.clear();
    this._lastTypingSentAt.clear();
    this.conversationPresence = {};
    this.threadPresence = {};
    this.conversationTyping = {};
    this.threadTyping = {};
  }

  private _scheduleConversationsRefresh(delayMs = 150) {
    if (this._refreshConversationsTimer) clearTimeout(this._refreshConversationsTimer);
    this._refreshConversationsTimer = setTimeout(() => {
      this._refreshConversationsTimer = null;
      void this.refreshConversations();
    }, delayMs);
  }

  private _extractChatBootstrap(message: Record<string, any>): ChatBootstrap | null {
    if (message.room && message.unread_summary) return message as ChatBootstrap;
    if (message.bootstrap?.room && message.bootstrap?.unread_summary) {
      return message.bootstrap as ChatBootstrap;
    }
    return null;
  }

  private _coerceRootMessageId(value: unknown): number | null {
    if (value == null || value === '') return null;
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (typeof value === 'string') {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  private _loadPersistedState() {
    if (typeof localStorage === 'undefined') return;
    try {
      const raw = localStorage.getItem(this._persistKey);
      if (!raw) return;
      const parsed = JSON.parse(raw) as {
        isOpen?: boolean;
        mode?: ChatMode;
        roomSubview?: ChatRoomSubview;
        selectedDmId?: string | null;
        activeThreadRootId?: number | null;
      };

      this.isOpen = !!parsed.isOpen;
      if (parsed.mode === 'room' || parsed.mode === 'dms' || parsed.mode === 'unread') {
        this.mode = parsed.mode;
      }
      if (parsed.selectedDmId) this.selectedDmId = parsed.selectedDmId;
      if (parsed.roomSubview === 'thread' && parsed.activeThreadRootId != null) {
        this.roomSubview = 'thread';
        this.activeThreadRootId = parsed.activeThreadRootId;
      } else {
        this.roomSubview = 'timeline';
        this.activeThreadRootId = null;
      }
    } catch {
      // Best-effort only.
    }
  }

  private _persistState() {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(
        this._persistKey,
        JSON.stringify({
          isOpen: this.isOpen,
          mode: this.mode,
          roomSubview: this.roomSubview,
          selectedDmId: this.selectedDmId,
          activeThreadRootId: this.roomSubview === 'thread' ? this.activeThreadRootId : null,
        }),
      );
    } catch {
      // Best-effort only.
    }
  }
}

export const chat = new ChatStore();
