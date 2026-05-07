<script module lang="ts">
  import { slashCommands } from '$lib/features/cortex/api/cortexApi';

  interface SlashCommand {
    name: string;
    description: string;
    tier?: string;
    model_tier?: string;
  }

  let slashCommandsPromise: Promise<SlashCommand[]> | null = null;

  function loadSlashCommands() {
    if (!slashCommandsPromise) {
      slashCommandsPromise = slashCommands().catch((error) => {
        slashCommandsPromise = null;
        throw error;
      });
    }
    return slashCommandsPromise;
  }
</script>

<script lang="ts">
  let {
    oninput,
    visible = false,
    placement = 'above',
  }: {
    oninput: (cmd: string) => void;
    visible: boolean;
    placement?: 'above' | 'below';
  } = $props();

  let commands = $state<SlashCommand[]>([]);
  let filtered = $state<SlashCommand[]>([]);
  let query = $state('');
  let selectedIndex = $state(0);
  let active = $state(false);
  let loading = $state(false);
  let loaded = $state(false);
  let loadError = $state<string | null>(null);

  const TIER_COLORS: Record<string, string> = {
    high: 'var(--constellation-control-pill-thinking-text)',
    medium: 'var(--constellation-color-text-muted)',
    low: 'var(--constellation-control-pill-success-text)',
    local: 'var(--constellation-control-pill-warning-text)',
  };

  const shouldShowMenu = $derived(
    visible && active && (loading || Boolean(loadError) || loaded || filtered.length > 0),
  );

  function normalizeTier(tier: string | null | undefined): string {
    const normalized = tier?.trim().toLowerCase() ?? '';
    return TIER_COLORS[normalized] ? normalized : 'medium';
  }

  async function ensureCommandsLoaded() {
    if (commands.length > 0 || loading) return;
    loading = true;
    loadError = null;
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
  }

  function applyFilter(text: string, resetSelection = true) {
    if (/\s/.test(text)) {
      filtered = [];
      selectedIndex = 0;
      return;
    }
    const q = text.toLowerCase();
    filtered = commands.filter(
      (c) =>
        String(c.name ?? '').toLowerCase().includes(q) ||
        String(c.description ?? '').toLowerCase().includes(q),
    );
    selectedIndex = resetSelection ? 0 : Math.min(selectedIndex, Math.max(filtered.length - 1, 0));
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
    oninput('/' + cmd.name + ' ');
    filtered = [];
  }

  function tierLabel(cmd: SlashCommand) {
    return cmd.tier ?? cmd.model_tier ?? '';
  }

  function commandDescription(cmd: SlashCommand) {
    return cmd.description || 'Skill';
  }
</script>

{#if shouldShowMenu}
  <div class="slash-dropdown" class:placement-below={placement === 'below'} role="listbox" aria-label="Skills">
    {#if loading}
      <div class="slash-status">Loading skills...</div>
    {:else if loadError}
      <div class="slash-status slash-status-error">{loadError}</div>
    {:else if filtered.length === 0}
      <div class="slash-status">No matching skills</div>
    {:else}
      {#each filtered as cmd, i (cmd.name)}
        {@const tier = tierLabel(cmd)}
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
          {#if tier}
            <span class="slash-tier" style="color: {TIER_COLORS[normalizeTier(tier)] ?? '#888'}">
              {normalizeTier(tier)}
            </span>
          {/if}
        </button>
      {/each}
    {/if}
  </div>
{/if}

<style>
  .slash-dropdown {
    position: absolute;
    bottom: 100%;
    left: 0;
    right: 0;
    max-height: min(220px, 46vh);
    overflow-y: auto;
    padding: 5px;
    background: var(--constellation-select-chip-menu-background, var(--constellation-surface-floating-background, var(--bg-2)));
    border: 1px solid var(--constellation-select-chip-menu-border, var(--constellation-surface-floating-border, var(--border-2)));
    border-radius: 8px;
    margin-bottom: 4px;
    box-shadow: var(--constellation-select-chip-menu-shadow, var(--constellation-surface-floating-shadow, 0 -4px 16px rgba(0, 0, 0, 0.4)));
    z-index: 100;
    scrollbar-color: var(--constellation-utility-panel-scrollbar, rgba(255, 255, 255, 0.14)) transparent;
  }

  .slash-dropdown.placement-below {
    top: 100%;
    bottom: auto;
    margin-top: 4px;
    margin-bottom: 0;
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

  .slash-tier {
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    flex-shrink: 0;
    letter-spacing: 0;
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
