<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';

  let {
    visible = false,
    label = 'Jump to latest',
    className = '',
    onclick,
  }: {
    visible?: boolean;
    label?: string;
    className?: string;
    onclick?: (event: MouseEvent) => void;
  } = $props();

  const rootClass = $derived(['conversation-scroll-cue', className].filter(Boolean).join(' '));
</script>

{#if visible}
  <button type="button" class={rootClass} aria-label={label} title={label} {onclick}>
    <ConstellationIcon name="chevron-down" size={14} stroke={1.8} />
  </button>
{/if}

<style>
  .conversation-scroll-cue {
    position: sticky;
    left: 50%;
    bottom: 16px;
    z-index: 8;
    width: 28px;
    height: 28px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    appearance: none;
    border-radius: 999px;
    border: 1px solid var(--constellation-thread-scroll-cue-border, rgba(240, 240, 250, 0.08));
    background: var(--constellation-thread-scroll-cue-background, rgba(10, 14, 22, 0.72));
    color: var(--constellation-thread-scroll-cue-text, rgba(240, 240, 250, 0.72));
    box-shadow: var(--constellation-thread-scroll-cue-shadow, 0 12px 26px rgba(0, 0, 0, 0.2));
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    cursor: pointer;
    transition:
      transform 160ms ease,
      color 160ms ease,
      border-color 160ms ease,
      background-color 160ms ease,
      box-shadow 160ms ease,
      opacity 160ms ease;
    animation: conversation-scroll-cue-breathe 1.8s ease-in-out infinite;
  }

  .conversation-scroll-cue:hover,
  .conversation-scroll-cue:focus-visible {
    color: var(--constellation-color-text-primary);
    transform: translateY(-1px);
  }

  .conversation-scroll-cue:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--constellation-color-accent, #5ecfa0) 52%, transparent);
    outline-offset: 3px;
  }

  .conversation-scroll-cue:active {
    transform: translateY(1px) scale(0.985);
  }

  .conversation-scroll-cue :global(svg) {
    width: 14px;
    height: 14px;
  }

  @keyframes conversation-scroll-cue-breathe {
    0%,
    100% {
      opacity: 0.74;
    }

    50% {
      opacity: 1;
    }
  }
</style>
