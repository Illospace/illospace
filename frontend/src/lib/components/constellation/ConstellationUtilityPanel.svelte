<script lang="ts">
  import type { Snippet } from 'svelte';
  import ConstellationIcon from './ConstellationIcon.svelte';

  export type ConstellationUtilityPanelTab = {
    id: string;
    label: string;
  };

  type Props = {
    open: boolean;
    tabs: ReadonlyArray<ConstellationUtilityPanelTab>;
    activeTab: string | null;
    onTabSelect: (tabId: string) => void;
    onClose: () => void;
    className?: string;
    children?: Snippet;
  };

  let {
    open,
    tabs,
    activeTab,
    onTabSelect,
    onClose,
    className = '',
    children,
  }: Props = $props();

  const rootClass = $derived(['constellation-utility-panel', open ? 'is-open' : '', className].filter(Boolean).join(' '));
</script>

<aside class={rootClass} aria-hidden={!open}>
  <div class="constellation-utility-panel-surface">
    <div class="constellation-utility-panel-header">
      <div class="constellation-utility-panel-tabs" role="tablist" aria-label="Thread utilities">
        {#each tabs as tab (tab.id)}
          <button
            type="button"
            class={`constellation-utility-panel-tab ${activeTab === tab.id ? 'is-active' : ''}`}
            onclick={() => onTabSelect(tab.id)}
            role="tab"
            aria-selected={activeTab === tab.id}
          >
            {tab.label}
          </button>
        {/each}
      </div>

      <button
        type="button"
        class="constellation-utility-panel-close-button"
        onclick={onClose}
        aria-label="Close utility panel"
      >
        <ConstellationIcon name="close" size={12} stroke={1.8} />
      </button>
    </div>

    <div class="constellation-utility-panel-content">
      {#if open && children}
        {@render children()}
      {/if}
    </div>
  </div>
</aside>

<style>
  .constellation-utility-panel {
    width: 100%;
    min-height: 0;
    height: 100%;
    box-sizing: border-box;
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
    transform: translateX(14px);
    transition:
      opacity 180ms ease,
      transform 240ms cubic-bezier(0.22, 1, 0.36, 1),
      visibility 180ms ease;
  }

  .constellation-utility-panel.is-open {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
    transform: translateX(0);
  }

  .constellation-utility-panel-surface {
    position: relative;
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
    border-radius: 24px;
    border: 1px solid var(--constellation-utility-panel-surface-border);
    background: var(--constellation-utility-panel-surface-background);
    box-shadow: var(--constellation-utility-panel-surface-shadow);
    overflow: hidden;
    backdrop-filter: blur(16px) saturate(1.04);
    -webkit-backdrop-filter: blur(16px) saturate(1.04);
  }

  .constellation-utility-panel-surface::before {
    content: '';
    position: absolute;
    inset: 0;
    background: var(--constellation-utility-panel-surface-highlight);
    pointer-events: none;
  }

  .constellation-utility-panel-header,
  .constellation-utility-panel-content {
    position: relative;
    z-index: 1;
  }

  .constellation-utility-panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 16px 12px;
    border-bottom: 1px solid var(--constellation-utility-panel-header-border);
  }

  .constellation-utility-panel-tabs {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }

  .constellation-utility-panel-tab {
    border: 0;
    padding: 0 0 4px;
    background: transparent;
    color: var(--constellation-utility-panel-tab-text);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    border-bottom: 1px solid transparent;
    cursor: pointer;
    transition:
      color 180ms ease,
      border-color 180ms ease,
      opacity 180ms ease;
  }

  .constellation-utility-panel-tab:hover {
    color: var(--constellation-utility-panel-tab-hover-text);
  }

  .constellation-utility-panel-tab.is-active {
    color: var(--constellation-utility-panel-tab-active-text);
    border-color: var(--constellation-utility-panel-tab-active-border);
  }

  .constellation-utility-panel-close-button {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--constellation-utility-panel-close-border);
    border-radius: 999px;
    background: var(--constellation-utility-panel-close-background);
    color: var(--constellation-utility-panel-close-text);
    cursor: pointer;
    transition:
      background-color 180ms ease,
      border-color 180ms ease,
      color 180ms ease,
      transform 180ms ease;
  }

  .constellation-utility-panel-close-button:hover {
    background: var(--constellation-utility-panel-close-hover-background);
    border-color: var(--constellation-utility-panel-close-hover-border);
    color: var(--constellation-utility-panel-close-hover-text);
    transform: translateY(-1px);
  }

  .constellation-utility-panel-close-button:active {
    transform: translateY(1px) scale(0.98);
  }

  .constellation-utility-panel-close-button:focus-visible,
  .constellation-utility-panel-tab:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-utility-panel-close-button :global(svg) {
    width: 12px;
    height: 12px;
  }

  .constellation-utility-panel-content {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 14px 18px 18px;
  }

  .constellation-utility-panel-content::-webkit-scrollbar {
    width: 4px;
  }

  .constellation-utility-panel-content::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: var(--constellation-utility-panel-scrollbar);
  }
</style>
