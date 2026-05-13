<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  import type { WorkspaceAppRead } from '$lib/api/client';
  import { ConstellationIcon, ConstellationIconButton } from '$lib/components/constellation';
  import { cortex, type Idea } from '$lib/stores/cortex.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import { relativeTimeAgo } from '$lib/utils/datetime';

  let {
    dragging = false,
    dropActive = false,
    onrestore,
    onrestoreapp,
  }: {
    dragging?: boolean;
    dropActive?: boolean;
    onrestore?: (idea: Idea, event: MouseEvent) => void | Promise<void>;
    onrestoreapp?: (app: WorkspaceAppRead, event: MouseEvent) => void | Promise<void>;
  } = $props();

  let open = $state(false);
  let emptying = $state(false);
  let rootEl: HTMLDivElement | undefined = $state();

  const recentArchived = $derived(cortex.archivedIdeas.slice(0, 12));
  const recentArchivedApps = $derived(workspaceApps.archivedApps.slice(0, 12));
  const visibleArchivedCount = $derived(recentArchived.length + recentArchivedApps.length);

  function timeAgo(value: string | null | undefined): string {
    return relativeTimeAgo(value) || 'recently';
  }

  function threadTitle(idea: Idea): string {
    return idea.display_title || idea.title || 'Untitled thread';
  }

  function actorInitial(idea: Idea): string {
    return (idea.author_name || threadTitle(idea)).slice(0, 1).toUpperCase();
  }

  function toggleMenu(event: MouseEvent) {
    event.stopPropagation();
    open = !open;
    if (open) {
      void cortex.loadArchivedIdeas(12);
      void workspaceApps.loadArchived({ limit: 12, silent: true });
    }
  }

  async function handleRestore(idea: Idea, event: MouseEvent) {
    event.stopPropagation();
    open = false;
    await onrestore?.(idea, event);
  }

  async function handleRestoreApp(app: WorkspaceAppRead, event: MouseEvent) {
    event.stopPropagation();
    open = false;
    await onrestoreapp?.(app, event);
  }

  async function handleEmptyBin(event: MouseEvent) {
    event.stopPropagation();
    if (emptying || visibleArchivedCount === 0) return;

    const confirmed = typeof window !== 'undefined' && window.confirm(
      'Permanently delete all archived threads and apps in the trash bin? This cannot be undone.',
    );
    if (!confirmed) return;

    emptying = true;
    try {
      const [threadResult, appResult] = await Promise.all([
        cortex.emptyArchivedIdeas(),
        workspaceApps.emptyArchived(),
      ]);
      const deleted = Number(threadResult?.deleted || 0) + Number(appResult?.deleted || 0);
      const suffix = deleted === 1 ? 'item' : 'items';
      ui.toast(
        deleted > 0 ? `Trash bin emptied. ${deleted} ${suffix} deleted.` : 'Trash bin is already empty.',
        'success',
      );
      open = false;
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to empty trash bin', 'error');
    } finally {
      emptying = false;
    }
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

<div
  class="cortex-archive-root"
  class:dragging
  class:drop-active={dropActive}
  bind:this={rootEl}
  data-cortex-archive-bin="true"
>
  <div class="cortex-archive-trigger-shell">
    <ConstellationIconButton
      label="Archived"
      title="Archived"
      size="md"
      variant="secondary"
      className="cortex-archive-trigger"
      pressed={open || dropActive}
      onclick={toggleMenu}
    >
      <ConstellationIcon name="trash" size={15} stroke={1.85} />
    </ConstellationIconButton>
  </div>

  {#if open}
    <div class="cortex-archive-menu" role="menu" aria-label="Recently archived items">
      <div class="cortex-archive-header">
        <div class="cortex-archive-heading">
          <strong>Archived</strong>
          <span>Recent threads and apps</span>
        </div>
        <button
          type="button"
          class="cortex-archive-empty-action"
          disabled={visibleArchivedCount === 0 || emptying}
          title="Empty trash bin"
          aria-label="Empty trash bin"
          role="menuitem"
          onclick={handleEmptyBin}
        >
          <ConstellationIcon name="trash" size={12} stroke={2} />
          <span>{emptying ? 'Emptying' : 'Empty'}</span>
        </button>
      </div>

      {#if (cortex.archivedIdeasLoading || workspaceApps.archivedLoading) && recentArchived.length === 0 && recentArchivedApps.length === 0}
        <div class="cortex-archive-empty">Loading archived items...</div>
      {:else if recentArchived.length === 0 && recentArchivedApps.length === 0}
        <div class="cortex-archive-empty">No archived items yet.</div>
      {:else}
        <div class="cortex-archive-list">
          {#if recentArchived.length > 0}
            <div class="cortex-archive-section-label">Threads</div>
            {#each recentArchived as idea (idea.id)}
              <button
                type="button"
                class="cortex-archive-row"
                role="menuitem"
                onclick={(event) => handleRestore(idea, event)}
              >
                <span
                  class="cortex-archive-avatar"
                  style:background={idea.author_color || 'rgba(94, 169, 255, 0.2)'}
                >
                  {actorInitial(idea)}
                </span>

                <span class="cortex-archive-copy">
                  <strong>{threadTitle(idea)}</strong>
                  <span>Thread archived {timeAgo(idea.archived_at)}</span>
                </span>
              </button>
            {/each}
          {/if}

          {#if recentArchivedApps.length > 0}
            <div class="cortex-archive-section-label">Apps</div>
            {#each recentArchivedApps as app (app.id)}
              <button
                type="button"
                class="cortex-archive-row"
                role="menuitem"
                onclick={(event) => handleRestoreApp(app, event)}
              >
                <span
                  class="cortex-archive-avatar cortex-archive-avatar--app"
                  style:background={app.visual_spec?.accent || 'rgba(87, 207, 160, 0.2)'}
                >
                  {(app.name || app.key || 'A').slice(0, 1).toUpperCase()}
                </span>

                <span class="cortex-archive-copy">
                  <strong>{app.name || app.key || 'Generated app'}</strong>
                  <span>App archived {timeAgo(app.archived_at)}</span>
                </span>
              </button>
            {/each}
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .cortex-archive-root {
    --cortex-archive-bin-size: var(--workspace-chrome-control-height, 46px);
    --cortex-archive-bin-radius: 14px;
    --cortex-archive-bin-glow-opacity: 0;
    position: relative;
    flex: 0 0 auto;
  }

  .cortex-archive-root.dragging {
    --cortex-archive-bin-size: 96px;
    --cortex-archive-bin-radius: 24px;
    --cortex-archive-bin-glow-opacity: 0.64;
  }

  .cortex-archive-root.drop-active {
    --cortex-archive-bin-size: 116px;
    --cortex-archive-bin-radius: 30px;
    --cortex-archive-bin-glow-opacity: 0.9;
  }

  .cortex-archive-trigger-shell {
    position: relative;
    width: var(--cortex-archive-bin-size);
    height: var(--cortex-archive-bin-size);
    transform-origin: right bottom;
    transition:
      width 260ms cubic-bezier(0.2, 0.95, 0.22, 1),
      height 260ms cubic-bezier(0.2, 0.95, 0.22, 1);
  }

  .cortex-archive-trigger-shell :global(.cortex-archive-trigger) {
    position: relative;
    width: var(--cortex-archive-bin-size);
    height: var(--cortex-archive-bin-size);
    min-width: var(--cortex-archive-bin-size);
    min-height: var(--cortex-archive-bin-size);
    box-sizing: border-box;
    overflow: hidden;
    border-radius: var(--cortex-archive-bin-radius);
    border-color: var(--constellation-system-chrome-border);
    background: var(--constellation-system-chrome-background);
    color: var(--constellation-system-chrome-text);
    box-shadow: var(--constellation-system-chrome-shadow);
    transform-origin: right bottom;
    transition:
      width 260ms cubic-bezier(0.2, 0.95, 0.22, 1),
      height 260ms cubic-bezier(0.2, 0.95, 0.22, 1),
      min-width 260ms cubic-bezier(0.2, 0.95, 0.22, 1),
      min-height 260ms cubic-bezier(0.2, 0.95, 0.22, 1),
      border-radius 260ms cubic-bezier(0.2, 0.95, 0.22, 1),
      transform var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-settle-duration) ease,
      border-color var(--constellation-motion-settle-duration) ease,
      color var(--constellation-motion-settle-duration) ease,
      box-shadow var(--constellation-motion-settle-duration) ease;
    backdrop-filter: blur(20px) saturate(1.08);
    -webkit-backdrop-filter: blur(20px) saturate(1.08);
  }

  .cortex-archive-trigger-shell :global(.cortex-archive-trigger)::before {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    background:
      radial-gradient(
        circle at 36% 34%,
        color-mix(in srgb, var(--constellation-system-chrome-active-text) 24%, transparent),
        transparent 58%
      ),
      linear-gradient(
        145deg,
        color-mix(in srgb, var(--constellation-system-chrome-active-background) 72%, transparent),
        transparent 62%
      );
    opacity: var(--cortex-archive-bin-glow-opacity);
    pointer-events: none;
    transition: opacity 220ms ease;
  }

  .cortex-archive-trigger-shell :global(.cortex-archive-trigger:hover:not(:disabled)) {
    border-color: var(--constellation-system-chrome-active-border);
    background: var(--constellation-system-chrome-active-background);
    color: var(--constellation-system-chrome-text-hover);
  }

  .cortex-archive-trigger-shell :global(.cortex-archive-trigger[aria-pressed='true']) {
    border-color: var(--constellation-system-chrome-active-border);
    background: var(--constellation-system-chrome-active-background);
    color: var(--constellation-system-chrome-active-text);
    box-shadow: var(--constellation-system-chrome-active-shadow);
  }

  .cortex-archive-trigger-shell :global(.cortex-archive-trigger svg) {
    width: 15px;
    height: 15px;
  }

  .cortex-archive-trigger-shell :global(.cortex-archive-trigger .constellation-icon-button-icon) {
    position: relative;
    z-index: 1;
    transition: transform 240ms cubic-bezier(0.2, 0.95, 0.22, 1);
  }

  .cortex-archive-root.dragging :global(.cortex-archive-trigger) {
    border-color: color-mix(in srgb, var(--constellation-system-chrome-active-border) 72%, transparent);
    background: color-mix(in srgb, var(--constellation-system-chrome-active-background) 88%, transparent);
    color: var(--constellation-system-chrome-active-text);
    box-shadow:
      var(--constellation-system-chrome-active-shadow),
      0 0 34px color-mix(in srgb, var(--constellation-system-chrome-active-text) 14%, transparent);
    transform: none;
  }

  .cortex-archive-root.dragging :global(.cortex-archive-trigger .constellation-icon-button-icon) {
    transform: scale(1.08);
  }

  .cortex-archive-root.drop-active :global(.cortex-archive-trigger) {
    color: var(--constellation-system-chrome-active-text);
    border-color: var(--constellation-system-chrome-active-border);
    background: var(--constellation-system-chrome-active-background);
    box-shadow:
      var(--constellation-system-chrome-active-shadow),
      0 0 46px color-mix(in srgb, var(--constellation-system-chrome-active-text) 22%, transparent);
  }

  .cortex-archive-root.drop-active :global(.cortex-archive-trigger .constellation-icon-button-icon) {
    transform: scale(1.18);
  }

  .cortex-archive-menu {
    position: absolute;
    right: 0;
    bottom: calc(100% + 14px);
    width: min(340px, 88vw);
    display: grid;
    gap: 14px;
    padding: 16px;
    border-radius: 22px;
    border: 1px solid var(--constellation-notification-menu-border);
    background: var(--constellation-notification-menu-background);
    box-shadow: var(--constellation-notification-menu-shadow);
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
    z-index: 42;
  }

  .cortex-archive-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .cortex-archive-heading {
    display: grid;
    gap: 4px;
  }

  .cortex-archive-heading strong {
    color: var(--constellation-notification-title);
    font-size: 14px;
    font-weight: 600;
  }

  .cortex-archive-heading span,
  .cortex-archive-copy span {
    color: var(--constellation-notification-subtitle);
    font-size: 12px;
  }

  .cortex-archive-empty-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    min-width: 78px;
    min-height: 30px;
    padding: 0 10px;
    border-radius: 10px;
    border: 1px solid color-mix(in srgb, var(--constellation-button-destructive-border) 82%, transparent);
    background: color-mix(in srgb, var(--constellation-button-destructive-background) 74%, transparent);
    color: var(--constellation-button-destructive-text);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0;
    text-transform: uppercase;
    white-space: nowrap;
    cursor: pointer;
    transition:
      transform 160ms ease,
      border-color 160ms ease,
      background-color 160ms ease,
      opacity 160ms ease;
  }

  .cortex-archive-empty-action:hover:not(:disabled),
  .cortex-archive-empty-action:focus-visible {
    transform: translateY(-1px);
    border-color: var(--constellation-button-destructive-border-hover);
    background: var(--constellation-button-destructive-background-hover);
    outline: none;
  }

  .cortex-archive-empty-action:disabled {
    opacity: 0.44;
    cursor: not-allowed;
  }

  .cortex-archive-empty {
    padding: 18px 14px;
    border-radius: 16px;
    border: 1px dashed var(--constellation-notification-empty-border);
    color: var(--constellation-notification-empty-text);
    font-size: 12px;
    text-align: center;
  }

  .cortex-archive-list {
    display: grid;
    gap: 8px;
    max-height: min(50vh, 380px);
    overflow: auto;
  }

  .cortex-archive-section-label {
    margin: 6px 2px 0;
    color: var(--constellation-notification-subtitle);
    font-family: var(--constellation-font-mono);
    font-size: 9px;
    font-weight: 680;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  .cortex-archive-row {
    display: grid;
    grid-template-columns: 34px minmax(0, 1fr);
    gap: 12px;
    align-items: center;
    width: 100%;
    padding: 12px;
    border: 1px solid var(--constellation-notification-row-border);
    border-radius: 16px;
    background: var(--constellation-notification-row-background);
    color: inherit;
    text-align: left;
    cursor: pointer;
    transition:
      transform 180ms ease,
      border-color 180ms ease,
      background-color 180ms ease;
  }

  .cortex-archive-row:hover,
  .cortex-archive-row:focus-visible {
    transform: translateY(-1px);
    border-color: var(--constellation-notification-row-hover-border);
    background: var(--constellation-notification-row-hover-background);
    outline: none;
  }

  .cortex-archive-avatar {
    display: inline-flex;
    width: 34px;
    height: 34px;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    color: var(--constellation-notification-avatar-text);
    font-size: 12px;
    font-weight: 700;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.16);
  }

  .cortex-archive-avatar--app {
    color: rgba(255, 255, 255, 0.9);
  }

  .cortex-archive-copy {
    display: grid;
    min-width: 0;
    gap: 4px;
  }

  .cortex-archive-copy strong {
    overflow: hidden;
    color: var(--constellation-notification-title);
    font-size: 13px;
    font-weight: 600;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (prefers-reduced-motion: reduce) {
    .cortex-archive-trigger-shell,
    .cortex-archive-trigger-shell :global(.cortex-archive-trigger),
    .cortex-archive-trigger-shell :global(.cortex-archive-trigger)::before,
    .cortex-archive-trigger-shell :global(.cortex-archive-trigger .constellation-icon-button-icon) {
      transition-duration: 1ms;
    }
  }
</style>
