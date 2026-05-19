<script lang="ts">
  /**
   * MentionAutocomplete — reusable @mention dropdown.
   * Attach to any textarea. Monitors input for @query and shows matching members.
   */
  import { cortex } from '$lib/stores/cortex.svelte';
  import {
    defaultIlloMentionOption,
    mentionDropdownGeometry,
    mentionHandleForPerson,
    normalizeMentionHandle,
    type MentionAutocompleteOption,
    type MentionDropdownPlacement,
  } from '$lib/features/composer/domain/mentionAutocomplete';

  type NormalizedMentionOption = Required<Pick<MentionAutocompleteOption, 'name' | 'hint'>> &
    MentionAutocompleteOption & {
      insertText: string;
      color: string;
      searchText: string;
    };

  let {
    textarea = $bindable(),
    options = [],
    includeIllo = true,
    onselect,
  }: {
    textarea: HTMLTextAreaElement | undefined;
    options?: MentionAutocompleteOption[];
    includeIllo?: boolean;
    onselect: (name: string) => void;
  } = $props();

  let loadedMembers = $state<MentionAutocompleteOption[]>([]);
  let matches = $state<NormalizedMentionOption[]>([]);
  let selectedIdx = $state(0);
  let visible = $state(false);
  let membersLoading = $state(false);
  let activeQuery = $state<string | null>(null);
  let effectivePlacement = $state<MentionDropdownPlacement>('above');
  let dropdownStyle = $state('');
  let geometryFrame: number | null = null;

  const normalizedOptions = $derived.by(() => options.map(normalizeOption).filter(isMentionOption));
  const normalizedLoadedMembers = $derived.by(() => loadedMembers.map(normalizeOption).filter(isMentionOption));
  const members = $derived.by(() => {
    const rows = new Map<string, NormalizedMentionOption>();
    if (includeIllo) {
      const illo = normalizeOption(defaultIlloMentionOption());
      if (illo) rows.set(illo.insertText.toLowerCase(), illo);
    }

    const source = normalizedOptions.length > 0 ? normalizedOptions : normalizedLoadedMembers;
    for (const member of source) {
      rows.set(member.insertText.toLowerCase(), member);
    }
    return Array.from(rows.values());
  });

  function portalToBody(node: HTMLElement) {
    if (typeof document === 'undefined') return {};
    document.body.appendChild(node);
    return {
      destroy() {
        node.remove();
      },
    };
  }

  function updateMenuGeometry() {
    if (typeof window === 'undefined' || !textarea) {
      effectivePlacement = 'above';
      dropdownStyle = '';
      return;
    }

    const rect = textarea.getBoundingClientRect();
    const geometry = mentionDropdownGeometry(rect, window.innerWidth, window.innerHeight);
    effectivePlacement = geometry.placement;
    dropdownStyle = geometry.style;
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
    if (!visible) return;
    textarea;
    matches.length;
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

  function normalizeOption(option: MentionAutocompleteOption): NormalizedMentionOption | null {
    const name = option.name?.trim();
    if (!name) return null;
    const insertText = normalizeMentionHandle(option.insertText || mentionHandleForPerson(option) || name);
    if (!insertText) return null;
    const keywords = (option.keywords ?? []).join(' ');
    return {
      ...option,
      name,
      insertText,
      color: option.color || '#6366f1',
      hint: option.hint || (option.isIllo ? 'Mention Illo' : 'Mention teammate'),
      searchText: `${name} ${insertText} ${keywords}`.toLowerCase(),
    };
  }

  function isMentionOption(option: NormalizedMentionOption | null): option is NormalizedMentionOption {
    return option !== null;
  }

  function closeMenu() {
    visible = false;
    activeQuery = null;
  }

  function ensureMembersLoaded() {
    if (options.length > 0 || loadedMembers.length > 0 || membersLoading) return;
    membersLoading = true;
    cortex.loadTeamMembers()
      .then((m: any[]) => {
        loadedMembers = m.map((u: any) => ({
          id: String(u.id),
          name: u.name,
          insertText: mentionHandleForPerson(u),
          color: u.color || u.cortex_color || '#6366f1',
          hint: u.email || 'Mention teammate',
          keywords: [u.email, u.name].filter(Boolean),
        }));
        check(textarea?.value ?? '');
      })
      .catch(() => {
        loadedMembers = [];
      })
      .finally(() => {
        membersLoading = false;
      });
  }

  /** Call this from the textarea's oninput handler. */
  export function check(value: string, cursor = textarea?.selectionStart ?? value.length) {
    const atMatch = value.slice(0, cursor).match(/(^|[\s([{])@([A-Za-z0-9._-]*)$/);
    if (atMatch) {
      ensureMembersLoaded();
      const query = atMatch[2].toLowerCase();
      const isSameQuery = activeQuery === query;
      matches = query ? members.filter((m) => m.searchText.includes(query)) : members;
      selectedIdx = isSameQuery ? Math.min(selectedIdx, Math.max(matches.length - 1, 0)) : 0;
      activeQuery = query;
      visible = matches.length > 0;
      if (visible) queueMenuGeometryUpdate();
    } else {
      closeMenu();
    }
  }

  /** Call this from the textarea's onkeydown handler. Returns true if handled. */
  export function handleKey(e: KeyboardEvent): boolean {
    if (!visible) return false;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      e.stopPropagation();
      selectedIdx = matches.length > 0 ? (selectedIdx + 1) % matches.length : 0;
      return true;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      e.stopPropagation();
      selectedIdx = matches.length > 0 ? (selectedIdx - 1 + matches.length) % matches.length : 0;
      return true;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      e.stopPropagation();
      const match = matches[selectedIdx];
      if (match) select(match);
      return true;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeMenu();
      return true;
    }
    return false;
  }

  function select(member: NormalizedMentionOption) {
    closeMenu();
    onselect(member.insertText);
  }
</script>

{#if visible}
  <div
    class="mention-dropdown"
    class:placement-below={effectivePlacement === 'below'}
    style={dropdownStyle}
    use:portalToBody
  >
    <div class="mention-dropdown-label">Mention someone</div>
    {#each matches as m, i (m.name)}
      <button
        type="button"
        class="mention-option"
        class:selected={i === selectedIdx}
        onclick={() => select(m)}
        onpointerenter={() => (selectedIdx = i)}
      >
        <div class="mention-avatar" style="background: {m.isIllo ? 'linear-gradient(135deg,#5ea898,#408880)' : m.color}">
          {m.isIllo ? 'i' : m.name.slice(0, 2).toUpperCase()}
        </div>
        <div>
          <div class="mention-name" style="color: {m.isIllo ? '#5ea898' : 'var(--text-1)'}">{m.name}</div>
          <div class="mention-hint">{m.hint}</div>
        </div>
      </button>
    {/each}
  </div>
{/if}

<style>
  .mention-dropdown {
    position: fixed;
    top: var(--mention-dropdown-top, 0);
    right: auto;
    bottom: auto;
    left: var(--mention-dropdown-left, 12px);
    width: min(var(--mention-dropdown-width, 260px), calc(100vw - 24px));
    max-height: var(--mention-dropdown-max-height, 260px);
    box-sizing: border-box;
    overflow-y: auto;
    transform: translateY(-100%);
    background: var(--constellation-select-chip-menu-background, rgba(12, 15, 22, 0.96));
    backdrop-filter: blur(20px);
    border: 1px solid var(--constellation-select-chip-menu-border, rgba(255, 255, 255, 0.08));
    border-radius: 12px;
    padding: 6px;
    min-width: 220px;
    box-shadow: var(--constellation-select-chip-menu-shadow, 0 12px 40px rgba(0, 0, 0, 0.5));
    z-index: var(--constellation-layer-popover, 1000);
    opacity: 1;
    animation: none;
    scrollbar-color: var(--constellation-utility-panel-scrollbar, rgba(255, 255, 255, 0.14)) transparent;
  }

  .mention-dropdown.placement-below {
    transform: none;
  }

  .mention-dropdown-label {
    font-size: 9px;
    color: var(--text-3);
    padding: 4px 12px 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .mention-option {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 6px 10px;
    border: 0;
    border-radius: 8px;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
    transition: background 0.1s;
  }
  .mention-option:hover, .mention-option.selected {
    background: rgba(255, 255, 255, 0.06);
  }

  .mention-avatar {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-weight: 600;
    color: #fff;
    flex-shrink: 0;
  }

  .mention-name {
    font-size: 12px;
    font-weight: 500;
  }

  .mention-hint {
    font-size: 10px;
    color: var(--text-3);
  }
</style>
