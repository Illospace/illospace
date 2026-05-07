import {
  api,
  type AppNotification,
  type AppNotificationSummary,
  type NotificationPreferences,
} from '$lib/api/client';
import { wsClient } from '$lib/stores/ws.svelte';

const DEFAULT_SUMMARY: AppNotificationSummary = {
  chat_unread_total: 0,
  workspace_attention_total: 0,
  unread_notification_total: 0,
  unread_chat_notification_total: 0,
  unread_workspace_notification_total: 0,
};

const DEFAULT_PREFERENCES: NotificationPreferences = {
  sound_enabled: true,
  message_notifications_enabled: true,
};

function cloneSummary(
  summary?: Partial<AppNotificationSummary> | null,
): AppNotificationSummary {
  return {
    chat_unread_total: summary?.chat_unread_total ?? 0,
    workspace_attention_total: summary?.workspace_attention_total ?? 0,
    unread_notification_total: summary?.unread_notification_total ?? 0,
    unread_chat_notification_total: summary?.unread_chat_notification_total ?? 0,
    unread_workspace_notification_total: summary?.unread_workspace_notification_total ?? 0,
  };
}

function sortUnread(notifications: AppNotification[]): AppNotification[] {
  return [...notifications].sort((a, b) => {
    const timeDiff = new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
    if (timeDiff !== 0) return timeDiff;
    return b.id - a.id;
  });
}

class NotificationsStore {
  initialized = $state(false);
  loading = $state(false);
  error = $state<string | null>(null);
  unread = $state<AppNotification[]>([]);
  summary = $state<AppNotificationSummary>(cloneSummary(DEFAULT_SUMMARY));
  preferences = $state<NotificationPreferences>({ ...DEFAULT_PREFERENCES });

  private _unsubs: (() => void)[] = [];

  get badgeCount(): number {
    return this.summary.unread_notification_total;
  }

  async setup() {
    if (!this.initialized) {
      this.initialized = true;
      this._registerWsHandlers();
    }
    await this.refreshAll(true);
  }

  teardown() {
    if (!this.initialized) return;
    this.initialized = false;
    this._unsubs.forEach((fn) => fn());
    this._unsubs = [];
  }

  async refreshAll(silent = false) {
    if (!silent) {
      this.loading = true;
      this.error = null;
    }
    try {
      const [summary, unread, preferences] = await Promise.all([
        api.notificationSummary(),
        api.listNotifications({ status: 'unread', limit: 50 }),
        api.notificationPreferences(),
      ]);
      this.summary = cloneSummary(summary);
      this.unread = sortUnread(unread);
      this.preferences = { ...DEFAULT_PREFERENCES, ...preferences };
    } catch (err: any) {
      this.error = err?.detail || err?.message || 'Failed to load notifications';
    } finally {
      if (!silent) this.loading = false;
    }
  }

  async refreshUnread(silent = true) {
    if (!silent) {
      this.loading = true;
      this.error = null;
    }
    try {
      this.unread = sortUnread(await api.listNotifications({ status: 'unread', limit: 50 }));
    } catch (err: any) {
      this.error = err?.detail || err?.message || 'Failed to refresh notifications';
    } finally {
      if (!silent) this.loading = false;
    }
  }

  async markRead(notificationId: number) {
    const previous = this.unread;
    this.unread = previous.filter((notification) => notification.id !== notificationId);
    try {
      this.summary = cloneSummary(await api.markNotificationRead(notificationId));
    } catch (err: any) {
      this.unread = previous;
      this.error = err?.detail || err?.message || 'Failed to mark notification read';
      throw err;
    }
  }

  async markAllRead() {
    const previous = this.unread;
    this.unread = [];
    try {
      this.summary = cloneSummary(await api.markAllNotificationsRead());
    } catch (err: any) {
      this.unread = previous;
      this.error = err?.detail || err?.message || 'Failed to mark notifications read';
      throw err;
    }
  }

  async setSoundEnabled(enabled: boolean) {
    const previous = this.preferences;
    this.preferences = { ...previous, sound_enabled: enabled };
    try {
      this.preferences = {
        ...DEFAULT_PREFERENCES,
        ...(await api.updateNotificationPreferences({ sound_enabled: enabled })),
      };
    } catch (err: any) {
      this.preferences = previous;
      this.error = err?.detail || err?.message || 'Failed to update notification sound';
      throw err;
    }
  }

  async setMessageNotificationsEnabled(enabled: boolean) {
    const previous = this.preferences;
    this.preferences = { ...previous, message_notifications_enabled: enabled };
    try {
      this.preferences = {
        ...DEFAULT_PREFERENCES,
        ...(await api.updateNotificationPreferences({ message_notifications_enabled: enabled })),
      };
      if (enabled) {
        void this.refreshAll(true);
      } else {
        void this.refreshUnread(true);
      }
    } catch (err: any) {
      this.preferences = previous;
      this.error = err?.detail || err?.message || 'Failed to update message notifications';
      throw err;
    }
  }

  private _playNotificationSound() {
    if (!this.preferences.sound_enabled || typeof window === 'undefined') return;
    try {
      const AudioContextCtor = window.AudioContext || (window as any).webkitAudioContext;
      if (!AudioContextCtor) return;
      const audioContext = new AudioContextCtor();
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(880, audioContext.currentTime);
      oscillator.frequency.exponentialRampToValueAtTime(660, audioContext.currentTime + 0.12);
      gain.gain.setValueAtTime(0.0001, audioContext.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.04, audioContext.currentTime + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.16);
      oscillator.connect(gain);
      gain.connect(audioContext.destination);
      oscillator.start();
      oscillator.stop(audioContext.currentTime + 0.18);
      setTimeout(() => void audioContext.close().catch(() => undefined), 260);
    } catch {
      // Browsers may block audio until user interaction; notifications still render.
    }
  }

  private _registerWsHandlers() {
    this._unsubs.push(
      wsClient.onReconnect(() => {
        void this.refreshAll(true);
      }),
    );

    this._unsubs.push(
      wsClient.on('notification_summary_updated', (msg) => {
        const previousUnreadTotal = this.summary.unread_notification_total;
        if (msg.summary) {
          this.summary = cloneSummary(msg.summary as Partial<AppNotificationSummary>);
        }
        if (this.summary.unread_notification_total > previousUnreadTotal) {
          this._playNotificationSound();
        }
        void this.refreshUnread(true);
      }),
    );
  }
}

export const notifications = new NotificationsStore();
