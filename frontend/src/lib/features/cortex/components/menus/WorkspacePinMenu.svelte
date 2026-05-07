<script lang="ts">
  import { onMount, tick } from 'svelte';
  import type { WorkspacePinRead } from '$lib/api/client';
  import { ConstellationIcon } from '$lib/components/constellation';

  export type CortexWorkspacePinMenuAnchor = {
    x: number;
    y: number;
    pin: WorkspacePinRead;
  };

  let {
    anchor,
    saving = false,
    deleting = false,
    onrename,
    ondelete,
    onclose,
  }: {
    anchor: CortexWorkspacePinMenuAnchor;
    saving?: boolean;
    deleting?: boolean;
    onrename?: (pin: WorkspacePinRead, label: string) => void | Promise<void>;
    ondelete?: (pin: WorkspacePinRead) => void | Promise<void>;
    onclose?: () => void;
  } = $props();

  let menuEl: HTMLElement | undefined = $state();
  let label = $state('');
  let lastPinId = $state('');
  let viewportWidth = $state(1280);
  let viewportHeight = $state(800);

  const trimmedLabel = $derived(label.trim());
  const hasLabelChange = $derived(trimmedLabel && trimmedLabel !== anchor.pin.label);
  const canSave = $derived(Boolean(hasLabelChange && !saving && !deleting));
  const menuStyle = $derived.by(() => {
    const width = 286;
    const height = 168;
    const left = clamp(anchor.x + 14, 12, Math.max(12, viewportWidth - width - 12));
    const top = clamp(anchor.y - 18, 12, Math.max(12, viewportHeight - height - 12));
    return `left:${left}px; top:${top}px;`;
  });

  $effect(() => {
    const nextPinId = anchor.pin.id;
    if (lastPinId === nextPinId) return;
    lastPinId = nextPinId;
    label = anchor.pin.label;
    void tick().then(() => {
      menuEl?.querySelector<HTMLInputElement>('#cortex-workspace-pin-label')?.focus();
    });
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

  function saveLabel() {
    if (!canSave) return;
    onrename?.(anchor.pin, trimmedLabel);
  }

  function handleSubmit(event: SubmitEvent) {
    event.preventDefault();
    saveLabel();
  }
</script>

<div class="cortex-workspace-pin-menu-layer">
  <button
    type="button"
    class="cortex-workspace-pin-menu-backdrop"
    aria-label="Close pin menu"
    onclick={() => onclose?.()}
  ></button>

  <section
    class="cortex-workspace-pin-menu"
    style={menuStyle}
    bind:this={menuEl}
    aria-label="Pin menu"
  >
    <form class="cortex-workspace-pin-menu__form" onsubmit={handleSubmit}>
      <label class="cortex-workspace-pin-menu__label" for="cortex-workspace-pin-label">Pin name</label>
      <div class="cortex-workspace-pin-menu__input-row">
        <input
          id="cortex-workspace-pin-label"
          class="cortex-workspace-pin-menu__input"
          bind:value={label}
          maxlength="160"
          disabled={saving || deleting}
        />
        <button
          type="submit"
          class="cortex-workspace-pin-menu__icon-action"
          disabled={!canSave}
          aria-label="Save pin name"
        >
          <ConstellationIcon name="check" size={15} stroke={2.1} />
        </button>
      </div>
    </form>

    <div class="cortex-workspace-pin-menu__actions">
      <button
        type="button"
        class="cortex-workspace-pin-menu__action cortex-workspace-pin-menu__action--danger"
        disabled={saving || deleting}
        onclick={() => ondelete?.(anchor.pin)}
      >
        <ConstellationIcon name="x" size={15} stroke={2.2} />
        <span>{deleting ? 'Deleting' : 'Delete'}</span>
      </button>
    </div>
  </section>
</div>

<style>
  .cortex-workspace-pin-menu-layer {
    position: fixed;
    inset: 0;
    z-index: 222;
    pointer-events: none;
  }

  .cortex-workspace-pin-menu-backdrop {
    position: absolute;
    inset: 0;
    border: 0;
    background: transparent;
    cursor: default;
    pointer-events: auto;
  }

  .cortex-workspace-pin-menu {
    --workspace-pin-menu-background:
      linear-gradient(180deg, rgba(255, 255, 255, 0.08), transparent 48%),
      color-mix(in srgb, var(--constellation-surface-panel-background) 95%, rgba(6, 8, 14, 0.9));
    --workspace-pin-menu-shadow:
      0 20px 52px rgba(0, 0, 0, 0.3),
      inset 0 1px 0 rgba(255, 255, 255, 0.08);
    --workspace-pin-menu-danger-text: #e98a9c;
    --workspace-pin-menu-danger-hover-background: rgba(219, 110, 130, 0.14);
    position: absolute;
    width: 286px;
    padding: 10px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 16px;
    background: var(--workspace-pin-menu-background);
    box-shadow: var(--workspace-pin-menu-shadow);
    color: var(--constellation-color-text-primary);
    pointer-events: auto;
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
  }

  :global(:root[data-color-scheme='light']) .cortex-workspace-pin-menu {
    --workspace-pin-menu-background:
      linear-gradient(180deg, rgba(255, 255, 255, 0.86), rgba(255, 255, 255, 0.58)),
      color-mix(in srgb, var(--constellation-surface-panel-background) 96%, rgba(255, 253, 250, 0.94));
    --workspace-pin-menu-shadow:
      0 20px 46px rgba(73, 54, 38, 0.14),
      inset 0 1px 0 rgba(255, 255, 255, 0.8);
    --workspace-pin-menu-danger-text: #9f4052;
    --workspace-pin-menu-danger-hover-background: rgba(178, 78, 97, 0.12);
  }

  .cortex-workspace-pin-menu__form {
    display: grid;
    gap: 7px;
  }

  .cortex-workspace-pin-menu__label {
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 720;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .cortex-workspace-pin-menu__input-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 34px;
    gap: 8px;
    align-items: center;
  }

  .cortex-workspace-pin-menu__input {
    min-width: 0;
    height: 36px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 10px;
    padding: 0 11px;
    background: color-mix(in srgb, var(--constellation-surface-panel-background) 78%, transparent);
    color: inherit;
    font: inherit;
    font-size: 13px;
    letter-spacing: 0;
    outline: none;
  }

  .cortex-workspace-pin-menu__input:focus-visible {
    border-color: color-mix(in srgb, var(--constellation-color-spectral-owner) 46%, transparent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--constellation-color-spectral-owner) 13%, transparent);
  }

  .cortex-workspace-pin-menu__icon-action,
  .cortex-workspace-pin-menu__action {
    border: 0;
    color: inherit;
    font: inherit;
    cursor: pointer;
  }

  .cortex-workspace-pin-menu__icon-action {
    display: grid;
    place-items: center;
    width: 34px;
    height: 34px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--constellation-color-spectral-owner) 20%, transparent);
  }

  .cortex-workspace-pin-menu__icon-action:disabled {
    cursor: default;
    opacity: 0.38;
  }

  .cortex-workspace-pin-menu__actions {
    display: grid;
    gap: 6px;
    margin-top: 12px;
  }

  .cortex-workspace-pin-menu__action {
    display: flex;
    align-items: center;
    gap: 9px;
    width: 100%;
    min-height: 38px;
    border-radius: 10px;
    padding: 0 10px;
    background: transparent;
    font-size: 13px;
    font-weight: 650;
    letter-spacing: 0;
    text-align: left;
  }

  .cortex-workspace-pin-menu__action:hover,
  .cortex-workspace-pin-menu__action:focus-visible {
    background: color-mix(in srgb, var(--constellation-color-spectral-owner) 12%, transparent);
    outline: none;
  }

  .cortex-workspace-pin-menu__action--danger {
    color: var(--workspace-pin-menu-danger-text);
  }

  .cortex-workspace-pin-menu__action--danger:hover,
  .cortex-workspace-pin-menu__action--danger:focus-visible {
    background: var(--workspace-pin-menu-danger-hover-background);
  }
</style>
