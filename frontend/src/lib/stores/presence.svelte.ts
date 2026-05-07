import { wsClient } from '$lib/stores/ws.svelte';

interface Viewer {
  user_id: string;
  name: string;
  color: string;
  idea_id: string;
}

class PresenceStore {
  viewers = $state<Viewer[]>([]);
  private _unsubs: (() => void)[] = [];

  /** Get viewers for a specific idea */
  viewersFor(ideaId: string): Viewer[] {
    return this.viewers.filter((v) => v.idea_id === ideaId);
  }

  setup() {
    this._unsubs.push(
      wsClient.on('presence', (msg) => {
        if (msg.status === 'online' && msg.idea_id) {
          // Add or update viewer
          this.viewers = [
            ...this.viewers.filter((v) => v.user_id !== msg.user_id || v.idea_id !== msg.idea_id),
            { user_id: msg.user_id, name: msg.name || '?', color: msg.color || '#6366f1', idea_id: msg.idea_id },
          ];
        } else if (msg.status === 'offline') {
          this.viewers = this.viewers.filter((v) => v.user_id !== msg.user_id);
        }
      }),
    );

    this._unsubs.push(
      wsClient.on('focus_idea', (msg) => {
        this.viewers = [
          ...this.viewers.filter((v) => v.user_id !== msg.user_id),
          { user_id: msg.user_id, name: msg.name || '?', color: msg.color || '#6366f1', idea_id: msg.idea_id },
        ];
      }),
    );

    this._unsubs.push(
      wsClient.on('unfocus_idea', (msg) => {
        this.viewers = this.viewers.filter((v) => v.user_id !== msg.user_id);
      }),
    );
  }

  teardown() {
    this._unsubs.forEach((fn) => fn());
    this._unsubs = [];
    this.viewers = [];
  }
}

export const presence = new PresenceStore();
