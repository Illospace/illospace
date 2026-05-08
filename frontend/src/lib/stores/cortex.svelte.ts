import { browser, dev } from '$app/environment';
import { api, type CortexBootstrapPayload } from '$lib/api/client';
import { auth } from '$lib/stores/auth.svelte';
import {
  loadRunSettings,
  normalizeRunOptions as normalizeCortexRunOptions,
  normalizeRunSettings,
  persistRunSettings,
} from '$lib/features/cortex/controllers/runSettingsController';
import {
  hasWorkingIdeas as hasWorkingCortexIdeas,
  normalizeIdea as normalizeCortexIdea,
  normalizeIdeaStatus as normalizeCortexIdeaStatus,
  patchIdeaById,
} from '$lib/features/cortex/domain/ideaReducer';
import {
  archivedIdeaCountForUser as selectArchivedIdeaCountForUser,
  registerArchivedIdea as registerArchivedIdeaInState,
  rememberArchivedIdea as rememberArchivedIdeaInList,
  seedArchivedIdeaCounts as seedArchivedIdeaCountsInState,
  unregisterArchivedIdea as unregisterArchivedIdeaInState,
  type ArchiveCountState,
  type ArchiveIdeaIdentity,
} from '$lib/features/cortex/domain/archiveReducer';
import {
  SEEN_IDEA_REVISIONS_STORAGE_KEY,
  ideaRevision as cortexIdeaRevision,
  isIdeaSeen as isCortexIdeaSeen,
  markIdeaSeen as markCortexIdeaSeen,
  parseSeenIdeaRevisions,
  serializeSeenIdeaRevisions,
} from '$lib/features/cortex/domain/seenIdeaRevisions';
import { setupCortexRealtimeBindings, type CortexRealtimeStoreBindings } from '$lib/stores/cortexRealtime';
import { wsClient } from '$lib/stores/ws.svelte';
import { ui } from '$lib/stores/ui.svelte';
import { bloop, ding, statusChime } from '$lib/utils/sounds';
import type { TeamMember } from '$lib/utils/attractors';
import { getRunDecision } from '$lib/utils/backgroundRun';
import {
  browserCommandPayload,
  emptyBrowserSessionViewState,
} from '$lib/utils/cortexBrowserSession';
import { applyBrowserSnapshotToState } from '$lib/features/browser-sessions/domain/browserSessionState.svelte';
import {
  normalizeBrowserSessionRealtimeEvent,
  reduceBrowserSessionRealtimeEvent,
} from '$lib/features/browser-sessions/realtime/browserSessionRealtime';
import { buildLocalPreviewThreadStream, isLocalPreviewIdeaId } from '$lib/utils/cortexLocalPreview';
import { isActiveRun, mergeRunProgressSnapshot } from '$lib/utils/cortexRunPresentation';
import {
  applyAgentActivityToStream,
  applyAgentTextDeltaToStream,
  applyRunCompletedToStream,
  mergeLiveStreamState,
  runUiEventKey,
  type CortexRunStreamItem,
} from '$lib/utils/cortexRunStream';
import { normalizeHexColor } from '$lib/utils/constellationPresence';
import type {
  AgentRunOptions,
  BrowserDiscoveryResult,
  BrowserExtractResult,
  BrowserFrame,
  BrowserSessionState,
  Connection,
  CortexEffortLevel,
  CortexExecutionProfile,
  CortexIntelligenceTier,
  Idea,
  StreamItem,
  VaultSecretPrompt,
} from '$lib/types/cortex';

export type {
  AgentRunOptions,
  BrowserDiscoveryResult,
  BrowserExtractResult,
  BrowserFrame,
  BrowserSessionState,
  Connection,
  CortexEffortLevel,
  CortexExecutionProfile,
  CortexIntelligenceTier,
  Idea,
  StreamItem,
  VaultSecretPrompt,
} from '$lib/types/cortex';

export interface CortexCreateIdeaOptions {
  origin?: string | null;
  originRef?: string | null;
  displayTitle?: string | null;
}

class CortexStore {
  ideas = $state<Idea[]>([]);
  connections = $state<Connection[]>([]);
  selectedIdeaId = $state<string | null>(null);
  panelOpen = $state(false);
  stream = $state<StreamItem[]>([]);
  streamLoading = $state(false);
  loading = $state(true);
  teamMembersLoaded = $state(false);
  view = $state<'canvas' | 'list'>('canvas');
  executionProfile = $state<CortexExecutionProfile>('fast');
  intelligenceTier = $state<CortexIntelligenceTier>('high');
  effortLevel = $state<CortexEffortLevel>('high');
  constellationMode = $state(false);
  canvasOpen = $state(false);
  browserSession = $state<BrowserSessionState | null>(null);
  browserFrame = $state<BrowserFrame | null>(null);
  browserDiscovery = $state<BrowserDiscoveryResult | null>(null);
  browserExtraction = $state<BrowserExtractResult | null>(null);
  vaultSecretPrompt = $state<VaultSecretPrompt | null>(null);
  teamMembers = $state<TeamMember[]>([]);
  birthContext = $state<{ x: number; y: number } | null>(null);
  filters = $state<{ statuses: Set<string>; search: string; staleOnly?: boolean }>({
    statuses: new Set(),
    search: '',
    staleOnly: false,
  });
  typingUsers = $state<Map<string, { user_id: string; idea_id: string; timeout: ReturnType<typeof setTimeout> }>>(new Map());
  archivedIdeaCountsByUser = $state<Record<string, number>>({});
  archivedIdeas = $state<Idea[]>([]);
  archivedIdeasLoading = $state(false);

  private _wsUnsubs: (() => void)[] = [];
  private _streamVersion = 0;
  private _pendingStreamRefresh: ReturnType<typeof setTimeout> | null = null;
  private _selectedIdeaReconcile: ReturnType<typeof setInterval> | null = null;
  private _ideasSnapshotReconcile: ReturnType<typeof setInterval> | null = null;
  private _pendingBrowserSessionRefresh: ReturnType<typeof setTimeout> | null = null;
  private _seenRunUiEvents = new Set<string>();
  private _browserFocusLoads = new Set<string>();
  private _pendingBrowserEvents = new Map<string, {
    state?: BrowserSessionState | null;
    frame?: BrowserFrame | null;
  }>();
  private _vaultPromptFocusLoads = new Set<string>();
  private _pendingVaultSecretPrompts = new Map<string, VaultSecretPrompt>();
  private _teamMembersPromise: Promise<TeamMember[]> | null = null;
  private _seenIdeaRevisions = new Map<string, string>();
  private _archivedIdeaIds = new Set<string>();
  private _seenIdeaStorageKey = SEEN_IDEA_REVISIONS_STORAGE_KEY;

  private _archiveCountState(): ArchiveCountState {
    return {
      countsByUser: this.archivedIdeaCountsByUser,
      archivedIdeaIds: this._archivedIdeaIds,
    };
  }

  private _applyArchiveCountState(state: ArchiveCountState) {
    this.archivedIdeaCountsByUser = state.countsByUser;
    this._archivedIdeaIds = new Set(state.archivedIdeaIds);
  }

  constructor() {
    this._loadSeenIdeaRevisions();
    this._loadRunSettings();
  }

  private _normalizeExecutionProfile(value: unknown): CortexExecutionProfile {
    return normalizeRunSettings({ executionProfile: value }).executionProfile;
  }

  private _normalizeIntelligenceTier(value: unknown): CortexIntelligenceTier {
    return normalizeRunSettings({ intelligenceTier: value }).intelligenceTier;
  }

  private _normalizeEffortLevel(value: unknown): CortexEffortLevel {
    return normalizeRunSettings({ effortLevel: value }).effortLevel;
  }

  private _loadRunSettings() {
    if (typeof localStorage === 'undefined') return;
    const settings = loadRunSettings(localStorage);
    this.executionProfile = settings.executionProfile;
    this.intelligenceTier = settings.intelligenceTier;
    this.effortLevel = settings.effortLevel;
  }

  private _persistRunSettings() {
    if (typeof localStorage === 'undefined') return;
    persistRunSettings(localStorage, this.runSettingsOptions());
  }

  setExecutionProfile(profile: CortexExecutionProfile) {
    this.executionProfile = this._normalizeExecutionProfile(profile);
    this._persistRunSettings();
  }

  setIntelligenceTier(tier: CortexIntelligenceTier) {
    this.intelligenceTier = this._normalizeIntelligenceTier(tier);
    this._persistRunSettings();
  }

  setEffortLevel(level: CortexEffortLevel) {
    this.effortLevel = this._normalizeEffortLevel(level);
    this._persistRunSettings();
  }

  runSettingsOptions(): Pick<AgentRunOptions, 'executionProfile' | 'intelligenceTier' | 'effortLevel'> {
    return {
      executionProfile: this.executionProfile,
      intelligenceTier: this.intelligenceTier,
      effortLevel: this.effortLevel,
    };
  }

  private _ideaRevision(idea: Pick<Idea, 'updated_at' | 'created_at'> | null | undefined): string {
    return cortexIdeaRevision(idea);
  }

  private _loadSeenIdeaRevisions() {
    if (typeof localStorage === 'undefined') return;
    this._seenIdeaRevisions = parseSeenIdeaRevisions(localStorage.getItem(this._seenIdeaStorageKey));
  }

  private _persistSeenIdeaRevisions() {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(
        this._seenIdeaStorageKey,
        serializeSeenIdeaRevisions(this._seenIdeaRevisions),
      );
    } catch {
      // Best-effort only.
    }
  }

  private _markIdeaSeen(ideaId: string, revision: string) {
    const next = markCortexIdeaSeen(this._seenIdeaRevisions, ideaId, revision);
    if (next === this._seenIdeaRevisions) return;
    this._seenIdeaRevisions = next;
    this._persistSeenIdeaRevisions();
  }

  private _runWhenIdle(callback: () => void, delayMs = 0, timeout = 1500) {
    if (typeof window === 'undefined') {
      callback();
      return;
    }
    window.setTimeout(() => {
      const requestIdle = (window as typeof window & {
        requestIdleCallback?: (cb: () => void, options?: { timeout?: number }) => number;
      }).requestIdleCallback;
      if (requestIdle) {
        requestIdle(callback, { timeout });
        return;
      }
      window.setTimeout(callback, Math.min(timeout, 500));
    }, delayMs);
  }

  private _hydrateSelectedIdeaSidecars(id: string, version: number) {
    this._runWhenIdle(() => {
      if (this.selectedIdeaId !== id || this._streamVersion !== version) return;

      api.getBrowserSession(id).then((browserSession) => {
        if (this.selectedIdeaId !== id || this._streamVersion !== version) return;
        this.browserSession = browserSession;
        this.browserFrame = null;
        this.browserDiscovery = null;
        this.browserExtraction = null;
        if (browserSession?.id) {
          wsClient.send('browser_subscribe', { session_id: browserSession.id });
        }
      }).catch(() => {});

      api.markRead(id).catch(() => {});
    }, 650, 2000);
  }

  private _applyBrowserEventPayload(
    state: BrowserSessionState | null,
    frame?: BrowserFrame | null,
  ) {
    const previousSessionId = this.browserSession?.id ?? null;
    const next = applyBrowserSnapshotToState({
      session: this.browserSession,
      frame: this.browserFrame,
      discovery: this.browserDiscovery,
      extraction: this.browserExtraction,
    }, state, frame);
    this.browserSession = next.session;
    this.browserFrame = next.frame;
    this.browserDiscovery = next.discovery;
    this.browserExtraction = next.extraction;
    if (state?.id) {
      if (previousSessionId !== state.id) {
        wsClient.send('browser_subscribe', { session_id: state.id });
      }
    }
  }

  private _focusThreadForBrowserEvent(
    ideaId: string,
    state: BrowserSessionState | null,
    frame?: BrowserFrame | null,
  ) {
    const previous = this._pendingBrowserEvents.get(ideaId);
    this._pendingBrowserEvents.set(ideaId, {
      state: state ?? previous?.state ?? null,
      frame: frame ?? previous?.frame ?? null,
    });
    if (this._browserFocusLoads.has(ideaId)) return;
    this._browserFocusLoads.add(ideaId);
    void this.selectIdea(ideaId).finally(() => {
      this._browserFocusLoads.delete(ideaId);
      const pending = this._pendingBrowserEvents.get(ideaId);
      this._pendingBrowserEvents.delete(ideaId);
      if (!pending || this.selectedIdeaId !== ideaId) return;
      this._applyBrowserEventPayload(pending.state ?? null, pending.frame ?? null);
    });
  }

  private _handleBrowserSessionEvent(msg: any, frame?: BrowserFrame | null) {
    const eventType = frame ? 'browser_session_frame' : 'browser_session_state';
    const event = normalizeBrowserSessionRealtimeEvent(eventType, frame ? { ...msg, frame } : msg);
    if (event.type !== 'state' && event.type !== 'frame') return;
    if (!event.ideaId) return;

    const result = reduceBrowserSessionRealtimeEvent({
      current: {
        session: this.browserSession,
        frame: this.browserFrame,
        discovery: this.browserDiscovery,
        extraction: this.browserExtraction,
      },
      selectedIdeaId: this.selectedIdeaId,
      event,
    });

    if (result.shouldFocusThread && result.focusIdeaId) {
      this._focusThreadForBrowserEvent(result.focusIdeaId, event.session, event.type === 'frame' ? event.frame : null);
      return;
    }

    if (event.ideaId === this.selectedIdeaId) {
      this.browserSession = result.state.session;
      this.browserFrame = result.state.frame;
      this.browserDiscovery = result.state.discovery;
      this.browserExtraction = result.state.extraction;
      if (result.shouldSubscribeSessionId) {
        wsClient.send('browser_subscribe', { session_id: result.shouldSubscribeSessionId });
      }
    }
  }

  private _normalizeVaultSecretPrompt(msg: any): VaultSecretPrompt | null {
    const source = msg?.prompt && typeof msg.prompt === 'object' ? msg.prompt : msg;
    const keyName = typeof source?.key_name === 'string' ? source.key_name.trim() : '';
    const ideaId = typeof msg?.idea_id === 'string'
      ? msg.idea_id
      : typeof source?.idea_id === 'string'
        ? source.idea_id
        : null;
    if (!keyName || !ideaId) return null;
    return {
      id: String(source?.id || `vault-secret-${ideaId}-${keyName}`),
      idea_id: ideaId,
      key_name: keyName,
      description: typeof source?.description === 'string' ? source.description : msg?.description ?? null,
      category: typeof source?.category === 'string' ? source.category : msg?.category ?? 'api',
      reason: typeof source?.reason === 'string' ? source.reason : msg?.reason ?? null,
      requested_by: typeof source?.requested_by === 'string' ? source.requested_by : msg?.requested_by ?? null,
      created_at: typeof source?.created_at === 'string' ? source.created_at : msg?.created_at ?? null,
    };
  }

  private _applyVaultSecretPrompt(prompt: VaultSecretPrompt) {
    this.vaultSecretPrompt = prompt;
    this.panelOpen = true;
  }

  private _focusThreadForVaultSecretPrompt(ideaId: string, prompt: VaultSecretPrompt) {
    this._pendingVaultSecretPrompts.set(ideaId, prompt);
    if (this._vaultPromptFocusLoads.has(ideaId)) return;
    this._vaultPromptFocusLoads.add(ideaId);
    void this.selectIdea(ideaId).finally(() => {
      this._vaultPromptFocusLoads.delete(ideaId);
      const pending = this._pendingVaultSecretPrompts.get(ideaId);
      this._pendingVaultSecretPrompts.delete(ideaId);
      if (!pending || this.selectedIdeaId !== ideaId) return;
      this._applyVaultSecretPrompt(pending);
    });
  }

  _handleVaultSecretPrompt(msg: any) {
    const prompt = this._normalizeVaultSecretPrompt(msg);
    const ideaId = prompt?.idea_id ?? null;
    if (!prompt || !ideaId) return;

    if (ideaId !== this.selectedIdeaId) {
      this._focusThreadForVaultSecretPrompt(ideaId, prompt);
      return;
    }

    this._applyVaultSecretPrompt(prompt);
  }

  clearVaultSecretPrompt(promptId?: string | null) {
    if (!promptId || this.vaultSecretPrompt?.id === promptId) {
      this.vaultSecretPrompt = null;
    }
  }

  private _scheduleSelectedBrowserSessionRefresh(delayMs = 450) {
    if (!this.selectedIdeaId || this.browserSession?.id || this._pendingBrowserSessionRefresh) return;
    const ideaId = this.selectedIdeaId;
    const version = this._streamVersion;
    this._pendingBrowserSessionRefresh = setTimeout(() => {
      this._pendingBrowserSessionRefresh = null;
      if (this.selectedIdeaId !== ideaId || this._streamVersion !== version || this.browserSession?.id) {
        return;
      }
      api.getBrowserSession(ideaId).then((browserSession) => {
        if (
          this.selectedIdeaId !== ideaId
          || this._streamVersion !== version
          || !browserSession?.id
          || this.browserSession?.id
        ) {
          return;
        }
        this._applyBrowserEventPayload(browserSession as BrowserSessionState);
      }).catch(() => {});
    }, delayMs);
  }

  private _isIdeaSeen(idea: Pick<Idea, 'id' | 'updated_at' | 'created_at'>): boolean {
    return isCortexIdeaSeen(idea, this._seenIdeaRevisions);
  }

  private _hasWorkingIdeas(): boolean {
    return hasWorkingCortexIdeas(this.ideas, { isLocalPreviewIdeaId: (id) => this._isLocalPreviewIdeaId(id) });
  }

  private async _refreshIdeasSnapshot() {
    try {
      const freshIdeas = await api.listIdeas();
      this._seedArchivedIdeaCounts(freshIdeas);
      const prevById = new Map(this.ideas.map((idea) => [idea.id, idea] as const));
      this.ideas = freshIdeas.map((idea) => {
        const prev = prevById.get(idea.id);
        const normalized = this._normalizeIdea({ ...(prev || {}), ...idea } as Idea);
        return prev ? { ...prev, ...normalized } : normalized;
      });
      if (this._hasWorkingIdeas()) this._ensureIdeasSnapshotReconcile();
      else this._stopIdeasSnapshotReconcile();
    } catch {
      // Best-effort fallback for missed websocket state.
    }
  }

  private _ensureIdeasSnapshotReconcile() {
    if (this._ideasSnapshotReconcile) return;
    this._ideasSnapshotReconcile = setInterval(() => {
      if (!this._hasWorkingIdeas()) {
        this._stopIdeasSnapshotReconcile();
        return;
      }
      this._refreshIdeasSnapshot();
    }, 1500);
  }

  private _stopIdeasSnapshotReconcile() {
    if (this._ideasSnapshotReconcile) {
      clearInterval(this._ideasSnapshotReconcile);
      this._ideasSnapshotReconcile = null;
    }
  }

  private _normalizeIdeaStatus(status: string | null | undefined): string {
    return normalizeCortexIdeaStatus(status);
  }

  private _normalizeIdea(idea: Idea): Idea {
    return normalizeCortexIdea(idea, { isIdeaSeen: (candidate) => this._isIdeaSeen(candidate) });
  }

  private _currentUserTeamMember(): TeamMember | null {
    const user = auth.user;
    if (!user?.id) return null;
    return {
      id: String(user.id),
      name: user.name || user.email || 'You',
      email: user.email,
      color: user.color || '#6d46d9',
    };
  }

  private _normalizeTeamMembers(members: any[] | null | undefined): TeamMember[] {
    const normalized = (members || [])
      .filter((member: any) => member?.id != null && member?.approved !== false)
      .map((member: any) => ({
        id: String(member.id),
        name: member.name || member.email || 'Unknown',
        email: member.email,
        color: member.color || member.cortex_color || '',
      }))
      .map((member) => member as TeamMember);

    const currentUser = this._currentUserTeamMember();
    if (!currentUser) return normalized;
    if (normalized.some((member) => member.id === currentUser.id)) return normalized;
    return [currentUser, ...normalized];
  }

  private _setIdeaPatch(id: string, patch: Partial<Idea>) {
    this.ideas = patchIdeaById(this.ideas, id, patch);
  }

  private _upsertIdea(idea: Idea) {
    const normalized = this._normalizeIdea(idea);
    if (normalized.archived_at) this._registerArchivedIdea(normalized);
    else this._unregisterArchivedIdea(normalized);
    const existing = this.ideas.find((entry) => entry.id === normalized.id);
    if (!existing) {
      this.ideas = [...this.ideas, normalized];
      return;
    }

    this.ideas = this.ideas.map((entry) =>
      entry.id === normalized.id ? { ...entry, ...normalized } : entry,
    );
  }

  private _isActiveRunItem(item: StreamItem): boolean {
    return item.type === 'run' && isActiveRun(item);
  }

  private _hasActiveRuns(items: StreamItem[]): boolean {
    return items.some((item) => this._isActiveRunItem(item));
  }

  private _isLocalPreviewIdeaId(id: unknown): boolean {
    return browser && dev && isLocalPreviewIdeaId(id);
  }

  private _reconcileIdeaStatusFromStream(ideaId: string, items: StreamItem[]) {
    const runs = items.filter((item) => item.type === 'run');
    if (runs.length === 0) return;

    const activeRun = runs.find((item) => this._isActiveRunItem(item));
    const latestRun = activeRun ?? [...runs].reverse().find((item) => item.status);
    if (!latestRun?.status) return;

    let nextStatus = activeRun ? 'working' : this._normalizeIdeaStatus(latestRun.status);
    if (ideaId === this.selectedIdeaId && nextStatus === 'done' && !this._isLocalPreviewIdeaId(ideaId)) {
      const current = this.ideas.find((i) => i.id === ideaId);
      this._markIdeaSeen(ideaId, this._ideaRevision(current));
      nextStatus = 'idle';
    }

    const current = this.ideas.find((i) => i.id === ideaId);
    if (!current) return;
    const normalizedCurrent = this._normalizeIdeaStatus(current.status);
    const patch: Partial<Idea> = { status: nextStatus };
    if (!activeRun && nextStatus !== 'working') {
      patch.active_agents = 0;
      patch._agents = 0;
    }
    if (normalizedCurrent !== nextStatus) {
      this._setIdeaPatch(ideaId, patch);
    } else if (!activeRun && nextStatus !== 'working' && (current.active_agents || current._agents)) {
      this._setIdeaPatch(ideaId, patch);
    }
  }

  private _mergeLiveStreamState(items: StreamItem[], ideaId: string | null = this.selectedIdeaId): StreamItem[] {
    return mergeLiveStreamState(
      items as CortexRunStreamItem[],
      this.stream as CortexRunStreamItem[],
      ideaId,
      (item) => this._isActiveRunItem(item as StreamItem),
      mergeRunProgressSnapshot,
    ) as StreamItem[];
  }

  private _selectedIdeaLooksWorking(id: string): boolean {
    const selected = this.ideas.find((idea) => idea.id === id);
    return this._normalizeIdeaStatus(selected?.status) === 'working';
  }

  private _handleAgentTextDelta(msg: any) {
    this.stream = applyAgentTextDeltaToStream(
      this.stream as CortexRunStreamItem[],
      msg,
      this.selectedIdeaId,
    ) as StreamItem[];
  }

  private _handleAgentActivity(msg: any) {
    this.stream = applyAgentActivityToStream(
      this.stream as CortexRunStreamItem[],
      msg,
      this.selectedIdeaId,
      this.executionProfile,
    ) as StreamItem[];
  }

  private _handleRunCompleted(msg: any) {
    this.stream = applyRunCompletedToStream(
      this.stream as CortexRunStreamItem[],
      msg,
      this.selectedIdeaId,
    ) as StreamItem[];
  }

  private _isRootRunUiEvent(msg: any): boolean {
    const runId = msg?.run_id;
    const rootRunId = msg?.root_run_id;
    if (runId == null || runId === '' || rootRunId == null || rootRunId === '') return true;
    return String(runId) === String(rootRunId);
  }

  private _markIdeaWorkingForRootRun(ideaId: string) {
    const current = this.ideas.find((i) => i.id === ideaId);
    if (current && ['archived', 'resolved'].includes(String(current.status || ''))) return;
    const activeAgents = Math.max(1, Number(current?.active_agents || current?._agents || 0) || 0);
    this._setIdeaPatch(ideaId, { status: 'working', active_agents: activeAgents, _agents: activeAgents });
    this._ensureIdeasSnapshotReconcile();
  }

  private _handleRunUiEvent(msg: any) {
    if (!msg?.idea_id) return;
    const isRootRunEvent = this._isRootRunUiEvent(msg);
    if (msg.type === 'run_started' && isRootRunEvent) {
      this._markIdeaWorkingForRootRun(msg.idea_id);
    }
    if (msg.idea_id !== this.selectedIdeaId || !isRootRunEvent) return;
    const eventKey = runUiEventKey(msg);
    if (eventKey) {
      if (this._seenRunUiEvents.has(eventKey)) return;
      this._seenRunUiEvents.add(eventKey);
    }
    const toolName = typeof msg.tool_name === 'string' ? msg.tool_name.trim() : '';
    const label = typeof msg.label === 'string' ? msg.label.trim() : '';

    if (msg.type === 'text_delta') {
      this._handleAgentTextDelta(msg);
      return;
    }

    if (msg.type === 'run_started') {
      this._handleAgentActivity({ ...msg, activity: label || 'Started' });
      this._ensureSelectedIdeaReconcile();
      return;
    }

    if (msg.type === 'step_started') {
      this._handleAgentActivity({ ...msg, activity: label || msg.activity || 'Working' });
      this._ensureSelectedIdeaReconcile();
      this._scheduleSelectedBrowserSessionRefresh();
      return;
    }

    if (msg.type === 'tool_started') {
      this._handleAgentActivity({ ...msg, activity: toolName ? `Using ${toolName}` : 'Using a tool' });
      this._ensureSelectedIdeaReconcile();
      this._scheduleSelectedBrowserSessionRefresh();
      return;
    }

    if (msg.type === 'tool_finished') {
      const status = msg.status === 'failed' ? 'failed' : 'completed';
      this._handleAgentActivity({
        ...msg,
        activity: toolName ? `${toolName} ${status}` : `Tool ${status}`,
      });
      this._ensureSelectedIdeaReconcile();
      this._scheduleSelectedBrowserSessionRefresh();
      return;
    }

    if (msg.type === 'run_completed') {
      this._handleRunCompleted(msg);
      this._reconcileIdeaStatusFromStream(msg.idea_id, this.stream);
      if (this._hasActiveRuns(this.stream)) this._ensureSelectedIdeaReconcile();
      else this._stopSelectedIdeaReconcile();
      this._scheduleSelectedStreamRefresh(120);
    }
  }

  private _registerArchivedIdea(idea: Pick<Idea, 'id' | 'user_id'> | null | undefined) {
    this._applyArchiveCountState(registerArchivedIdeaInState(this._archiveCountState(), idea as ArchiveIdeaIdentity | null | undefined));
  }

  private _unregisterArchivedIdea(idea: Pick<Idea, 'id' | 'user_id'> | null | undefined) {
    this._applyArchiveCountState(unregisterArchivedIdeaInState(this._archiveCountState(), idea as ArchiveIdeaIdentity | null | undefined));
  }

  private _rememberArchivedIdea(idea: Idea | null | undefined) {
    this.archivedIdeas = rememberArchivedIdeaInList(this.archivedIdeas, idea, { archivedAt: idea?.archived_at || undefined });
  }

  private _seedArchivedIdeaCounts(ideas: Array<Partial<Idea> & { id?: string; user_id?: string; archived_at?: string | null }>) {
    this._applyArchiveCountState(seedArchivedIdeaCountsInState(this._archiveCountState(), ideas));
  }

  archivedIdeaCountForUser(userId: string | null | undefined): number {
    return selectArchivedIdeaCountForUser(this.archivedIdeaCountsByUser, userId);
  }

  async loadArchivedIdeas(limit = 12) {
    this.archivedIdeasLoading = true;
    try {
      const ideas = await api.listArchivedIdeas(limit);
      this.archivedIdeas = ideas.slice(0, limit);
      this._seedArchivedIdeaCounts(ideas);
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load archived threads', 'error');
    } finally {
      this.archivedIdeasLoading = false;
    }
  }

  applyUserProfileUpdate(update: {
    userId: string;
    name?: string | null;
    color?: string | null;
    previousName?: string | null;
    previousColor?: string | null;
  }) {
    const userId = String(update.userId);
    const nextName = (update.name ?? '').trim();
    const previousName = (update.previousName ?? '').trim();
    const nextColor = normalizeHexColor(update.color);
    const previousColor = normalizeHexColor(update.previousColor);
    const nextMemberColor = nextColor ?? update.color ?? undefined;

    this.teamMembers = this.teamMembers.map((member: any) =>
      String(member?.id) === userId
        ? {
            ...member,
            ...(nextName ? { name: nextName } : {}),
            ...(nextMemberColor
              ? { color: nextMemberColor, cortex_color: nextMemberColor }
              : {}),
          }
        : member,
    );

    const matchesPreviousDisplayAuthor = (
      name?: string | null,
      color?: string | null,
      allowBlank = false,
    ) => {
      const normalizedName = (name ?? '').trim().toLowerCase();
      const normalizedColor = normalizeHexColor(color)?.toLowerCase();
      if (previousColor && normalizedColor === previousColor.toLowerCase()) return true;
      if (previousName && normalizedName === previousName.toLowerCase()) return true;
      if (nextName && normalizedName === nextName.toLowerCase()) return true;
      return allowBlank && !normalizedName && !normalizedColor;
    };

    this.ideas = this.ideas.map((idea) => {
      if (String(idea.user_id) !== userId) return idea;
      if (!matchesPreviousDisplayAuthor(idea.author_name, idea.author_color, true)) return idea;

      return {
        ...idea,
        ...(nextName ? { author_name: nextName } : {}),
        ...(nextColor ? { author_color: nextColor } : {}),
      };
    });

    this.stream = this.stream.map((item) => {
      if (item.type !== 'message') return item;
      const itemUserId = item.user_id ? String(item.user_id) : '';
      const matchesUserId = itemUserId === userId;
      const matchesDisplayAuthor = matchesPreviousDisplayAuthor(
        item.user_name,
        item.user_color ?? item.author_color,
      );
      if (!matchesUserId && !matchesDisplayAuthor) return item;

      return {
        ...item,
        ...(nextName ? { user_name: nextName } : {}),
        ...(nextColor ? { user_color: nextColor, author_color: nextColor } : {}),
      };
    });
  }

  get selectedIdea(): Idea | undefined {
    return this.ideas.find((i) => i.id === this.selectedIdeaId);
  }

  typingInIdea(ideaId: string): string[] {
    return [...this.typingUsers.values()]
      .filter((t) => t.idea_id === ideaId)
      .map((t) => t.user_id);
  }

  get filteredIdeas(): Idea[] {
    let result = this.ideas;
    if (this.filters.statuses.size > 0) {
      result = result.filter((i) => this.filters.statuses.has(i.status));
    }
    if (this.filters.search) {
      const q = this.filters.search.toLowerCase();
      result = result.filter(
        (i) =>
          i.title.toLowerCase().includes(q) ||
          (i.description || '').toLowerCase().includes(q),
      );
    }
    if (this.filters.staleOnly) {
      const cutoff = Date.now() - 14 * 24 * 60 * 60 * 1000;
      result = result.filter((i) => new Date(i.updated_at || i.created_at).getTime() < cutoff);
    }
    return result;
  }

  private _initialLoadDone = false;
  private _loadPromise: Promise<void> | null = null;

  private async _loadCorePayload(shouldLoadTeamMembers: boolean) {
    try {
      const include = shouldLoadTeamMembers
        ? ['ideas', 'connections', 'team_members']
        : ['ideas', 'connections'];
      const bootstrap = await api.cortexBootstrap({ include });
      if (Array.isArray(bootstrap?.ideas) && Array.isArray(bootstrap?.connections)) {
        return {
          ideas: bootstrap.ideas,
          connections: bootstrap.connections,
          teamMembers: Array.isArray(bootstrap.team_members) ? bootstrap.team_members : null,
        };
      }
    } catch {
      // Older API builds do not have /api/cortex/bootstrap; fall through to the
      // established endpoint fanout so local previews and mixed deploys survive.
    }

    const [ideas, connections, teamMembers] = await Promise.all([
      api.listIdeas(),
      api.listConnections(),
      shouldLoadTeamMembers ? this.loadTeamMembers() : Promise.resolve(null),
    ]);
    return { ideas, connections, teamMembers };
  }

  async loadTeamMembers(options: { force?: boolean } = {}): Promise<TeamMember[]> {
    if (!options.force && this.teamMembersLoaded) return this.teamMembers;
    if (!options.force && this._teamMembersPromise) return this._teamMembersPromise;

    const promise = api.listTeamMembers()
      .then((members) => {
        this.teamMembers = this._normalizeTeamMembers(members);
        this.teamMembersLoaded = true;
        return this.teamMembers;
      })
      .catch(() => {
        const currentUserId = auth.user?.id;
        const canReuseExistingMembers = currentUserId
          ? this.teamMembers.some((member) => member.id === currentUserId)
          : false;
        this.teamMembers = this._normalizeTeamMembers(canReuseExistingMembers ? this.teamMembers : []);
        this.teamMembersLoaded = true;
        return this.teamMembers;
      })
      .finally(() => {
        if (this._teamMembersPromise === promise) {
          this._teamMembersPromise = null;
        }
      });

    this._teamMembersPromise = promise;
    return promise;
  }

  async load(_options: { loadTeamMembers?: boolean } = {}) {
    if (this._loadPromise) return this._loadPromise;
    const promise = this._load(_options).finally(() => {
      if (this._loadPromise === promise) this._loadPromise = null;
    });
    this._loadPromise = promise;
    return promise;
  }

  hydrateCorePayload(
    payload: Pick<CortexBootstrapPayload, 'ideas' | 'connections' | 'team_members'> | null | undefined,
    options: { loadTeamMembers?: boolean } = {},
  ): boolean {
    if (!Array.isArray(payload?.ideas) || !Array.isArray(payload?.connections)) return false;
    const shouldLoadTeamMembers = options.loadTeamMembers !== false;
    this.ideas = payload.ideas.map((idea) => this._normalizeIdea(idea));
    this._seedArchivedIdeaCounts(payload.ideas);
    this.connections = payload.connections;
    if (shouldLoadTeamMembers) {
      this.teamMembers = this._normalizeTeamMembers(
        Array.isArray(payload.team_members) ? payload.team_members : [],
      );
      this.teamMembersLoaded = true;
    } else if (!this.teamMembersLoaded) {
      this.teamMembers = this._normalizeTeamMembers(this.teamMembers);
      this.teamMembersLoaded = true;
    }
    if (this._hasWorkingIdeas()) this._ensureIdeasSnapshotReconcile();
    else this._stopIdeasSnapshotReconcile();
    return true;
  }

  async loadWorkspaceBootstrap(): Promise<CortexBootstrapPayload | null> {
    if (!this._initialLoadDone) this.loading = true;
    try {
      const bootstrap = await api.cortexBootstrap({ include: ['core', 'workspace'] });
      if (!this.hydrateCorePayload(bootstrap, { loadTeamMembers: true })) {
        throw new Error('Incomplete cortex workspace bootstrap');
      }
      return bootstrap;
    } catch {
      await this._load({ loadTeamMembers: true });
      return null;
    } finally {
      this.loading = false;
      this._initialLoadDone = true;
    }
  }

  private async _load(_options: { loadTeamMembers?: boolean } = {}) {
    // Only show loading spinner on first load — subsequent loads (WS reconnect) are silent
    if (!this._initialLoadDone) this.loading = true;
    const shouldLoadTeamMembers = _options.loadTeamMembers !== false;
    try {
      const { ideas, connections, teamMembers } = await this._loadCorePayload(shouldLoadTeamMembers);
      this.hydrateCorePayload(
        { ideas, connections, team_members: teamMembers },
        { loadTeamMembers: shouldLoadTeamMembers },
      );
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load cortex', 'error');
      if (!this.teamMembersLoaded && shouldLoadTeamMembers) {
        this.teamMembers = this._normalizeTeamMembers(this.teamMembers);
        this.teamMembersLoaded = true;
      }
    } finally {
      this.loading = false;
      this._initialLoadDone = true;
    }
  }

  private _applyLoadedStream(id: string, version: number, items: StreamItem[]) {
    if (this.selectedIdeaId !== id || this._streamVersion !== version) return false;
    const mergedItems = this._mergeLiveStreamState(items, id);
    this.stream = mergedItems;
    this._reconcileIdeaStatusFromStream(id, mergedItems);
    this.browserFrame = null;
    this.browserDiscovery = null;
    this.browserExtraction = null;
    const selected = this.ideas.find((i) => i.id === id);
    if (selected?.status === 'done' && !this._isLocalPreviewIdeaId(id)) {
      this._markIdeaSeen(id, this._ideaRevision(selected));
      this._setIdeaPatch(id, { status: 'idle' });
    }
    if (this._hasActiveRuns(mergedItems) || this._selectedIdeaLooksWorking(id)) {
      this._ensureSelectedIdeaReconcile();
    } else {
      this._stopSelectedIdeaReconcile();
    }
    if (this._hasWorkingIdeas()) this._ensureIdeasSnapshotReconcile();
    else this._stopIdeasSnapshotReconcile();
    this.streamLoading = false;

    this._hydrateSelectedIdeaSidecars(id, version);
    return true;
  }

  async loadDirectThread(id: string) {
    if (this._isLocalPreviewIdeaId(id)) {
      await this.selectIdea(id);
      return;
    }

    if (!this._initialLoadDone) this.loading = true;
    if (this.browserSession?.id && this.browserSession.idea_id !== id) {
      wsClient.send('browser_unsubscribe', { session_id: this.browserSession.id });
      this.browserSession = null;
      this.browserFrame = null;
      this.browserDiscovery = null;
      this.browserExtraction = null;
    }
    const version = ++this._streamVersion;
    this._seenRunUiEvents.clear();
    if (this.vaultSecretPrompt?.idea_id && this.vaultSecretPrompt.idea_id !== id) {
      this.vaultSecretPrompt = null;
    }
    this.selectedIdeaId = id;
    this.panelOpen = true;
    this.canvasOpen = false;
    this.streamLoading = true;
    this.stream = [];

    try {
      const bootstrap = await api.cortexBootstrap({
        include: ['selected_idea', 'direct_thread'],
        ideaId: id,
      });
      if (
        !bootstrap?.selected_idea
        || !Array.isArray(bootstrap?.direct_thread?.stream)
      ) {
        throw new Error('Incomplete cortex direct-thread bootstrap');
      }
      const selectedIdea = this._normalizeIdea(bootstrap.selected_idea);
      const remainingIdeas = this.ideas.filter((idea) => idea.id !== selectedIdea.id);
      this.ideas = [selectedIdea, ...remainingIdeas];
      if (!this.teamMembersLoaded) {
        this.teamMembers = this._normalizeTeamMembers(this.teamMembers);
        this.teamMembersLoaded = true;
      }
      this._applyLoadedStream(id, version, bootstrap.direct_thread.stream);
    } catch {
      await this._load({ loadTeamMembers: false });
      await this.selectIdea(id);
    } finally {
      this.loading = false;
      this._initialLoadDone = true;
      if (this.selectedIdeaId === id && this._streamVersion === version) {
        this.streamLoading = false;
      }
    }
  }

  async selectIdea(id: string | null) {
    if (id === null) {
      this._streamVersion += 1;
      if (this.browserSession?.id) {
        wsClient.send('browser_unsubscribe', { session_id: this.browserSession.id });
      }
      if (this._pendingBrowserSessionRefresh) {
        clearTimeout(this._pendingBrowserSessionRefresh);
        this._pendingBrowserSessionRefresh = null;
      }
      if (this._pendingStreamRefresh) {
        clearTimeout(this._pendingStreamRefresh);
        this._pendingStreamRefresh = null;
      }
      this._stopSelectedIdeaReconcile();
      this._seenRunUiEvents.clear();
      this.selectedIdeaId = null;
      this.panelOpen = false;
      this.canvasOpen = false;
      this.streamLoading = false;
      this.browserSession = null;
      this.browserFrame = null;
      this.browserDiscovery = null;
      this.browserExtraction = null;
      this.vaultSecretPrompt = null;
      this.stream = [];
      return;
    }
    if (this.browserSession?.id && this.browserSession.idea_id !== id) {
      wsClient.send('browser_unsubscribe', { session_id: this.browserSession.id });
      this.browserSession = null;
      this.browserFrame = null;
      this.browserDiscovery = null;
      this.browserExtraction = null;
    }
    const version = ++this._streamVersion;
    this._seenRunUiEvents.clear();
    if (this.vaultSecretPrompt?.idea_id && this.vaultSecretPrompt.idea_id !== id) {
      this.vaultSecretPrompt = null;
    }
    this.selectedIdeaId = id;
    this.panelOpen = true;
    this.canvasOpen = false;
    this.streamLoading = true;
    this.stream = [];

    if (this._isLocalPreviewIdeaId(id)) {
      const idea = this.ideas.find((candidate) => candidate.id === id);
      if (idea) {
        this._applyLoadedStream(
          id,
          version,
          buildLocalPreviewThreadStream(idea, this.teamMembers) as StreamItem[],
        );
      } else {
        this.streamLoading = false;
      }
      return;
    }

    try {
      const items = await api.unifiedStream(id);
      // Guard: user may have switched ideas while we were fetching
      this._applyLoadedStream(id, version, items);
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load thread', 'error');
    } finally {
      if (this.selectedIdeaId === id && this._streamVersion === version) {
        this.streamLoading = false;
      }
    }
  }

  /** Whether a message should trigger agent run. In Cortex, any user text is addressed to Illo. */
  private _runDecision(content: string) {
    return getRunDecision(content);
  }

  async ensureBrowserSession(
    url?: string,
    options: {
      storage_mode?: 'ephemeral' | 'idea';
      allow_downloads?: boolean;
      allow_file_uploads?: boolean;
    } = {},
  ) {
    if (!this.selectedIdeaId) return null;
    try {
      const session = await api.createBrowserSession(this.selectedIdeaId, { url, ...options });
      this.browserSession = session;
      const empty = emptyBrowserSessionViewState<
        BrowserSessionState,
        BrowserFrame,
        BrowserDiscoveryResult,
        BrowserExtractResult
      >();
      this.browserFrame = empty.frame;
      this.browserDiscovery = empty.discovery;
      this.browserExtraction = empty.extraction;
      wsClient.send('browser_subscribe', { session_id: session.id });
      return session;
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to start browser session', 'error');
      return null;
    }
  }

  private _sendBrowser(event: string, data: Record<string, unknown> = {}) {
    const payload = browserCommandPayload(this.browserSession?.id, data);
    if (!payload) return;
    wsClient.send(event, payload);
  }

  browserNavigate(url: string) {
    if (!url.trim()) return;
    this._sendBrowser('browser_navigate', { url });
  }

  browserClick(x: number, y: number) {
    this._sendBrowser('browser_click', { x, y });
  }

  browserScroll(deltaX: number, deltaY: number) {
    this._sendBrowser('browser_scroll', { delta_x: deltaX, delta_y: deltaY });
  }

  browserClickSelector(selector: string) {
    if (!selector.trim()) return;
    this._sendBrowser('browser_click', { selector });
  }

  browserType(text: string, pressEnter = false) {
    if (!text) return;
    this._sendBrowser('browser_type', { text, press_enter: pressEnter });
  }

  browserKey(key: string) {
    this._sendBrowser('browser_key', { key });
  }

  browserRefresh() {
    this._sendBrowser('browser_refresh');
  }

  browserBack() {
    this._sendBrowser('browser_back');
  }

  browserForward() {
    this._sendBrowser('browser_forward');
  }

  browserNewTab(url?: string) {
    this._sendBrowser('browser_new_tab', url ? { url } : {});
  }

  browserSwitchTab(index: number) {
    this._sendBrowser('browser_switch_tab', { index });
  }

  browserCloseTab(index?: number) {
    this._sendBrowser('browser_close_tab', index == null ? {} : { index });
  }

  browserUploadAttachment(selector: string, attachmentUrl: string) {
    if (!selector || !attachmentUrl) return;
    this._sendBrowser('browser_upload_attachment', { selector, attachment_url: attachmentUrl });
  }

  browserDiscover(selector = "a,button,input,textarea,select,[role='button']", maxResults = 40) {
    this._sendBrowser('browser_discover', { selector, max_results: maxResults });
  }

  browserExtract(selector?: string, mode = 'text', maxChars = 6000) {
    this._sendBrowser('browser_extract', { selector, mode, max_chars: maxChars });
  }

  browserSnapshot(persist = false, title?: string) {
    this._sendBrowser('browser_snapshot', { persist, title });
  }

  browserSaveScreenshot(fullPage = true) {
    this._sendBrowser('browser_save_screenshot', { full_page: fullPage });
  }

  browserPrintPdf(landscape = false) {
    this._sendBrowser('browser_print_pdf', { landscape });
  }

  async browserClose() {
    if (!this.browserSession?.id) return;
    const sessionId = this.browserSession.id;
    try {
      wsClient.send('browser_unsubscribe', { session_id: sessionId });
      await api.closeBrowserSession(sessionId);
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to close browser session', 'error');
    } finally {
      const empty = emptyBrowserSessionViewState<
        BrowserSessionState,
        BrowserFrame,
        BrowserDiscoveryResult,
        BrowserExtractResult
      >();
      this.browserSession = empty.session;
      this.browserFrame = empty.frame;
      this.browserDiscovery = empty.discovery;
      this.browserExtraction = empty.extraction;
    }
  }

  private _shouldRun(content: string): boolean {
    return this._runDecision(content).shouldRun;
  }

  private _normalizeRunOptions(options: AgentRunOptions = {}): AgentRunOptions {
    return normalizeCortexRunOptions(options, this.runSettingsOptions());
  }

  setBirthContext(position: { x: number; y: number } | null) {
    this.birthContext = position;
  }

  /**
   * Post a message to an idea's thread and optionally run.
   * Used by both createIdea (first message) and sendMessage (replies).
   */
  private async _postAndMaybeRun(
    ideaId: string,
    content: string,
    event: string,
    attachments: any[] = [],
    runContent = content,
    options: AgentRunOptions = {},
  ) {
    const runOptions = this._normalizeRunOptions(options);
    const projectContextAttachment = attachments.find((att: any) => att?.type === 'project_context' || att?.project_context);
    const projectContext = projectContextAttachment?.project_context;
    const messageMetadata: Record<string, any> = {
      execution_profile: runOptions.executionProfile,
      intelligence: runOptions.intelligenceTier,
      effort: runOptions.effortLevel,
      ...(runOptions.metadata || {}),
    };
    if (projectContext) messageMetadata.project_context = projectContext;
    const threadMessage = await api.addThreadMessage(ideaId, {
      content,
      attachments: attachments.length ? attachments : undefined,
      metadata: Object.keys(messageMetadata).length ? messageMetadata : undefined,
    });
    const decision = this._runDecision(runContent);
    if (!runOptions.skipRun && decision.shouldRun) {
      await api.updateIdeaStatus(ideaId, 'queued');
      const idea = this.ideas.find((i) => i.id === ideaId);
      if (idea) idea.status = 'working';
      this._ensureIdeasSnapshotReconcile();
      const runMetadata: Record<string, any> = {
        ...(decision.isExplicit ? {} : { background_activation: decision.reason }),
        execution_profile: runOptions.executionProfile,
        model_tier: runOptions.intelligenceTier,
        intelligence: runOptions.intelligenceTier,
        thinking_tier: runOptions.effortLevel,
        effort: runOptions.effortLevel,
        thread_message_id: threadMessage?.id,
        ...(runOptions.metadata || {}),
      };
      if (projectContext) runMetadata.project_context = projectContext;
      await api.notifyCortex({
        event,
        idea_id: ideaId,
        thread_message: runContent,
        metadata: Object.keys(runMetadata).length ? runMetadata : undefined,
      }).catch((e) => console.warn('[cortex] run notify failed', e));
      if (ideaId === this.selectedIdeaId) {
        this._ensureSelectedIdeaReconcile();
        this._scheduleSelectedStreamRefresh(120);
      }
    }
  }

  async createIdea(
    title: string,
    description?: string,
    attachments: any[] = [],
    runContent = title,
    options: AgentRunOptions = {},
    createOptions: CortexCreateIdeaOptions = {},
  ) {
    const origin = this.birthContext;
    return this.createIdeaAt(
      title,
      origin?.x ?? 0,
      origin?.y ?? 0,
      description,
      attachments,
      runContent,
      options,
      createOptions,
    );
  }

  async createIdeaAt(
    title: string,
    x: number,
    y: number,
    description?: string,
    attachments: any[] = [],
    runContent = title,
    options: AgentRunOptions = {},
    createOptions: CortexCreateIdeaOptions = {},
  ) {
    try {
      const ideaInput: Record<string, any> = { title, description };
      if (createOptions.origin) ideaInput.origin = createOptions.origin;
      if (createOptions.originRef) ideaInput.origin_ref = createOptions.originRef;
      const idea = await api.createIdea(ideaInput);
      bloop();
      if (createOptions.displayTitle) {
        idea.display_title = createOptions.displayTitle;
        api.updateIdea(idea.id, { display_title: createOptions.displayTitle }).catch(() => {});
      } else {
        // Fire-and-forget: generate a concise display title via local LLM
        api.generateTitle(title).then((r) => {
          if (r?.title) {
            api.updateIdea(idea.id, { display_title: r.title });
            // Update local state so UI reflects the generated title immediately
            this.ideas = this.ideas.map((i) =>
              i.id === idea.id ? { ...i, display_title: r.title } : i,
            );
          }
        }).catch(() => {});
      }
      // The typed text becomes the first thread message and queues Illo by default.
      // Set status optimistically BEFORE adding to ideas array so the
      // SVG birth animation renders with the correct color immediately.
      const originalStatus = this._normalizeIdeaStatus(idea.status);
      if (this._shouldRun(runContent)) {
        idea.status = 'working';
      } else {
        idea.status = 'idle';
      }
      const normalizedIdea = this._normalizeIdea(idea);
      this._upsertIdea({
        ...normalizedIdea,
        position_x: x,
        position_y: y,
      });
      try {
        await this._postAndMaybeRun(
          idea.id,
          title,
          'idea_created',
          attachments,
          runContent,
          options,
        );
      } catch {
        // Revert optimistic status on run failure
        idea.status = originalStatus;
        this.ideas = this.ideas.map((i) =>
          i.id === idea.id ? { ...i, status: originalStatus } : i,
        );
      }
      return idea;
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to create idea', 'error');
    }
  }

  async sendMessage(content: string, attachments: any[] = [], options: AgentRunOptions = {}) {
    if (!this.selectedIdeaId || !content.trim()) return;
    try {
      await this._postAndMaybeRun(this.selectedIdeaId, content, 'thread_reply', attachments, content, options);
      // Refresh stream to show the new message
      await this._refreshSelectedStream();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to send message', 'error');
      throw err;
    }
  }

  async updateIdeaStatus(id: string, status: string) {
    try {
      const updated = await api.updateIdeaStatus(id, status);
      this.ideas = this.ideas.map((i) => (i.id === id ? this._normalizeIdea({ ...i, ...updated }) : i));
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to update status', 'error');
    }
  }

  async deleteIdea(id: string) {
    try {
      const existing = this.ideas.find((i) => i.id === id);
      await api.deleteIdea(id);
      this._registerArchivedIdea(existing);
      this._rememberArchivedIdea(existing);
      this.ideas = this.ideas.filter((i) => i.id !== id);
      if (!this._hasWorkingIdeas()) this._stopIdeasSnapshotReconcile();
      if (this.selectedIdeaId === id) {
        this.selectedIdeaId = null;
        this.panelOpen = false;
        this.stream = [];
      }
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to delete idea', 'error');
    }
  }

  async restoreIdea(id: string): Promise<Idea | undefined> {
    try {
      const restored = this._normalizeIdea(await api.restoreIdea(id));
      this.archivedIdeas = this.archivedIdeas.filter((idea) => idea.id !== id);
      this._upsertIdea(restored);
      return restored;
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to restore thread', 'error');
    }
  }

  async updateIdeaPosition(id: string, x: number, y: number) {
    try {
      await api.updateIdea(id, { position_x: x, position_y: y });
      const idea = this.ideas.find((i) => i.id === id);
      if (idea) { idea.position_x = x; idea.position_y = y; }
    } catch { /* silent — position is best-effort */ }
  }

  async updateIdeaOwner(id: string, userId: string) {
    try {
      const updated = await api.updateIdea(id, { user_id: userId });
      this._upsertIdea(updated);
      return updated;
    } catch (e) {
      console.error('Failed to update idea owner:', e);
      throw e;
    }
  }

  async updateIdeaOrbitAnchor(id: string, anchorType: string | null, anchorId: string | null) {
    try {
      const updated = await api.updateIdea(id, {
        orbit_anchor_type: anchorType,
        orbit_anchor_id: anchorId,
      });
      this._upsertIdea(updated);
      return updated;
    } catch (e) {
      console.error('Failed to update idea orbit anchor:', e);
      throw e;
    }
  }

  async updateIdeaTitle(id: string, title: string) {
    try {
      await api.updateIdea(id, { title, display_title: title });
      this.ideas = this.ideas.map((i) => (i.id === id ? { ...i, title, display_title: title } : i));
    } catch { /* silent */ }
  }

  private async _refreshSelectedStream() {
    if (!this.selectedIdeaId) return;
    if (this._isLocalPreviewIdeaId(this.selectedIdeaId)) return;
    const version = ++this._streamVersion;
    try {
      const items = await api.unifiedStream(this.selectedIdeaId);
      // Only apply if no newer request has been issued
      if (this._streamVersion === version) {
        const mergedItems = this._mergeLiveStreamState(items, this.selectedIdeaId);
        this.stream = mergedItems;
        this._reconcileIdeaStatusFromStream(this.selectedIdeaId, mergedItems);
        const hasActiveRun = this._hasActiveRuns(mergedItems);
        if (hasActiveRun || this._selectedIdeaLooksWorking(this.selectedIdeaId)) this._ensureSelectedIdeaReconcile();
        else this._stopSelectedIdeaReconcile();
        if (this._hasWorkingIdeas()) this._ensureIdeasSnapshotReconcile();
        else this._stopIdeasSnapshotReconcile();
      }
    } catch { /* silent */ }
  }

  private _scheduleSelectedStreamRefresh(delayMs = 250) {
    if (!this.selectedIdeaId) return;
    if (this._isLocalPreviewIdeaId(this.selectedIdeaId)) return;
    if (this._pendingStreamRefresh) {
      clearTimeout(this._pendingStreamRefresh);
    }
    const ideaId = this.selectedIdeaId;
    this._pendingStreamRefresh = setTimeout(() => {
      this._pendingStreamRefresh = null;
      if (this.selectedIdeaId === ideaId) {
        this._refreshSelectedStream();
      }
    }, delayMs);
  }

  private _ensureSelectedIdeaReconcile() {
    if (!this.selectedIdeaId || this._selectedIdeaReconcile) return;
    if (this._isLocalPreviewIdeaId(this.selectedIdeaId)) return;
    this._selectedIdeaReconcile = setInterval(() => {
      if (!this.selectedIdeaId) {
        this._stopSelectedIdeaReconcile();
        return;
      }
      this._refreshSelectedStream();
    }, 1200);
  }

  private _stopSelectedIdeaReconcile() {
    if (this._selectedIdeaReconcile) {
      clearInterval(this._selectedIdeaReconcile);
      this._selectedIdeaReconcile = null;
    }
  }

  async approveRun(runId: number) {
    try {
      await api.approveRun(runId);
      ui.toast('Run approved', 'success');
      await this._refreshSelectedStream();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to approve', 'error');
    }
  }

  async denyRun(runId: number) {
    try {
      await api.denyRun(runId);
      ui.toast('Run denied', 'info');
      await this._refreshSelectedStream();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to deny', 'error');
    }
  }

  async cancelAll() {
    if (!this.selectedIdeaId) return;
    const ideaId = this.selectedIdeaId;
    try {
      await api.cancelAllRuns(ideaId);
      this._setIdeaPatch(ideaId, { status: 'idle', active_agents: 0, _agents: 0 });
      ui.toast('All runs cancelled', 'info');
      await this._refreshSelectedStream();
      api.getIdea(ideaId)
        .then((fresh) => this._upsertIdea(fresh))
        .catch((e) => console.warn('[cortex] failed to refresh idea after cancel', e));
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to cancel', 'error');
    }
  }

  // WebSocket event handlers
  setupWs() {
    this._wsUnsubs.push(
      ...setupCortexRealtimeBindings({
        store: this as unknown as CortexRealtimeStoreBindings,
        wsClient,
        api,
        ui,
        statusChime,
        ding,
      }),
    );
  }

  teardownWs() {
    if (this.browserSession?.id) {
      wsClient.send('browser_unsubscribe', { session_id: this.browserSession.id });
    }
    if (this._pendingStreamRefresh) {
      clearTimeout(this._pendingStreamRefresh);
      this._pendingStreamRefresh = null;
    }
    this._stopSelectedIdeaReconcile();
    this._stopIdeasSnapshotReconcile();
    this._wsUnsubs.forEach((fn) => fn());
    this._wsUnsubs = [];
  }

  toggleFilter(status: string) {
    const next = new Set(this.filters.statuses);
    if (next.has(status)) next.delete(status);
    else next.add(status);
    this.filters = { ...this.filters, statuses: next };
  }

  setSearch(q: string) {
    this.filters = { ...this.filters, search: q };
  }
}

export const cortex = new CortexStore();
