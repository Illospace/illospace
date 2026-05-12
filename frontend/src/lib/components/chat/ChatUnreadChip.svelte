<script lang="ts">
  let {
    count = 0,
    label = '',
    compact = false,
    muted = false,
    className = '',
    style = '',
  }: {
    count?: number;
    label?: string;
    compact?: boolean;
    muted?: boolean;
    className?: string;
    style?: string;
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
  <span class={rootClass} {style} aria-label={`${resolvedCount} unread`}>
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
    --chat-unread-chip-accent: var(--thread-accent, var(--constellation-color-user-accent, #57CFA0));
    background:
      linear-gradient(
        180deg,
        color-mix(in srgb, var(--chat-unread-chip-accent) 24%, transparent),
        color-mix(in srgb, var(--chat-unread-chip-accent) 14%, transparent)
      );
    border: 1px solid color-mix(in srgb, var(--chat-unread-chip-accent) 30%, transparent);
    color: rgba(230, 238, 255, 0.96);
    font-family: var(--constellation-font-mono);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.14em;
    line-height: 1;
    text-transform: uppercase;
    box-shadow: 0 0 14px color-mix(in srgb, var(--chat-unread-chip-accent) 18%, transparent);
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
