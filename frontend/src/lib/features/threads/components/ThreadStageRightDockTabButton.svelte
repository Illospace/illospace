<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';

  let {
    label,
    active = false,
    appTab = false,
    closeable = false,
    onselect,
    onclose,
  }: {
    label: string;
    active?: boolean;
    appTab?: boolean;
    closeable?: boolean;
    onselect?: () => void;
    onclose?: () => void;
  } = $props();

  function handleClose(event: MouseEvent) {
    event.stopPropagation();
    onclose?.();
  }
</script>

{#if closeable}
  <span
    class="right-dock-tab-button"
    class:is-active={active}
    role="presentation"
  >
    <button
      type="button"
      class="right-dock-tab-button-close"
      aria-label={`Close ${label}`}
      title={`Close ${label}`}
      onclick={handleClose}
    >
      <ConstellationIcon name="close" size={10} stroke={2.2} />
    </button>
    <button
      type="button"
      class="right-dock-tab-button-tab"
      class:is-app-tab={appTab}
      class:is-active={active}
      role="tab"
      aria-selected={active}
      title={label}
      onclick={onselect}
    >
      <span class="right-dock-tab-button-label">{label}</span>
    </button>
  </span>
{:else}
  <button
    type="button"
    class="right-dock-tab-button-tab is-standalone"
    class:is-app-tab={appTab}
    class:is-active={active}
    role="tab"
    aria-selected={active}
    title={label}
    onclick={onselect}
  >
    <span class="right-dock-tab-button-label">{label}</span>
  </button>
{/if}

<style>
  .right-dock-tab-button-tab {
    position: relative;
    display: inline-flex;
    align-items: center;
    min-width: 0;
    min-height: 28px;
    padding: 0 8px;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: var(--constellation-utility-panel-tab-text);
    font-family: var(--constellation-font-sans, var(--font-sans, system-ui, sans-serif));
    font-size: 13px;
    font-weight: 600;
    line-height: 1;
    letter-spacing: 0;
    text-transform: none;
    cursor: pointer;
    transition:
      background-color 160ms ease,
      color 180ms ease,
      opacity 180ms ease;
  }

  .right-dock-tab-button-tab.is-app-tab {
    min-width: 0;
    justify-content: flex-start;
  }

  .right-dock-tab-button {
    display: inline-flex;
    min-width: 0;
    max-width: min(190px, 30vw);
    height: 28px;
    align-items: center;
    gap: 0;
    padding: 0 6px;
    border-radius: 999px;
    background: transparent;
    color: var(--constellation-utility-panel-tab-text);
    transition:
      background-color 160ms ease,
      color 160ms ease;
  }

  .right-dock-tab-button:hover,
  .right-dock-tab-button:focus-within,
  .right-dock-tab-button.is-active {
    background: var(--right-dock-tab-active-background);
  }

  .right-dock-tab-button:hover,
  .right-dock-tab-button:focus-within {
    color: var(--constellation-utility-panel-tab-hover-text);
  }

  .right-dock-tab-button.is-active {
    color: var(--constellation-utility-panel-tab-active-text);
  }

  .right-dock-tab-button-label {
    display: inline-block;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .right-dock-tab-button .right-dock-tab-button-tab {
    min-width: 0;
    flex: 1 1 auto;
    justify-content: flex-start;
    min-height: 28px;
    padding: 0 4px;
    color: inherit;
    background: transparent;
  }

  .right-dock-tab-button-tab.is-standalone:hover,
  .right-dock-tab-button-tab.is-standalone.is-active {
    background: var(--right-dock-tab-active-background);
  }

  .right-dock-tab-button-close {
    position: relative;
    z-index: 1;
    display: inline-flex;
    width: 0;
    min-width: 0;
    height: 18px;
    flex: 0 0 auto;
    align-items: center;
    justify-content: center;
    padding: 0;
    margin: 0;
    overflow: hidden;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: currentColor;
    cursor: pointer;
    opacity: 0;
    pointer-events: none;
    transform: scale(0.88);
    transition:
      width 150ms ease,
      margin-right 150ms ease,
      opacity 150ms ease,
      transform 150ms ease,
      color 150ms ease;
  }

  .right-dock-tab-button-close:hover,
  .right-dock-tab-button-close:focus-visible {
    color: var(--constellation-utility-panel-tab-active-text);
    opacity: 1;
  }

  .right-dock-tab-button-close:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .right-dock-tab-button-close :global(svg) {
    width: 10px;
    height: 10px;
    flex: 0 0 auto;
  }

  .right-dock-tab-button:hover .right-dock-tab-button-close,
  .right-dock-tab-button:focus-within .right-dock-tab-button-close {
    width: 14px;
    margin-right: 3px;
    opacity: 0.82;
    pointer-events: auto;
    transform: scale(1);
  }

  .right-dock-tab-button-tab:hover {
    color: var(--constellation-utility-panel-tab-hover-text);
  }

  .right-dock-tab-button-tab.is-active {
    color: var(--constellation-utility-panel-tab-active-text);
  }

  .right-dock-tab-button-tab:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 4px;
  }
</style>
