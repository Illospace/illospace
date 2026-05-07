import { api, type WorkspacePinRead, type WorkspacePinCreateInput, type WorkspacePinUpdateInput } from '$lib/api/client';
import { ui } from '$lib/stores/ui.svelte';
import { wsClient } from '$lib/stores/ws.svelte';

class WorkspacePinsStore {
  pins = $state<WorkspacePinRead[]>([]);
  loading = $state(false);
  loaded = $state(false);
  error = $state<string | null>(null);
  initialized = $state(false);
  private _wsUnsubs: (() => void)[] = [];

  get visiblePins() {
    return this.pins.filter((pin) => !pin.archived_at);
  }

  pinById(pinId: string | null | undefined) {
    if (!pinId) return null;
    return this.pins.find((pin) => pin.id === pinId) ?? null;
  }

  private _upsertPin(pin: WorkspacePinRead) {
    const exists = this.pins.some((entry) => entry.id === pin.id);
    this.pins = exists
      ? this.pins.map((entry) => (entry.id === pin.id ? { ...entry, ...pin } : entry))
      : [...this.pins, pin];
  }

  private _removePin(pinId: string) {
    this.pins = this.pins.filter((pin) => pin.id !== pinId);
  }

  hydrate(pins: WorkspacePinRead[] | null | undefined) {
    if (!Array.isArray(pins)) return;
    this.pins = pins;
    this.loaded = true;
    this.loading = false;
    this.error = null;
  }

  async load(options: { silent?: boolean; force?: boolean } = {}) {
    if (this.loaded && !options.force) return;
    if (this.loading) return;
    this.loading = true;
    this.error = null;
    try {
      this.pins = await api.listWorkspacePins();
      this.loaded = true;
    } catch (err: any) {
      const message = String(err?.detail ?? 'Failed to load workspace pins');
      this.error = message;
      if (!options.silent) ui.toast(message, 'info');
    } finally {
      this.loading = false;
    }
  }

  async create(data: WorkspacePinCreateInput) {
    const pin = await api.createWorkspacePin(data);
    this._upsertPin(pin);
    return pin;
  }

  patchLocal(pinId: string, data: Partial<WorkspacePinRead>) {
    this.pins = this.pins.map((pin) => (pin.id === pinId ? { ...pin, ...data } : pin));
  }

  async update(pinId: string, data: WorkspacePinUpdateInput) {
    const pin = await api.updateWorkspacePin(pinId, data);
    this._upsertPin(pin);
    return pin;
  }

  async deletePin(pinId: string) {
    await api.deleteWorkspacePin(pinId);
    this._removePin(pinId);
  }

  async archive(pinId: string) {
    await this.deletePin(pinId);
  }

  setupWs() {
    if (this.initialized) return;
    this.initialized = true;
    this._wsUnsubs.push(
      wsClient.onReconnect(() => {
        void this.load({ silent: true, force: true });
      }),
      wsClient.on('workspace_pin_created', (msg) => {
        if (msg.pin) this._upsertPin(msg.pin);
      }),
      wsClient.on('workspace_pin_updated', (msg) => {
        if (msg.pin) this._upsertPin(msg.pin);
      }),
      wsClient.on('workspace_pin_archived', (msg) => {
        if (!msg.pin_id) return;
        this._removePin(msg.pin_id);
      }),
      wsClient.on('workspace_pin_deleted', (msg) => {
        if (!msg.pin_id) return;
        this._removePin(msg.pin_id);
      }),
    );
  }

  teardownWs() {
    if (!this.initialized) return;
    this.initialized = false;
    for (const unsub of this._wsUnsubs) unsub();
    this._wsUnsubs = [];
  }
}

export const workspacePins = new WorkspacePinsStore();
