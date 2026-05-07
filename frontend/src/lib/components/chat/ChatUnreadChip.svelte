<script lang="ts">
  let {
    count = 0,
    label = '',
    compact = false,
    muted = false,
    className = '',
  }: {
    count?: number;
    label?: string;
    compact?: boolean;
    muted?: boolean;
    className?: string;
  } = $props();

  const resolvedCount = $derived(Math.max(0, count));
  const rootClass = $derived(
    ['chat-unread-chip', compact ? 'is-compact' : '', muted ? 'is-muted' : '', className]
      .filter(Boolean)
      .join(' '),
  );
  const text = $derived(label || (resolvedCount > 99 ? '99+' : `${resolvedCount}`));
</script>

{#if resolvedCount > 0 || label}
  <span class={rootClass} aria-label={`${resolvedCount} unread`}>
    {text}
  </span>
{/if}

<style>
  .chat-unread-chip {
    display: inline-flex;
    min-width: 20px;
    align-items: center;
    justify-content: center;
    padding: 4px 8px;
    border-radius: 999px;
    background: linear-gradient(180deg, rgba(141, 183, 255, 0.2), rgba(141, 183, 255, 0.12));
    border: 1px solid rgba(141, 183, 255, 0.26);
    color: rgba(230, 238, 255, 0.96);
    font-family: var(--constellation-font-mono);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.14em;
    line-height: 1;
    text-transform: uppercase;
    box-shadow: 0 0 14px rgba(141, 183, 255, 0.14);
  }

  .chat-unread-chip.is-compact {
    min-width: 18px;
    padding: 3px 6px;
    font-size: 8px;
  }

  .chat-unread-chip.is-muted {
    background: var(--constellation-chat-unread-muted-background);
    border-color: var(--constellation-chat-unread-muted-border);
    color: var(--constellation-chat-unread-muted-text);
    box-shadow: none;
  }
</style>
