<script lang="ts">
  import { onMount } from 'svelte';
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { CortexWorkspacePoint } from '$lib/features/workspace-scene/domain/workspacePoint';

  export type CortexWorkspaceMenuAnchor = CortexWorkspacePoint;

  let {
    anchor,
    onnewpin,
    onclose,
  }: {
    anchor: CortexWorkspaceMenuAnchor;
    onnewpin?: (point: CortexWorkspacePoint) => void;
    onclose?: () => void;
  } = $props();

  let viewportWidth = $state(1280);
  let viewportHeight = $state(800);

  const menuStyle = $derived.by(() => {
    const width = 210;
    const height = 58;
    const left = clamp(anchor.screenX + 10, 12, Math.max(12, viewportWidth - width - 12));
    const top = clamp(anchor.screenY + 10, 12, Math.max(12, viewportHeight - height - 12));
    return `left:${left}px; top:${top}px;`;
  });

  onMount(() => {
    updateViewport();
    window.addEventListener('resize', updateViewport);
    window.addEventListener('keydown', handleKeydown);
    return () => {
      window.removeEventListener('resize', updateViewport);
      window.removeEventListener('keydown', handleKeydown);
    };
  });

  function clamp(value: number, min: number, max: number) {
    return Math.min(max, Math.max(min, value));
  }

  function updateViewport() {
    viewportWidth = window.innerWidth;
    viewportHeight = window.innerHeight;
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onclose?.();
  }

  function createPin() {
    onnewpin?.(anchor);
  }
</script>

<div class="cortex-workspace-menu-layer">
  <button
    type="button"
    class="cortex-workspace-menu-backdrop"
    aria-label="Close workspace menu"
    onclick={() => onclose?.()}
  ></button>

  <section class="cortex-workspace-menu" style={menuStyle} aria-label="Workspace menu">
    <button type="button" class="cortex-workspace-menu-item" onclick={createPin}>
      <span class="cortex-workspace-menu-item__icon">
        <ConstellationIcon name="plus" size={15} stroke={2.2} />
      </span>
      <span>New Pin</span>
    </button>
  </section>
</div>

<style>
  .cortex-workspace-menu-layer {
    position: fixed;
    inset: 0;
    z-index: 221;
    pointer-events: none;
  }

  .cortex-workspace-menu-backdrop {
    position: absolute;
    inset: 0;
    border: 0;
    background: transparent;
    cursor: default;
    pointer-events: auto;
  }

  .cortex-workspace-menu {
    --workspace-menu-background:
      linear-gradient(180deg, rgba(255, 255, 255, 0.08), transparent 48%),
      color-mix(in srgb, var(--constellation-surface-panel-background) 94%, rgba(6, 8, 14, 0.88));
    --workspace-menu-shadow:
      0 18px 46px rgba(0, 0, 0, 0.28),
      inset 0 1px 0 rgba(255, 255, 255, 0.08);
    position: absolute;
    width: 210px;
    padding: 6px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 14px;
    background: var(--workspace-menu-background);
    box-shadow: var(--workspace-menu-shadow);
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    color: var(--constellation-color-text-primary);
    pointer-events: auto;
  }

  .cortex-workspace-menu-item {
    display: flex;
    align-items: center;
    width: 100%;
    min-height: 42px;
    gap: 10px;
    border: 0;
    border-radius: 10px;
    padding: 0 10px;
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: 13px;
    font-weight: 650;
    letter-spacing: 0;
    text-align: left;
    cursor: pointer;
  }

  .cortex-workspace-menu-item:hover,
  .cortex-workspace-menu-item:focus-visible {
    background: color-mix(in srgb, var(--constellation-color-spectral-owner) 12%, transparent);
    outline: none;
  }

  .cortex-workspace-menu-item__icon {
    display: grid;
    place-items: center;
    width: 26px;
    height: 26px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--constellation-color-amber-owner) 18%, transparent);
    color: color-mix(in srgb, var(--constellation-color-amber-owner) 74%, var(--constellation-color-text-primary));
  }

  :global(:root[data-color-scheme='light']) .cortex-workspace-menu {
    --workspace-menu-background:
      linear-gradient(180deg, rgba(255, 255, 255, 0.84), rgba(255, 255, 255, 0.52)),
      color-mix(in srgb, var(--constellation-surface-panel-background) 96%, rgba(255, 253, 250, 0.94));
    --workspace-menu-shadow:
      0 18px 42px rgba(73, 54, 38, 0.14),
      inset 0 1px 0 rgba(255, 255, 255, 0.8);
  }
</style>
