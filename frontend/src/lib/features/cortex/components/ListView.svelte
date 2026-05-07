<script lang="ts">
  import { ConstellationPresenceSeed } from '$lib/components/constellation';
  import { auth } from '$lib/stores/auth.svelte';
  import { cortex } from '$lib/stores/cortex.svelte';
  import {
    buildPresenceSeedStyle,
    normalizeHexColor,
    presenceToneForColor,
  } from '$lib/utils/constellationPresence';

  const STATUS_COLORS: Record<string, string> = {
    idle: '#57CFA0',
    working: '#E3AA54',
    done: '#57CFA0',
  };

  const STATUS_LABELS: Record<string, string> = {
    idle: 'idle',
    working: 'working',
    done: 'unread',
  };

  function timeAgo(ts: string) {
    const diff = Date.now() - new Date(ts).getTime();
    if (diff < 60_000) return 'just now';
    if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
    if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
    return new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function teamMemberFor(userId: string | null | undefined) {
    if (!userId) return null;
    return cortex.teamMembers.find((member) => String(member.id) === String(userId)) ?? null;
  }

  function ideaOwnerName(idea: typeof cortex.filteredIdeas[number]) {
    if (String(idea.user_id) === String(auth.user?.id ?? '')) {
      return auth.user?.name || auth.user?.email || idea.author_name || 'You';
    }

    const member = teamMemberFor(idea.user_id);
    return member?.name || member?.email || idea.author_name || 'Team member';
  }

  function ideaOwnerColor(idea: typeof cortex.filteredIdeas[number]) {
    if (String(idea.user_id) === String(auth.user?.id ?? '')) {
      return normalizeHexColor(auth.user?.color) ?? normalizeHexColor(idea.author_color) ?? '#57CFA0';
    }

    const member = teamMemberFor(idea.user_id) as ({ color?: string | null; cortex_color?: string | null } | null);
    return (
      normalizeHexColor(member?.color ?? member?.cortex_color) ??
      normalizeHexColor(idea.author_color) ??
      '#57CFA0'
    );
  }

  let sorted = $derived(
    [...cortex.filteredIdeas].sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    ),
  );
</script>

<div class="list-view">
  {#if sorted.length === 0}
    <div class="empty">No ideas match your filters.</div>
  {:else}
    {#each sorted as idea, idx (idea.id)}
      {@const ownerColor = ideaOwnerColor(idea)}
      {@const ownerName = ideaOwnerName(idea)}
      {@const statusColor = STATUS_COLORS[idea.status] ?? '#8090A8'}
      <button
        class="list-item"
        class:selected={cortex.selectedIdeaId === idea.id}
        style="--row-color: {ownerColor}; --status-color: {statusColor}; animation-delay: {idx * 30}ms"
        onclick={() => cortex.selectIdea(idea.id)}
      >
        <ConstellationPresenceSeed
          label={ownerName}
          size="sm"
          role="user"
          tone={presenceToneForColor(ownerColor)}
          style={buildPresenceSeedStyle(ownerColor) || undefined}
          title={ownerName}
        />
        <span class="item-title">{idea.title}</span>
        <span class="item-status">
          {STATUS_LABELS[idea.status] ?? idea.status.replace('_', ' ')}
        </span>
        <span class="item-time">{timeAgo(idea.updated_at)}</span>
      </button>
    {/each}
  {/if}
</div>

<style>
  @keyframes stream-in {
    from {
      opacity: 0;
      transform: translateY(6px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }

  .list-view {
    --list-view-background: rgba(6, 8, 14, 0.4);
    --list-empty-text: rgba(255, 255, 255, 0.3);
    --list-item-border: rgba(255, 255, 255, 0.04);
    --list-item-background: rgba(8, 11, 18, 0.5);
    --list-item-text: rgba(255, 255, 255, 0.7);
    --list-item-hover-border: rgba(255, 255, 255, 0.08);
    --list-item-hover-background: rgba(255, 255, 255, 0.04);
    --list-item-hover-shadow: 0 0 20px rgba(255, 255, 255, 0.02);
    --list-item-selected-border-strength: 32%;
    --list-item-selected-border-base: rgba(245, 214, 138, 0.18);
    --list-item-selected-background-base: rgba(245, 214, 138, 0.03);
    --list-item-time-text: rgba(255, 255, 255, 0.3);
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px;
    overflow-y: auto;
    height: 100%;
    background: var(--list-view-background);
  }

  :global(:root[data-color-scheme='light']) .list-view {
    --list-view-background: color-mix(in srgb, var(--constellation-surface-panel-background) 52%, transparent);
    --list-empty-text: var(--constellation-color-text-muted);
    --list-item-border: rgba(24, 35, 49, 0.08);
    --list-item-background: rgba(255, 255, 255, 0.42);
    --list-item-text: var(--constellation-color-text-secondary);
    --list-item-hover-border: color-mix(in srgb, var(--row-color) 20%, rgba(24, 35, 49, 0.08));
    --list-item-hover-background: color-mix(in srgb, var(--row-color) 5%, rgba(255, 255, 255, 0.62));
    --list-item-hover-shadow: none;
    --list-item-selected-border-strength: 30%;
    --list-item-selected-border-base: rgba(24, 35, 49, 0.1);
    --list-item-selected-background-base: rgba(255, 255, 255, 0.68);
    --list-item-time-text: var(--constellation-color-text-muted);
  }

  .empty {
    color: var(--list-empty-text);
    font-size: 13px;
    letter-spacing: 0.3px;
    text-align: center;
    padding: 40px 0;
    font-weight: 300;
  }

  .list-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    background: var(--list-item-background);
    border: 1px solid var(--list-item-border);
    border-radius: 8px;
    cursor: pointer;
    text-align: left;
    color: var(--list-item-text);
    font-family: var(--font-sans);
    font-weight: 300;
    letter-spacing: 0.2px;
    transition: background 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
    animation: stream-in 0.3s ease both;
  }

  .list-item:hover {
    background: var(--list-item-hover-background);
    border-color: var(--list-item-hover-border);
    box-shadow: var(--list-item-hover-shadow);
  }

  .list-item.selected {
    border-color: color-mix(in srgb, var(--row-color) var(--list-item-selected-border-strength), var(--list-item-selected-border-base));
    background: color-mix(in srgb, var(--row-color) 8%, var(--list-item-selected-background-base));
    box-shadow:
      0 0 24px color-mix(in srgb, var(--row-color) 10%, transparent),
      inset 0 1px 0 color-mix(in srgb, var(--row-color) 8%, transparent);
  }

  .item-title {
    flex: 1;
    font-size: 13px;
    font-weight: 400;
    letter-spacing: 0.2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .item-status {
    font-size: 11px;
    letter-spacing: 0.3px;
    text-transform: capitalize;
    flex-shrink: 0;
    opacity: 0.8;
    color: var(--status-color);
  }

  .item-time {
    font-size: 11px;
    color: var(--list-item-time-text);
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.3px;
  }

</style>
