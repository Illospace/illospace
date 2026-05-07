import {
  api,
  type WorkspaceAppCreateInput,
  type WorkspaceAppRead,
  type WorkspaceAppUpdateInput,
} from '$lib/api/client';
import { auth } from '$lib/stores/auth.svelte';
import { ui } from '$lib/stores/ui.svelte';
import { wsClient } from '$lib/stores/ws.svelte';

type WorkspaceAppsChangedMessage = {
  action?: string;
  app?: WorkspaceAppRead;
  app_id?: string;
  key?: string;
  org_id?: string;
};

class WorkspaceAppsStore {
  apps = $state<WorkspaceAppRead[]>([]);
  archivedApps = $state<WorkspaceAppRead[]>([]);
  stateCache = $state<Record<string, Record<string, any>>>({});
  stateLoading = $state<Record<string, boolean>>({});
  loading = $state(false);
  archivedLoading = $state(false);
  loaded = $state(false);
  error = $state<string | null>(null);
  initialized = $state(false);
  lastChangedAppId = $state<string | null>(null);
  lastChangeAction = $state<string | null>(null);
  private _unsubs: (() => void)[] = [];
  private _refreshTimer: ReturnType<typeof setTimeout> | null = null;
  private _followupRefreshTimers = new Set<ReturnType<typeof setTimeout>>();
  private _pendingLoad = false;
  private _stateLoadPromises = new Map<string, Promise<Record<string, any> | null>>();
  private _stateLoadQueue: Array<() => void> = [];
  private _stateLoadsInFlight = 0;
  private _stateLoadDrainTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly _maxConcurrentStateLoads = 2;

  get visibleApps() {
    return this.apps.filter((app) => !app.archived_at);
  }

  appById(appId: string | null | undefined) {
    if (!appId) return null;
    return this.apps.find((app) => app.id === appId) ?? null;
  }

  patchLocal(appId: string | null | undefined, patch: Partial<WorkspaceAppRead>) {
    if (!appId) return;
    this.apps = this.apps.map((app) => (
      app.id === appId
        ? { ...app, ...patch, updated_at: patch.updated_at ?? app.updated_at }
        : app
    ));
  }

  rememberApp(app: WorkspaceAppRead) {
    const exists = this.apps.some((candidate) => candidate.id === app.id);
    this.apps = exists
      ? this.apps.map((candidate) => (candidate.id === app.id ? app : candidate))
      : [app, ...this.apps];
  }

  hydrate(apps: WorkspaceAppRead[] | null | undefined) {
    if (!Array.isArray(apps)) return;
    this.apps = apps;
    this.loaded = true;
    this.loading = false;
    this.error = null;
  }

  async create(data: WorkspaceAppCreateInput) {
    const app = await api.createWorkspaceApp(data);
    this.rememberApp(app);
    this.lastChangedAppId = app.id;
    this.lastChangeAction = 'create';
    return app;
  }

  async update(appId: string | null | undefined, data: WorkspaceAppUpdateInput) {
    if (!appId) return null;
    const updated = await api.updateWorkspaceApp(appId, data);
    this.rememberApp(updated);
    this.lastChangedAppId = updated.id;
    this.lastChangeAction = 'update';
    return updated;
  }

  async moveToPosition(appId: string | null | undefined, x: number, y: number) {
    const app = this.appById(appId);
    if (!appId || !app) return null;

    const visualSpec: Record<string, any> = {
      ...(app.visual_spec ?? {}),
      position_x: x,
      position_y: y,
      placement: 'free',
    };
    delete visualSpec.orbit_anchor_type;
    delete visualSpec.orbit_anchor_id;

    const localPatch: Partial<WorkspaceAppRead> = {
      visual_spec: visualSpec,
    };
    const payload: WorkspaceAppUpdateInput = { visual_spec: visualSpec };

    this.patchLocal(appId, localPatch);

    try {
      return await this.update(appId, payload);
    } catch (err) {
      this.patchLocal(appId, {
        visual_spec: app.visual_spec,
        anchor_user_id: app.anchor_user_id,
      });
      throw err;
    }
  }

  async archive(appId: string | null | undefined) {
    if (!appId) return;
    await api.archiveWorkspaceApp(appId);
    this.apps = this.apps.filter((app) => app.id !== appId);
    void this.loadArchived({ silent: true });
  }

  async loadArchived(options: { silent?: boolean; limit?: number } = {}) {
    if (this.archivedLoading) return;
    this.archivedLoading = true;
    try {
      this.archivedApps = await api.listArchivedWorkspaceApps(options.limit ?? 12);
    } catch (err: any) {
      if (!options.silent) {
        ui.toast(err?.detail || 'Failed to load archived apps', 'info');
      }
    } finally {
      this.archivedLoading = false;
    }
  }

  async restore(appId: string | null | undefined) {
    if (!appId) return null;
    const restored = await api.restoreWorkspaceApp(appId);
    this.archivedApps = this.archivedApps.filter((app) => app.id !== appId);
    this.rememberApp(restored);
    this.lastChangedAppId = restored.id;
    this.lastChangeAction = 'restore';
    return restored;
  }

  stateCacheKey(appId: string, stateKey = 'default') {
    return `${appId}:${stateKey}`;
  }

  cachedState(appId: string | null | undefined, stateKey = 'default') {
    if (!appId) return null;
    return this.stateCache[this.stateCacheKey(appId, stateKey)] ?? null;
  }

  rememberState(appId: string | null | undefined, stateKey = 'default', data: Record<string, any> | null | undefined) {
    if (!appId || !data) return;
    this.stateCache = {
      ...this.stateCache,
      [this.stateCacheKey(appId, stateKey)]: data,
    };
  }

  async loadState(appId: string | null | undefined, stateKey = 'default', options: { silent?: boolean; force?: boolean } = {}) {
    if (!appId) return this.cachedState(appId, stateKey);
    const cacheKey = this.stateCacheKey(appId, stateKey);
    if (!options.force && this.stateCache[cacheKey]) return this.stateCache[cacheKey];
    const pending = this._stateLoadPromises.get(cacheKey);
    if (pending) return pending;

    this.stateLoading = { ...this.stateLoading, [cacheKey]: true };
    const promise = api.getWorkspaceAppState(appId, stateKey)
      .then((state) => {
        this.rememberState(appId, stateKey, state.data);
        return state.data;
      })
      .catch((err: any) => {
        if (!options.silent) {
          ui.toast(err?.detail || 'Failed to load workspace app state', 'info');
        }
        return this.stateCache[cacheKey] ?? null;
      })
      .finally(() => {
        const { [cacheKey]: _removed, ...nextLoading } = this.stateLoading;
        this.stateLoading = nextLoading;
        this._stateLoadPromises.delete(cacheKey);
      });
    this._stateLoadPromises.set(cacheKey, promise);
    return promise;
  }

  loadStateQueued(
    appId: string | null | undefined,
    stateKey = 'default',
    options: { silent?: boolean; force?: boolean; delayMs?: number } = {},
  ) {
    if (!appId) return Promise.resolve(this.cachedState(appId, stateKey));
    const cacheKey = this.stateCacheKey(appId, stateKey);
    if (!options.force && this.stateCache[cacheKey]) return Promise.resolve(this.stateCache[cacheKey]);

    return new Promise<Record<string, any> | null>((resolve) => {
      this._stateLoadQueue.push(() => {
        this._stateLoadsInFlight += 1;
        void this.loadState(appId, stateKey, options)
          .then(resolve)
          .finally(() => {
            this._stateLoadsInFlight = Math.max(0, this._stateLoadsInFlight - 1);
            this._drainStateLoadQueue();
          });
      });
      this._scheduleStateLoadDrain(options.delayMs ?? 0);
    });
  }

  private _scheduleStateLoadDrain(delayMs: number) {
    if (this._stateLoadDrainTimer) return;
    const run = () => {
      this._stateLoadDrainTimer = null;
      this._drainStateLoadQueue();
    };
    if (typeof window === 'undefined') {
      this._stateLoadDrainTimer = setTimeout(run, delayMs);
      return;
    }
    this._stateLoadDrainTimer = window.setTimeout(() => {
      const requestIdle = (window as typeof window & {
        requestIdleCallback?: (callback: () => void, options?: { timeout?: number }) => number;
      }).requestIdleCallback;
      if (requestIdle) requestIdle(run, { timeout: 1600 });
      else run();
    }, delayMs);
  }

  private _drainStateLoadQueue() {
    while (this._stateLoadsInFlight < this._maxConcurrentStateLoads && this._stateLoadQueue.length > 0) {
      const next = this._stateLoadQueue.shift();
      next?.();
    }
  }

  async updateState(appId: string | null | undefined, stateKey: string, data: Record<string, any>) {
    if (!appId) return data;
    this.rememberState(appId, stateKey, data);

    const state = await api.updateWorkspaceAppState(appId, stateKey, data);
    this.rememberState(appId, stateKey, state.data);
    return state.data;
  }

  async load(options: { silent?: boolean; force?: boolean } = {}) {
    if (this.loaded && !options.force) return;
    if (this.loading) {
      if (options.force) this._pendingLoad = true;
      return;
    }
    this.loading = true;
    this.error = null;
    try {
      this.apps = await api.listWorkspaceApps();
      this.loaded = true;
    } catch (err: any) {
      const message = String(err?.detail ?? 'Failed to load workspace apps');
      this.error = message;
      if (!options.silent) {
        ui.toast(message, 'info');
      }
    } finally {
      this.loading = false;
      if (this._pendingLoad) {
        this._pendingLoad = false;
        void this.load({ silent: true, force: true });
      }
    }
  }

  setup() {
    if (this.initialized) return;
    this.initialized = true;
    this._unsubs.push(
      wsClient.onReconnect(() => {
        this.scheduleRefresh(120);
      }),
    );
    this._unsubs.push(
      wsClient.on('workspace_apps_changed', (msg: WorkspaceAppsChangedMessage) => {
        this.applyChangeEvent(msg);
      }),
    );
    this._unsubs.push(
      wsClient.on('status_change', (msg: any) => {
        const status = String(msg?.new_status || '').toLowerCase();
        if (status === 'completed' || status === 'done' || status === 'failed' || status === 'cancelled') {
          this.scheduleRefresh(700);
          this.scheduleFollowupRefreshes([1800, 4200]);
        }
      }),
    );
  }

  teardown() {
    if (!this.initialized) return;
    this.initialized = false;
    this._unsubs.forEach((fn) => fn());
    this._unsubs = [];
    if (this._refreshTimer) {
      clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
    this._followupRefreshTimers.forEach((timer) => clearTimeout(timer));
    this._followupRefreshTimers.clear();
    if (this._stateLoadDrainTimer) {
      clearTimeout(this._stateLoadDrainTimer);
      this._stateLoadDrainTimer = null;
    }
    this._stateLoadQueue = [];
  }

  scheduleRefresh(delayMs = 250) {
    if (this._refreshTimer) clearTimeout(this._refreshTimer);
    this._refreshTimer = setTimeout(() => {
      this._refreshTimer = null;
      void this.load({ silent: true, force: true });
    }, delayMs);
  }

  scheduleFollowupRefreshes(delaysMs: number[]) {
    delaysMs.forEach((delayMs) => {
      const timer = setTimeout(() => {
        this._followupRefreshTimers.delete(timer);
        void this.load({ silent: true, force: true });
      }, delayMs);
      this._followupRefreshTimers.add(timer);
    });
  }

  private _isOwnOrg(msg: WorkspaceAppsChangedMessage) {
    const eventOrgId = String(msg?.org_id || '').trim();
    return !eventOrgId || !auth.user?.org_id || eventOrgId === auth.user.org_id;
  }

  private _removeApp(appId?: string, key?: string) {
    if (!appId && !key) return;
    const removed = this.apps.find((app) => app.id === appId || app.key === key);
    this.apps = this.apps.filter((app) => app.id !== appId && app.key !== key);
    if (removed) {
      this.archivedApps = [
        { ...removed, archived_at: removed.archived_at || new Date().toISOString() },
        ...this.archivedApps.filter((app) => app.id !== removed.id),
      ].slice(0, 12);
    }
    if (appId) {
      const prefix = `${appId}:`;
      this.stateCache = Object.fromEntries(
        Object.entries(this.stateCache).filter(([cacheKey]) => !cacheKey.startsWith(prefix)),
      );
    }
  }

  private _upsertApp(nextApp: WorkspaceAppRead) {
    const existingIndex = this.apps.findIndex((app) => app.id === nextApp.id);
    if (existingIndex === -1) {
      this.apps = [nextApp, ...this.apps];
      return;
    }
    this.apps = this.apps.map((app, index) => (index === existingIndex ? nextApp : app));
  }

  applyChangeEvent(msg: WorkspaceAppsChangedMessage) {
    if (!this._isOwnOrg(msg)) return;
    const action = String(msg?.action || '').toLowerCase();
    const appId = msg?.app_id || msg?.app?.id;
    const key = msg?.key || msg?.app?.key;
    if (action === 'archive' || action === 'delete' || action === 'remove') {
      this._removeApp(appId, key);
    } else if (msg?.app) {
      this._upsertApp(msg.app);
      this.archivedApps = this.archivedApps.filter((app) => app.id !== msg.app?.id);
      this.lastChangedAppId = msg.app.id;
      this.lastChangeAction = action || 'update';
    }
    if (action === 'archive') void this.loadArchived({ silent: true });
    this.scheduleRefresh(action === 'archive' ? 450 : 650);
  }
}

export const workspaceApps = new WorkspaceAppsStore();
