<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  import type { AppNotification } from '$lib/api/client';
  import { ConstellationIconButton } from '$lib/components/constellation';
  import { notifications } from '$lib/stores/notifications.svelte';
  import { relativeTimeAgo } from '$lib/utils/datetime';

  let {
    onSelect,
  }: {
    onSelect?: (notification: AppNotification) => void | Promise<void>;
  } = $props();

  let open = $state(false);
  let rootEl: HTMLDivElement | undefined = $state();

  function timeAgo(value: string): string {
    return relativeTimeAgo(value) || '0s ago';
  }

  function sourceLabel(source: string): string {
    return source === 'workspace' ? 'Workspace' : 'Chat';
  }

  function actorInitial(notification: AppNotification): string {
    const seed = notification.actor_name || sourceLabel(notification.source);
    return seed.slice(0, 1).toUpperCase();
  }

  function notificationActorColor(notification: AppNotification): string {
    return notification.actor_color || (notification.source === 'chat' ? '#5ea9ff' : '#5ecfa0');
  }

  function notificationRowStyle(notification: AppNotification): string {
    return `--notification-actor-color:${notificationActorColor(notification)}`;
  }

  function toggleMenu(event: MouseEvent) {
    event.stopPropagation();
    open = !open;
    if (open) {
      void notifications.refreshAll(true);
    }
  }

  async function handleSelect(notification: AppNotification) {
    open = false;
    await onSelect?.(notification);
  }

  async function handleMarkAllRead() {
    await notifications.markAllRead();
  }

  async function toggleSoundNotifications() {
    await notifications.setSoundEnabled(!notifications.preferences.sound_enabled);
  }

  async function toggleMessageNotifications() {
    await notifications.setMessageNotificationsEnabled(
      !notifications.preferences.message_notifications_enabled,
    );
  }

  function handleDocumentClick(event: MouseEvent) {
    const target = event.target as Node | null;
    if (open && rootEl && target && !rootEl.contains(target)) {
      open = false;
    }
  }

  onMount(() => {
    document.addEventListener('click', handleDocumentClick);
  });

  onDestroy(() => {
    document.removeEventListener('click', handleDocumentClick);
  });
</script>

<div class="notifications-root" bind:this={rootEl}>
  <div class="notifications-trigger-shell">
    <ConstellationIconButton
      label="Notifications"
      title="Notifications"
      size="md"
      variant="secondary"
      pressed={open}
      onclick={toggleMenu}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M8 18h8" />
        <path d="M6.5 16.5h11a1 1 0 0 0 .86-1.5c-1.12-1.86-1.68-3.8-1.68-5.8V9a4.68 4.68 0 0 0-9.36 0v.2c0 2-.56 3.94-1.68 5.8a1 1 0 0 0 .86 1.5Z" />
        <path d="M10 19a2 2 0 0 0 4 0" />
      </svg>
    </ConstellationIconButton>

    {#if notifications.badgeCount > 0}
      <span class="notifications-badge">
        {notifications.badgeCount > 9 ? '9+' : notifications.badgeCount}
      </span>
    {/if}
  </div>

  {#if open}
    <div class="notifications-menu" role="menu" aria-label="Unread notifications">
      <div class="notifications-header">
        <div class="notifications-heading">
          <strong>Notifications</strong>
          <span>{notifications.badgeCount === 0 ? 'Unread inbox' : `${notifications.badgeCount} unread`}</span>
        </div>

        {#if notifications.unread.length > 0}
          <button type="button" class="notifications-mark-all" onclick={handleMarkAllRead}>
            Mark all read
          </button>
        {/if}
      </div>

      <div class="notifications-preferences" aria-label="Notification preferences">
        <button
          type="button"
          class:active={notifications.preferences.message_notifications_enabled}
          aria-pressed={notifications.preferences.message_notifications_enabled}
          title={notifications.preferences.message_notifications_enabled ? 'Mute message notifications' : 'Unmute message notifications'}
          onclick={toggleMessageNotifications}
        >
          <span aria-hidden="true">{notifications.preferences.message_notifications_enabled ? '🔔' : '🔕'}</span>
          <span>Messages</span>
        </button>
        <button
          type="button"
          class:active={notifications.preferences.sound_enabled}
          aria-pressed={notifications.preferences.sound_enabled}
          title={notifications.preferences.sound_enabled ? 'Mute notification sound' : 'Unmute notification sound'}
          onclick={toggleSoundNotifications}
        >
          <span aria-hidden="true">{notifications.preferences.sound_enabled ? '🔊' : '🔇'}</span>
          <span>Sound</span>
        </button>
      </div>

      {#if notifications.loading && notifications.unread.length === 0}
        <div class="notifications-empty">Loading notifications...</div>
      {:else if notifications.unread.length === 0}
        <div class="notifications-empty">No unseen notifications right now.</div>
      {:else}
        <div class="notifications-list">
          {#each notifications.unread as notification}
            <button
              type="button"
              class="notification-row"
              style={notificationRowStyle(notification)}
              role="menuitem"
              onclick={() => handleSelect(notification)}
            >
              <span class="notification-avatar">
                {actorInitial(notification)}
              </span>

              <span class="notification-copy">
                <span class="notification-meta">
                  <span class="notification-source">{sourceLabel(notification.source)}</span>
                  <span class="notification-time">{timeAgo(notification.updated_at)}</span>
                </span>

                <strong>{notification.title}</strong>

                {#if notification.body}
                  <span class="notification-body">{notification.body}</span>
                {/if}

                {#if notification.occurrence_count > 1}
                  <span class="notification-occurrence">{notification.occurrence_count} unseen updates</span>
                {/if}
              </span>
            </button>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .notifications-root {
    position: relative;
    flex: 0 0 auto;
  }

  .notifications-trigger-shell {
    position: relative;
  }

  .notifications-badge {
    position: absolute;
    top: -5px;
    right: -4px;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background: linear-gradient(180deg, rgba(241, 118, 118, 0.96), rgba(217, 69, 69, 0.94));
    color: rgba(255, 247, 247, 0.96);
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
    box-shadow: 0 10px 22px rgba(135, 28, 28, 0.32);
  }

  .notifications-menu {
    position: absolute;
    top: calc(100% + 14px);
    right: 0;
    width: min(360px, 88vw);
    display: grid;
    gap: 14px;
    padding: 16px;
    border-radius: 22px;
    border: 1px solid var(--constellation-notification-menu-border);
    background: var(--constellation-notification-menu-background);
    box-shadow: var(--constellation-notification-menu-shadow);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    z-index: 30;
  }

  .notifications-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .notifications-heading {
    display: grid;
    gap: 4px;
  }

  .notifications-heading strong {
    color: var(--constellation-notification-title);
    font-size: 14px;
    font-weight: 600;
  }

  .notifications-heading span {
    color: var(--constellation-notification-subtitle);
    font-size: 12px;
  }

  .notifications-mark-all {
    border: 0;
    background: transparent;
    color: var(--constellation-notification-mark-all);
    font-size: 12px;
    cursor: pointer;
  }

  .notifications-preferences {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .notifications-preferences button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-height: 32px;
    border-radius: 999px;
    border: 1px solid var(--constellation-notification-row-border);
    background: var(--constellation-notification-row-background);
    color: var(--constellation-notification-subtitle);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition:
      border-color 180ms ease,
      background-color 180ms ease,
      color 180ms ease;
  }

  .notifications-preferences button.active {
    border-color: var(--constellation-notification-row-hover-border);
    background: var(--constellation-notification-row-hover-background);
    color: var(--constellation-notification-title);
  }

  .notifications-empty {
    padding: 18px 14px;
    border-radius: 16px;
    border: 1px dashed var(--constellation-notification-empty-border);
    color: var(--constellation-notification-empty-text);
    font-size: 12px;
    text-align: center;
  }

  .notifications-list {
    display: grid;
    gap: 8px;
    max-height: min(52vh, 420px);
    overflow: auto;
  }

  .notification-row {
    display: grid;
    grid-template-columns: 34px minmax(0, 1fr);
    gap: 12px;
    align-items: start;
    width: 100%;
    padding: 12px;
    border: 1px solid var(--constellation-notification-row-border);
    border-radius: 16px;
    background: var(--constellation-notification-row-background);
    color: inherit;
    --notification-actor-color: var(--constellation-color-user-accent, #57cfa0);
    text-align: left;
    cursor: pointer;
    transition:
      transform 180ms ease,
      border-color 180ms ease,
      background-color 180ms ease;
  }

  .notification-row:hover {
    transform: translateY(-1px);
    border-color: color-mix(in srgb, var(--notification-actor-color) 24%, transparent);
    background: var(--constellation-notification-row-hover-background);
  }

  .notification-avatar {
    width: 34px;
    height: 34px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background:
      radial-gradient(circle at 36% 28%, rgba(255, 255, 255, 0.34), transparent 34%),
      color-mix(in srgb, var(--notification-actor-color) 88%, rgba(9, 12, 19, 0.18));
    color: var(--constellation-notification-avatar-text);
    font-size: 12px;
    font-weight: 700;
  }

  :global(:root[data-color-scheme='light']) .notification-avatar {
    background:
      radial-gradient(circle at 36% 28%, rgba(255, 255, 255, 0.5), transparent 34%),
      color-mix(in srgb, var(--notification-actor-color) 84%, rgba(255, 253, 247, 0.16));
  }

  .notification-copy {
    display: grid;
    gap: 5px;
    min-width: 0;
  }

  .notification-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    color: var(--constellation-notification-meta);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .notification-copy strong {
    color: var(--constellation-notification-title);
    font-size: 13px;
    font-weight: 600;
    line-height: 1.35;
  }

  .notification-body {
    color: var(--constellation-notification-body);
    font-size: 12px;
    line-height: 1.45;
  }

  .notification-occurrence {
    color: color-mix(in srgb, var(--notification-actor-color) 84%, transparent);
    font-size: 11px;
  }
</style>
