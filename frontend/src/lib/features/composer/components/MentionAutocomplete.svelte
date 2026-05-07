<script lang="ts">
  /**
   * MentionAutocomplete — reusable @mention dropdown.
   * Attach to any textarea. Monitors input for @query and shows matching members.
   */
  import { cortex } from '$lib/stores/cortex.svelte';

  let { textarea = $bindable(), onselect }: {
    textarea: HTMLTextAreaElement | undefined;
    onselect: (name: string) => void;
  } = $props();

  let members = $state<{ name: string; color: string; isIllo?: boolean; hint: string }[]>([]);
  let matches = $state<typeof members>([]);
  let selectedIdx = $state(0);
  let visible = $state(false);
  let membersLoading = false;

  function ensureMembersLoaded() {
    if (members.length > 0 || membersLoading) return;
    membersLoading = true;
    cortex.loadTeamMembers()
      .then((m: any[]) => {
        members = m.map((u: any) => ({ name: u.name, color: u.color || '#6366f1', hint: 'Mention teammate' }));
        check(textarea?.value ?? '');
      })
      .catch(() => {
        members = [];
      })
      .finally(() => {
        membersLoading = false;
      });
  }

  /** Call this from the textarea's oninput handler. */
  export function check(value: string) {
    const atMatch = value.match(/(^|\s)@(\w*)$/);
    if (atMatch) {
      if (members.length === 0) {
        ensureMembersLoaded();
        matches = [];
        visible = false;
        return;
      }
      const query = atMatch[2].toLowerCase();
      matches = query ? members.filter(m => m.name.toLowerCase().includes(query)) : members;
      selectedIdx = 0;
      visible = matches.length > 0;
    } else {
      visible = false;
    }
  }

  /** Call this from the textarea's onkeydown handler. Returns true if handled. */
  export function handleKey(e: KeyboardEvent): boolean {
    if (!visible) return false;
    if (e.key === 'ArrowDown') { e.preventDefault(); selectedIdx = Math.min(selectedIdx + 1, matches.length - 1); return true; }
    if (e.key === 'ArrowUp') { e.preventDefault(); selectedIdx = Math.max(selectedIdx - 1, 0); return true; }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      select(matches[selectedIdx].name);
      return true;
    }
    if (e.key === 'Escape') { visible = false; return true; }
    return false;
  }

  function select(name: string) {
    visible = false;
    onselect(name);
  }
</script>

{#if visible}
  <div class="mention-dropdown">
    <div class="mention-dropdown-label">Mention someone</div>
    {#each matches as m, i (m.name)}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_static_element_interactions -->
      <div
        class="mention-option"
        class:selected={i === selectedIdx}
        onclick={() => select(m.name)}
        onpointerenter={() => (selectedIdx = i)}
      >
        <div class="mention-avatar" style="background: {m.isIllo ? 'linear-gradient(135deg,#5ea898,#408880)' : m.color}">
          {m.isIllo ? '✦' : m.name.slice(0, 2).toUpperCase()}
        </div>
        <div>
          <div class="mention-name" style="color: {m.isIllo ? '#5ea898' : 'var(--text-1)'}">{m.name}</div>
          <div class="mention-hint">{m.hint}</div>
        </div>
      </div>
    {/each}
  </div>
{/if}

<style>
  .mention-dropdown {
    position: absolute;
    bottom: 100%;
    left: 50%;
    transform: translateX(-50%);
    margin-bottom: 8px;
    background: rgba(12, 15, 22, 0.95);
    backdrop-filter: blur(20px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 12px;
    padding: 6px;
    min-width: 220px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
    z-index: 100;
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
    padding: 6px 10px;
    border-radius: 8px;
    cursor: pointer;
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
