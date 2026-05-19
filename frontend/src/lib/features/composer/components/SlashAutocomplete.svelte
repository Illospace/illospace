<script module lang="ts">
  import { slashCommands } from '$lib/features/cortex/api/cortexApi';
  import {
    anchoredShortcutMenuGeometry,
    shortcutMenuCssVariables,
    shortcutMenuPortal,
    type ShortcutMenuGeometry,
  } from '$lib/features/composer/domain/shortcutMenu';

  interface SlashCommand {
    name: string;
    description: string;
    tier?: string;
    model_tier?: string;
  }

  let slashCommandsPromise: Promise<SlashCommand[]> | null = null;
  const MENU_MAX_HEIGHT = 220;
  const MENU_MIN_HEIGHT = 96;
  const MENU_PREFERRED_HEIGHT = 160;
  const MENU_VIEWPORT_GAP = 12;
  const MENU_ANCHOR_GAP = 8;

  function loadSlashCommands() {
    if (!slashCommandsPromise) {
      slashCommandsPromise = slashCommands().catch((error) => {
        slashCommandsPromise = null;
        throw error;
      });
    }
    return slashCommandsPromise;
  }

  function defaultMenuGeometry(): ShortcutMenuGeometry {
    return {
      placement: 'above',
      maxHeight: MENU_MAX_HEIGHT,
      left: 0,
      width: 0,
      top: null,
      bottom: null,
    };
  }

</script>

<script lang="ts">
  let {
    oninput,
    visible = false,
    placement = 'above',
    anchor,
  }: {
    oninput: (cmd: string) => void;
    visible: boolean;
    placement?: 'above' | 'below';
    anchor?: HTMLElement | undefined;
  } = $props();

  let commands = $state<SlashCommand[]>([]);
  let filtered = $state<SlashCommand[]>([]);
  let query = $state('');
  let selectedIndex = $state(0);
  let active = $state(false);
  let loading = $state(false);
  let loaded = $state(false);
  let loadError = $state<string | null>(null);
  let effectivePlacement = $state<'above' | 'below'>('above');
  let menuGeometry = $state<ShortcutMenuGeometry>(defaultMenuGeometry());
  let geometryFrame: number | null = null;

  const shouldShowMenu = $derived(
    visible && active && (loading || Boolean(loadError) || loaded || filtered.length > 0),
  );
  const dropdownStyle = $derived(
    shortcutMenuCssVariables(menuGeometry, 'slash'),
  );

  function updateMenuGeometry() {
    if (typeof window === 'undefined' || !anchor) {
      effectivePlacement = placement;
      menuGeometry = defaultMenuGeometry();
      return;
    }

    const rect = anchor.getBoundingClientRect();
    menuGeometry = anchoredShortcutMenuGeometry(rect, window.innerWidth, window.innerHeight, {
      placement,
      preferredHeight: MENU_PREFERRED_HEIGHT,
      minHeight: MENU_MIN_HEIGHT,
      maxHeight: MENU_MAX_HEIGHT,
      viewportGap: MENU_VIEWPORT_GAP,
      anchorGap: MENU_ANCHOR_GAP,
    });
    effectivePlacement = menuGeometry.placement;
  }

  function queueMenuGeometryUpdate() {
    if (typeof window === 'undefined') return;
    if (geometryFrame !== null) window.cancelAnimationFrame(geometryFrame);
    geometryFrame = window.requestAnimationFrame(() => {
      geometryFrame = null;
      updateMenuGeometry();
    });
  }

  $effect(() => {
    if (!visible || !active) return;
    placement;
    anchor;
    loading;
    loadError;
    filtered.length;
    queueMenuGeometryUpdate();

    if (typeof window === 'undefined') return;
    const handleViewportChange = () => queueMenuGeometryUpdate();
    window.addEventListener('resize', handleViewportChange);
    window.addEventListener('scroll', handleViewportChange, true);

    return () => {
      window.removeEventListener('resize', handleViewportChange);
      window.removeEventListener('scroll', handleViewportChange, true);
      if (geometryFrame !== null) {
        window.cancelAnimationFrame(geometryFrame);
        geometryFrame = null;
      }
    };
  });

  async function ensureCommandsLoaded() {
    if (commands.length > 0 || loading || loaded) return;
    loading = true;
    loadError = null;
    updateMenuGeometry();
    try {
      commands = await loadSlashCommands();
      loaded = true;
    } catch {
      commands = [];
      loaded = false;
      loadError = 'Could not load skills';
    } finally {
      loading = false;
    }

    if (active) {
      applyFilter(query, false);
    }
    updateMenuGeometry();
  }

  function applyFilter(text: string, resetSelection = true) {
    if (/\s/.test(text)) {
      filtered = [];
      selectedIndex = 0;
      updateMenuGeometry();
      return;
    }
    const q = text.toLowerCase();
    filtered = commands.filter(
      (c) =>
        String(c.name ?? '').toLowerCase().includes(q) ||
        String(c.description ?? '').toLowerCase().includes(q),
    );
    selectedIndex = resetSelection ? 0 : Math.min(selectedIndex, Math.max(filtered.length - 1, 0));
    updateMenuGeometry();
  }

  export function filter(text: string) {
    const normalized = text.startsWith('/') ? text.slice(1) : text;
    const resetSelection = normalized !== query;
    query = normalized;
    active = true;
    if (commands.length === 0) {
      filtered = [];
      selectedIndex = 0;
      void ensureCommandsLoaded();
      updateMenuGeometry();
      return;
    }
    applyFilter(normalized, resetSelection);
  }

  export function clear() {
    query = '';
    filtered = [];
    selectedIndex = 0;
    active = false;
    loadError = null;
    if (geometryFrame !== null && typeof window !== 'undefined') {
      window.cancelAnimationFrame(geometryFrame);
      geometryFrame = null;
    }
  }

  export function handleKey(e: KeyboardEvent): boolean {
    if (!visible) return false;
    if (e.key === 'Escape') {
      clear();
      return true;
    }
    if (filtered.length === 0) return false;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      selectedIndex = (selectedIndex + 1) % filtered.length;
      return true;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      selectedIndex = (selectedIndex - 1 + filtered.length) % filtered.length;
      return true;
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && filtered.length > 0)) {
      e.preventDefault();
      select(filtered[selectedIndex]);
      return true;
    }
    return false;
  }

  function select(cmd: SlashCommand) {
    clear();
    oninput('/' + cmd.name + ' ');
  }

  function commandDescription(cmd: SlashCommand) {
    return cmd.description || 'Skill';
  }
</script>

{#if shouldShowMenu}
  <div
    use:shortcutMenuPortal
    class="slash-dropdown"
    class:placement-below={effectivePlacement === 'below'}
    style={dropdownStyle}
    role="listbox"
    aria-label="Skills"
  >
    {#if loading}
      <div class="slash-status">Loading skills...</div>
    {:else if loadError}
      <div class="slash-status slash-status-error">{loadError}</div>
    {:else if filtered.length === 0}
      <div class="slash-status">No matching skills</div>
    {:else}
      {#each filtered as cmd, i (cmd.name)}
        <button
          class="slash-item"
          class:selected={i === selectedIndex}
          role="option"
          aria-selected={i === selectedIndex}
          onclick={() => select(cmd)}
          onmouseenter={() => (selectedIndex = i)}
        >
          <span class="slash-name">/{cmd.name}</span>
          <span class="slash-desc">{commandDescription(cmd)}</span>
        </button>
      {/each}
    {/if}
  </div>
{/if}

<style>
  .slash-dropdown {
    position: fixed;
    top: var(--slash-dropdown-top, auto);
    bottom: var(--slash-dropdown-bottom, auto);
    left: var(--slash-dropdown-left, 0);
    width: var(--slash-dropdown-width, min(480px, calc(100vw - 24px)));
    max-height: var(--slash-dropdown-max-height, 220px);
    overflow-y: auto;
    padding: 5px;
    background: var(--constellation-select-chip-menu-background, var(--constellation-surface-floating-background, var(--bg-2)));
    border: 1px solid var(--constellation-select-chip-menu-border, var(--constellation-surface-floating-border, var(--border-2)));
    border-radius: 8px;
    margin: 0;
    box-shadow: var(--constellation-select-chip-menu-shadow, var(--constellation-surface-floating-shadow, 0 -4px 16px rgba(0, 0, 0, 0.4)));
    z-index: var(--constellation-layer-popover, 1000);
    scrollbar-color: var(--constellation-utility-panel-scrollbar, rgba(255, 255, 255, 0.14)) transparent;
  }

  .slash-dropdown.placement-below {
    box-shadow: var(--constellation-select-chip-menu-shadow, var(--constellation-surface-floating-shadow, 0 14px 34px rgba(0, 0, 0, 0.28)));
  }

  .slash-item {
    display: flex;
    align-items: center;
    gap: 9px;
    width: 100%;
    min-height: 30px;
    padding: 5px 8px;
    background: transparent;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    text-align: left;
    color: var(--constellation-select-chip-option-text, var(--constellation-color-text-secondary, var(--text-2)));
    font-family: var(--constellation-font-sans, var(--font-sans));
    line-height: 1.2;
    transition:
      background-color var(--constellation-motion-hover-duration, 160ms) ease,
      color var(--constellation-motion-hover-duration, 160ms) ease;
  }

  .slash-item:hover,
  .slash-item.selected {
    background: var(--constellation-select-chip-option-active-background, var(--constellation-control-button-quiet-hover-background, var(--bg-3)));
    color: var(--constellation-select-chip-option-active-text, var(--constellation-color-text-primary, var(--text-1)));
  }

  .slash-name {
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 12px;
    font-weight: 600;
    color: var(--constellation-color-text-primary, var(--text-1));
    flex-shrink: 0;
    letter-spacing: 0;
  }

  .slash-desc {
    min-width: 0;
    font-size: 11px;
    line-height: 1.25;
    color: var(--constellation-select-chip-option-description, var(--constellation-color-text-muted, var(--text-3)));
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }

  .slash-status {
    padding: 9px 10px;
    color: var(--constellation-select-chip-option-description, var(--constellation-color-text-muted, var(--text-3)));
    font-family: var(--constellation-font-sans, var(--font-sans));
    font-size: 11px;
    line-height: 1.25;
  }

  .slash-status-error {
    color: var(--constellation-control-pill-danger-text, var(--negative, #b24a61));
  }
</style>
