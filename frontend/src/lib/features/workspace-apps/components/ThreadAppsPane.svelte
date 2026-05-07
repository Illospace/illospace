<script lang="ts">
  import type { WorkspaceAppRead } from '$lib/features/workspace-apps/api/workspaceAppsApi';
  import { ConstellationIcon } from '$lib/components/constellation';

  import GeneratedAppRenderer from './GeneratedAppOverlay.svelte';

  let {
    apps = [],
    selectedAppId = null,
    onSelectApp,
  }: {
    apps?: WorkspaceAppRead[];
    selectedAppId?: string | null;
    onSelectApp?: (appId: string | null) => void;
  } = $props();

  const selectedApp = $derived(apps.find((app) => app.id === selectedAppId) ?? null);
</script>

<section class="thread-apps-pane" class:has-selected={!!selectedApp} aria-label="Generated apps">
  {#if selectedApp}
    <div class="thread-apps-pane__selected">
      <GeneratedAppRenderer app={selectedApp} surface="dock" onclose={() => onSelectApp?.(null)} />
    </div>
  {:else}
    <header class="thread-apps-pane__header">
      <span>Generated apps</span>
      <strong>{apps.length}</strong>
    </header>

    {#if apps.length > 0}
      <div class="thread-apps-pane__list">
        {#each apps as app (app.id)}
          <button type="button" class="thread-apps-pane__app" onclick={() => onSelectApp?.(app.id)}>
            <span class="thread-apps-pane__app-thumb" style={`--app-accent:${app.visual_spec?.accent || '#57CFA0'}`} aria-hidden="true">
              <ConstellationIcon name="code" size={15} stroke={1.9} />
            </span>
            <span class="thread-apps-pane__app-copy">
              <span>{app.name}</span>
              <small>{app.description || app.renderer_key}</small>
            </span>
            <ConstellationIcon name="forward" size={13} stroke={1.8} />
          </button>
        {/each}
      </div>
    {:else}
      <div class="thread-apps-pane__empty">
        <ConstellationIcon name="code" size={18} stroke={1.8} />
        <span>No generated apps yet.</span>
      </div>
    {/if}
  {/if}
</section>

<style>
  .thread-apps-pane {
    display: flex;
    flex: 1 1 auto;
    width: 100%;
    min-width: 0;
    min-height: 0;
    flex-direction: column;
    color: rgba(244, 246, 250, 0.9);
  }

  .thread-apps-pane__header {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 14px 11px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
    color: rgba(244, 246, 250, 0.56);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 680;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .thread-apps-pane__header strong {
    color: rgba(244, 246, 250, 0.82);
    font-size: 11px;
  }

  .thread-apps-pane__list {
    display: grid;
    gap: 1px;
    min-height: 0;
    overflow-y: auto;
    padding: 8px 10px 12px;
    scrollbar-color: var(--constellation-utility-panel-scrollbar) transparent;
  }

  .thread-apps-pane__list::-webkit-scrollbar {
    width: 4px;
  }

  .thread-apps-pane__list::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: var(--constellation-utility-panel-scrollbar);
  }

  .thread-apps-pane__app {
    display: grid;
    grid-template-columns: 38px minmax(0, 1fr) 16px;
    gap: 11px;
    width: 100%;
    align-items: center;
    min-width: 0;
    padding: 10px 4px;
    border: 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.055);
    background: transparent;
    color: inherit;
    text-align: left;
    cursor: pointer;
    transition:
      background-color 160ms ease,
      color 160ms ease;
  }

  .thread-apps-pane__app:hover {
    background: rgba(255, 255, 255, 0.04);
  }

  .thread-apps-pane__app:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .thread-apps-pane__app-thumb {
    --app-accent: #57CFA0;
    display: inline-flex;
    width: 34px;
    height: 34px;
    align-items: center;
    justify-content: center;
    border-radius: 12px;
    color: color-mix(in srgb, var(--app-accent) 52%, white 48%);
    background: color-mix(in srgb, var(--app-accent) 13%, rgba(255, 255, 255, 0.055));
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--app-accent) 20%, rgba(255, 255, 255, 0.08)),
      0 0 18px color-mix(in srgb, var(--app-accent) 12%, transparent);
  }

  .thread-apps-pane__app-copy {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .thread-apps-pane__app-copy span,
  .thread-apps-pane__app-copy small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .thread-apps-pane__app-copy span {
    color: rgba(255, 255, 255, 0.88);
    font-size: 13px;
    font-weight: 620;
    letter-spacing: 0;
  }

  .thread-apps-pane__app-copy small {
    color: rgba(244, 246, 250, 0.5);
    font-size: 11px;
    line-height: 1.25;
  }

  .thread-apps-pane__selected {
    display: flex;
    flex: 1 1 auto;
    min-height: 0;
    flex-direction: column;
  }

  .thread-apps-pane__empty {
    display: flex;
    flex: 1 1 auto;
    align-items: center;
    justify-content: center;
    gap: 9px;
    padding: 20px;
    color: rgba(244, 246, 250, 0.52);
    font-size: 12px;
  }
</style>
