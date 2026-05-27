<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import {
    ConstellationIcon,
    ConstellationIconButton,
  } from '$lib/components/constellation';
  import ThreadStageRightDockTabButton from './ThreadStageRightDockTabButton.svelte';
  import {
    createDefaultThreadSidePanelTabs,
    threadSidePanelIconForKind,
    type ThreadStageRightDockAddMenuItem,
    type ThreadStageRightDockTab,
    type ThreadStageRightDockTabKind,
  } from '$lib/features/threads/controllers/threadSidePanelController';

  type DockWidth = number | string;
  const PANE_LABEL_BY_KIND = {
    browser: 'Browser',
    preview: 'Preview',
    discussion: 'Discussion',
    activity: 'Activity',
    'handoff-summary': 'Handoff summary',
    project: 'Project draft state',
    app: 'Generated apps',
    vault: 'Vault',
    cycles: 'Cycles',
    'code-review': 'Review changed files',
  } satisfies Record<ThreadStageRightDockTabKind, string>;

  function clamp(value: number, min: number, max: number) {
    return Math.min(max, Math.max(min, value));
  }

  function toCssLength(value: DockWidth | null | undefined, fallback: string): string {
    if (value === null || value === undefined || value === '') {
      return fallback;
    }

    return typeof value === 'number' ? `${value}px` : value;
  }

  let {
    activeTabId = 'activity',
    tabs = createDefaultThreadSidePanelTabs(),
    addMenuItems = [],
    width = 432,
    minWidth = 344,
    maxWidth = 620,
    resizable = false,
    onWidthChange,
    onTabChange,
    onTabClose,
    onAddMenuItem,
    label = 'Thread side panel',
    className,
    browserPane,
    previewPane,
    discussionPane,
    utilityPane,
    projectPane,
    appsPane,
    vaultPane,
    cyclesPane,
    codeReviewPane,
    empty,
  }: {
    activeTabId?: string | null;
    tabs?: ReadonlyArray<ThreadStageRightDockTab>;
    addMenuItems?: ReadonlyArray<ThreadStageRightDockAddMenuItem>;
    width?: DockWidth;
    minWidth?: number;
    maxWidth?: number;
    resizable?: boolean;
    onWidthChange?: (width: number) => void;
    onTabChange?: (tabId: string) => void;
    onTabClose?: (tabId: string) => void;
    onAddMenuItem?: (item: ThreadStageRightDockAddMenuItem) => void;
    label?: string;
    className?: string;
    browserPane?: Snippet;
    previewPane?: Snippet;
    discussionPane?: Snippet;
    utilityPane?: Snippet;
    projectPane?: Snippet;
    appsPane?: Snippet;
    vaultPane?: Snippet;
    cyclesPane?: Snippet;
    codeReviewPane?: Snippet;
    empty?: Snippet;
  } = $props();

  const paneByKind = $derived<Record<ThreadStageRightDockTabKind, Snippet | undefined>>({
    browser: browserPane,
    preview: previewPane,
    discussion: discussionPane,
    activity: utilityPane,
    'handoff-summary': utilityPane,
    project: projectPane,
    app: appsPane,
    vault: vaultPane,
    cycles: cyclesPane,
    'code-review': codeReviewPane,
  });
  const availableTabs = $derived(tabs.filter((tab) => Boolean(paneByKind[tab.kind])));
  const resolvedActiveTab = $derived(
    availableTabs.find((tab) => tab.id === activeTabId) ?? availableTabs[0] ?? null,
  );
  const resolvedActivePane = $derived(
    resolvedActiveTab ? paneByKind[resolvedActiveTab.kind] : undefined,
  );
  const resolvedActivePaneClass = $derived(
    resolvedActiveTab
      ? `right-dock-pane ${paneClassForKind(resolvedActiveTab.kind)}`
      : 'right-dock-pane',
  );
  const resolvedActivePaneLabel = $derived(
    resolvedActiveTab ? PANE_LABEL_BY_KIND[resolvedActiveTab.kind] : '',
  );
  const hasDockContent = $derived(Boolean(resolvedActiveTab));
  const hasAddMenu = $derived(addMenuItems.length > 0);
  const hasEmptyState = $derived(!hasDockContent && (!!empty || hasAddMenu));
  const dockClass = $derived(
    ['cortex-thread-stage-right-dock', hasEmptyState ? 'is-empty' : '', className ?? '']
      .filter(Boolean)
      .join(' '),
  );
  let internalWidth = $state(560);
  const resolvedWidth = $derived(typeof width === 'number' ? internalWidth : width);
  const dockStyle = $derived(
    `--cortex-thread-stage-right-dock-width:${toCssLength(resolvedWidth, '432px')}`,
  );
  let resizeDrag = $state<{ pointerId: number; startX: number; startWidth: number } | null>(null);
  let addMenuOpen = $state(false);

  $effect(() => {
    if (typeof width === 'number') {
      internalWidth = width;
    }
  });

  $effect(() => {
    if (!resizeDrag || !resizable) {
      return;
    }

    function handlePointerMove(event: PointerEvent) {
      if (!resizeDrag) return;
      const nextWidth = clamp(resizeDrag.startWidth + (resizeDrag.startX - event.clientX), minWidth, maxWidth);
      internalWidth = nextWidth;
      onWidthChange?.(nextWidth);
    }

    function handlePointerUp(event: PointerEvent) {
      if (!resizeDrag || resizeDrag.pointerId !== event.pointerId) return;
      resizeDrag = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerUp);

    return () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  });

  function handleResizeStart(event: PointerEvent) {
    if (!resizable) return;
    resizeDrag = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startWidth: typeof width === 'number' ? width : internalWidth,
    };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }

  function handleResizeKeydown(event: KeyboardEvent) {
    if (!resizable) return;
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;

    event.preventDefault();
    const step = event.shiftKey ? 40 : 16;
    const delta = event.key === 'ArrowLeft' ? step : -step;
    const nextWidth = clamp((typeof width === 'number' ? width : internalWidth) + delta, minWidth, maxWidth);
    internalWidth = nextWidth;
    onWidthChange?.(nextWidth);
  }

  function handleTabChange(nextTabId: string) {
    if (!availableTabs.some((tab) => tab.id === nextTabId)) return;
    addMenuOpen = false;
    onTabChange?.(nextTabId);
  }

  function handleTabClose(tabId: string) {
    addMenuOpen = false;
    onTabClose?.(tabId);
  }

  function handleAddMenuItemClick(item: ThreadStageRightDockAddMenuItem) {
    if (item.disabled) return;
    addMenuOpen = false;
    onAddMenuItem?.(item);
  }

  function paneClassForKind(kind: ThreadStageRightDockTabKind) {
    return `right-dock-${kind === 'app' ? 'apps' : kind}`;
  }

  function handleDocumentClick(event: MouseEvent) {
    if (!addMenuOpen) return;
    const target = event.target as HTMLElement | null;
    if (target?.closest('.right-dock-add')) return;
    addMenuOpen = false;
  }

  onMount(() => {
    document.addEventListener('click', handleDocumentClick);
  });

  onDestroy(() => {
    document.removeEventListener('click', handleDocumentClick);
  });
</script>

{#if hasDockContent || hasEmptyState || hasAddMenu}
  <aside
    class={dockClass}
    style={dockStyle}
    aria-label={label}
    data-design-composition="ConstellationThreadStageScreen"
    data-dock-state={resolvedActiveTab?.kind ?? 'empty'}
  >
    {#if resizable}
      <button
        type="button"
        class="right-dock-resize-handle"
        class:is-resizing={Boolean(resizeDrag)}
        aria-label="Resize thread side panel"
        onpointerdown={handleResizeStart}
        onkeydown={handleResizeKeydown}
      >
        <span class="right-dock-resize-grip" aria-hidden="true"></span>
      </button>
    {/if}

    <div class="right-dock-surface">
      <header class="right-dock-header">
        <div class="right-dock-tab-strip">
          <div class="right-dock-tabs" role="tablist" aria-label="Thread side panel tabs">
            {#each availableTabs as tab (tab.id)}
              {@const isActive = tab.id === resolvedActiveTab?.id}
              <ThreadStageRightDockTabButton
                label={tab.label}
                active={isActive}
                appTab={tab.kind === 'app'}
                closeable={Boolean(tab.closeable)}
                onselect={() => handleTabChange(tab.id)}
                onclose={() => handleTabClose(tab.id)}
              />
            {/each}
          </div>

          {#if hasAddMenu}
            <div class="right-dock-add">
              <ConstellationIconButton
                className="right-dock-add-button"
                label="Add side panel tab"
                size="sm"
                expanded={addMenuOpen}
                popup="menu"
                pressed={addMenuOpen}
                title="Add side panel tab"
                onclick={() => (addMenuOpen = !addMenuOpen)}
              >
                <ConstellationIcon name="plus" size={16} stroke={1.8} />
              </ConstellationIconButton>
              {#if addMenuOpen}
                <div class="right-dock-add-menu" role="menu" aria-label="Add side panel tab">
                  <div class="right-dock-add-menu-label">Open in panel</div>
                  {#each addMenuItems as item (item.id)}
                    <button
                      type="button"
                      class="right-dock-add-item"
                      disabled={item.disabled}
                      role="menuitem"
                      onclick={() => handleAddMenuItemClick(item)}
                    >
                      <span class="right-dock-add-item-icon" aria-hidden="true">
                        <ConstellationIcon name={threadSidePanelIconForKind(item.kind)} size={14} stroke={1.8} />
                      </span>
                      <span class="right-dock-add-item-copy">
                        <strong>{item.label}</strong>
                        {#if item.description}
                          <small>{item.description}</small>
                        {/if}
                      </span>
                    </button>
                  {/each}
                </div>
              {/if}
            </div>
          {/if}
        </div>

      </header>

      <div class="right-dock-content" data-active-tab={resolvedActiveTab?.kind ?? 'empty'}>
        {#if resolvedActivePane}
          <section class={resolvedActivePaneClass} aria-label={resolvedActivePaneLabel}>
            {@render resolvedActivePane()}
          </section>
        {:else if hasEmptyState}
          <div class="right-dock-empty">
            {#if empty}
              {@render empty()}
            {:else}
              <span>No panel open</span>
            {/if}
          </div>
        {/if}
      </div>
    </div>
  </aside>
{/if}

<style>
  .cortex-thread-stage-right-dock {
    --cortex-thread-stage-right-dock-width: 432px;
    --right-dock-tab-active-background: rgba(255, 255, 255, 0.065);
    --right-dock-add-menu-border: rgba(255, 255, 255, 0.11);
    --right-dock-add-menu-background: rgba(13, 17, 26, 0.98);
    --right-dock-add-menu-shadow:
      0 18px 46px rgba(0, 0, 0, 0.42),
      inset 0 1px 0 rgba(255, 255, 255, 0.06);
    --right-dock-add-menu-label-text: rgba(244, 246, 250, 0.48);
    --right-dock-add-item-text: rgba(244, 246, 250, 0.84);
    --right-dock-add-item-hover-background: rgba(255, 255, 255, 0.06);
    --right-dock-add-item-icon-background: rgba(255, 255, 255, 0.055);
    --right-dock-add-item-icon-text: rgba(141, 183, 255, 0.86);
    --right-dock-muted-text: rgba(244, 246, 250, 0.46);
    --right-dock-resize-handle-background: transparent;
    --right-dock-resize-grip-background: rgba(255, 255, 255, 0.24);
    --right-dock-resize-grip-shadow: none;
    width: min(var(--cortex-thread-stage-right-dock-width), 100%);
    max-width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
    display: flex;
    align-items: stretch;
    pointer-events: auto;
    position: relative;
  }

  :global(:root[data-color-scheme='light']) .cortex-thread-stage-right-dock {
    --right-dock-tab-active-background: rgba(49, 63, 76, 0.065);
    --right-dock-add-menu-border: var(--constellation-surface-floating-border);
    --right-dock-add-menu-background: var(--constellation-surface-floating-background);
    --right-dock-add-menu-shadow: var(--constellation-surface-floating-shadow);
    --right-dock-add-menu-label-text: rgba(82, 98, 111, 0.66);
    --right-dock-add-item-text: rgba(49, 63, 76, 0.86);
    --right-dock-add-item-hover-background: rgba(49, 63, 76, 0.065);
    --right-dock-add-item-icon-background: rgba(49, 63, 76, 0.07);
    --right-dock-add-item-icon-text: #18212a;
    --right-dock-muted-text: rgba(82, 98, 111, 0.66);
    --right-dock-resize-handle-background: transparent;
    --right-dock-resize-grip-background: rgba(126, 92, 52, 0.2);
    --right-dock-resize-grip-shadow: none;
  }

  .right-dock-surface {
    position: relative;
    width: 100%;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    border-radius: 24px;
    border: 1px solid var(--constellation-utility-panel-surface-border);
    background: var(--constellation-utility-panel-surface-background);
    box-shadow: var(--constellation-utility-panel-surface-shadow);
    overflow: hidden;
    backdrop-filter: blur(16px) saturate(1.04);
    -webkit-backdrop-filter: blur(16px) saturate(1.04);
  }

  .right-dock-surface::before {
    content: '';
    position: absolute;
    inset: 0;
    background: var(--constellation-utility-panel-surface-highlight);
    pointer-events: none;
  }

  .cortex-thread-stage-right-dock.is-stage-integrated .right-dock-surface {
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .cortex-thread-stage-right-dock.is-stage-integrated .right-dock-surface::before {
    content: none;
  }

  .right-dock-header,
  .right-dock-content {
    position: relative;
    z-index: 1;
  }

  .right-dock-header {
    z-index: 5;
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    min-height: 46px;
    padding: 0 10px 0 12px;
    border-bottom: 1px solid var(--constellation-utility-panel-header-border);
  }

  .right-dock-tab-strip {
    flex: 1 1 auto;
    min-width: 0;
    height: 100%;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    overflow: visible;
  }

  .right-dock-tabs {
    flex: 0 1 auto;
    min-width: 0;
    height: 100%;
    display: flex;
    align-items: center;
    gap: 4px;
    overflow-x: auto;
    scrollbar-width: none;
  }

  .right-dock-tabs::-webkit-scrollbar {
    display: none;
  }

  .right-dock-add {
    position: relative;
    display: inline-flex;
    flex: 0 0 auto;
  }

  .right-dock-add :global(.right-dock-add-button.constellation-icon-button-sm) {
    flex: 0 0 auto;
  }

  .right-dock-add-menu {
    position: absolute;
    top: calc(100% + 10px);
    left: 0;
    z-index: 8;
    display: grid;
    width: min(280px, 76vw);
    max-height: min(420px, 64vh);
    padding: 8px;
    overflow-y: auto;
    border: 1px solid var(--right-dock-add-menu-border);
    border-radius: 14px;
    background: var(--right-dock-add-menu-background);
    box-shadow: var(--right-dock-add-menu-shadow);
    backdrop-filter: blur(18px) saturate(1.08);
    -webkit-backdrop-filter: blur(18px) saturate(1.08);
    scrollbar-color: var(--constellation-utility-panel-scrollbar) transparent;
  }

  .right-dock-add-menu::-webkit-scrollbar {
    width: 4px;
  }

  .right-dock-add-menu::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: var(--constellation-utility-panel-scrollbar);
  }

  .right-dock-add-menu-label {
    padding: 5px 7px 8px;
    color: var(--right-dock-add-menu-label-text);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 680;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .right-dock-add-item {
    display: grid;
    grid-template-columns: 30px minmax(0, 1fr);
    gap: 9px;
    width: 100%;
    min-width: 0;
    align-items: center;
    padding: 8px 7px;
    border: 0;
    border-radius: 10px;
    background: transparent;
    color: var(--right-dock-add-item-text);
    text-align: left;
    cursor: pointer;
  }

  .right-dock-add-item:hover:not(:disabled) {
    background: var(--right-dock-add-item-hover-background);
  }

  .right-dock-add-item:disabled {
    cursor: default;
    opacity: 0.5;
  }

  .right-dock-add-item:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .right-dock-add-item-icon {
    display: inline-flex;
    width: 28px;
    height: 28px;
    align-items: center;
    justify-content: center;
    border-radius: 9px;
    background: var(--right-dock-add-item-icon-background);
    color: var(--right-dock-add-item-icon-text);
  }

  .right-dock-add-item-copy {
    display: grid;
    min-width: 0;
    gap: 3px;
  }

  .right-dock-add-item-copy strong,
  .right-dock-add-item-copy small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .right-dock-add-item-copy strong {
    font-size: 12px;
    font-weight: 640;
    letter-spacing: 0;
  }

  .right-dock-add-item-copy small {
    color: var(--right-dock-muted-text);
    font-size: 11px;
    line-height: 1.25;
  }

  .right-dock-tab:hover {
    color: var(--constellation-utility-panel-tab-hover-text);
  }

  .right-dock-tab.is-active {
    color: var(--constellation-utility-panel-tab-active-text);
  }

  .right-dock-tab:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 4px;
  }

  .right-dock-content {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    overflow: hidden;
    padding: 0;
  }

  .right-dock-content[data-active-tab='activity'],
  .right-dock-content[data-active-tab='handoff-summary'],
  .right-dock-content[data-active-tab='project'],
  .right-dock-content[data-active-tab='vault'],
  .right-dock-content[data-active-tab='cycles'] {
    overflow-y: auto;
    padding: 8px 10px 10px;
    scrollbar-color: var(--constellation-utility-panel-scrollbar) transparent;
  }

  .right-dock-content[data-active-tab='app'] {
    overflow: hidden;
  }

  .right-dock-content[data-active-tab='discussion'] {
    overflow: hidden;
    padding: 0;
  }

  .right-dock-pane,
  .right-dock-empty {
    flex: 1 1 auto;
    width: 100%;
    min-width: 0;
    min-height: 0;
  }

  .right-dock-pane {
    display: flex;
    overflow: hidden;
  }

  .right-dock-pane :global(*) {
    min-width: 0;
  }

  .right-dock-pane :global([data-cortex-migration-surface]) {
    flex: 1 1 auto;
    min-height: 0;
  }

  .right-dock-activity {
    overflow: visible;
  }

  .right-dock-discussion {
    overflow: hidden;
  }

  .right-dock-handoff-summary {
    overflow: visible;
  }

  .right-dock-project {
    align-items: stretch;
    overflow: visible;
  }

  .right-dock-project > :global(*) {
    flex: 1 1 auto;
    width: 100%;
    min-width: 0;
  }

  .right-dock-vault {
    overflow: visible;
  }

  .right-dock-cycles {
    overflow: visible;
  }

  .right-dock-apps {
    overflow: hidden;
  }

  .right-dock-content[data-active-tab='activity']::-webkit-scrollbar,
  .right-dock-content[data-active-tab='handoff-summary']::-webkit-scrollbar,
  .right-dock-content[data-active-tab='project']::-webkit-scrollbar,
  .right-dock-content[data-active-tab='vault']::-webkit-scrollbar,
  .right-dock-content[data-active-tab='cycles']::-webkit-scrollbar {
    width: 4px;
  }

  .right-dock-content[data-active-tab='activity']::-webkit-scrollbar-thumb,
  .right-dock-content[data-active-tab='handoff-summary']::-webkit-scrollbar-thumb,
  .right-dock-content[data-active-tab='project']::-webkit-scrollbar-thumb,
  .right-dock-content[data-active-tab='vault']::-webkit-scrollbar-thumb,
  .right-dock-content[data-active-tab='cycles']::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: var(--constellation-utility-panel-scrollbar);
  }

  .right-dock-browser :global(.browser-window-shell) {
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
  }

  .right-dock-browser :global(.window-bar),
  .right-dock-browser :global(.browser-nav-bar) {
    background: transparent;
  }

  .right-dock-browser :global(.browser-nav-bar) {
    min-height: 46px;
    padding-inline: 12px;
  }

  .right-dock-browser :global(.browser-content) {
    padding: 10px 12px 12px;
  }

  .right-dock-browser :global(.browser-stage) {
    min-height: 240px;
    margin-bottom: 10px;
    border-radius: 10px;
    box-shadow: none;
  }

  .right-dock-browser :global(.browser-advanced-panel) {
    margin-bottom: 8px;
    border-radius: 0;
    border-width: 1px 0 0;
    background: transparent;
  }

  .right-dock-browser :global(.browser-advanced-summary) {
    padding: 10px 0;
  }

  .right-dock-browser :global(.browser-advanced-body) {
    padding: 0 0 10px;
  }

  .right-dock-browser :global(.utility-panel) {
    padding: 10px 0;
    border-width: 1px 0 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
  }

  .right-dock-activity :global(.panel-utility-content-bare) {
    width: 100%;
    min-height: 0;
  }

  .right-dock-activity :global(.activity-list) {
    width: 100%;
  }

  .right-dock-activity :global(.activity-list-item) {
    margin: 0;
    background: transparent;
  }

  .right-dock-activity :global(.activity-list-item:last-child) {
    border-bottom-color: transparent;
  }

  .right-dock-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--right-dock-muted-text);
    font-size: 12px;
  }

  .right-dock-empty :global(*) {
    min-width: 0;
    min-height: 0;
  }

  .right-dock-resize-handle {
    position: absolute;
    left: -14px;
    top: 16px;
    bottom: 16px;
    width: 28px;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: col-resize;
    z-index: 3;
    display: flex;
    align-items: center;
    justify-content: center;
    touch-action: none;
  }

  .right-dock-resize-handle::before {
    content: '';
    position: absolute;
    top: 50%;
    left: 50%;
    width: 8px;
    height: 84px;
    border-radius: 999px;
    transform: translate(-50%, -50%);
    background: var(--right-dock-resize-handle-background);
    opacity: 0;
    transition: opacity 180ms ease;
  }

  .right-dock-resize-handle:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring, rgba(240, 240, 250, 0.52));
    outline-offset: 2px;
    border-radius: 8px;
  }

  .right-dock-resize-grip {
    display: block;
    width: 4px;
    height: 76px;
    border-radius: 999px;
    background: var(--right-dock-resize-grip-background);
    box-shadow: var(--right-dock-resize-grip-shadow);
    opacity: 0.46;
    transition: opacity 180ms ease, transform 180ms ease;
  }

  .cortex-thread-stage-right-dock.is-stage-integrated .right-dock-resize-grip {
    box-shadow: none;
    opacity: 0;
  }

  .right-dock-resize-handle:hover::before,
  .right-dock-resize-handle:focus-visible::before,
  .right-dock-resize-handle.is-resizing::before {
    opacity: 1;
  }

  .right-dock-resize-handle:hover .right-dock-resize-grip,
  .right-dock-resize-handle:focus-visible .right-dock-resize-grip,
  .right-dock-resize-handle.is-resizing .right-dock-resize-grip {
    opacity: 0.78;
    transform: scaleX(1.22);
  }


  @media (max-width: 1179px) {
    .right-dock-resize-handle {
      display: none;
    }

    .cortex-thread-stage-right-dock {
      width: 100%;
    }
  }
</style>
