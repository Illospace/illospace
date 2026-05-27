<script lang="ts">
  import type { Snippet } from 'svelte';

  let {
    entering = false,
    ready = true,
    zIndex = 25,
    dismissLabel,
    dismissCursor = 'default',
    className = '',
    frameClassName = '',
    style = '',
    ondismiss,
    periphery,
    children,
  }: {
    entering?: boolean;
    ready?: boolean;
    zIndex?: number;
    dismissLabel?: string;
    dismissCursor?: string;
    className?: string;
    frameClassName?: string;
    style?: string;
    ondismiss?: () => void;
    periphery?: Snippet;
    children?: Snippet;
  } = $props();

  const shellStyle = $derived(
    [
      `--workspace-stage-z-index:${zIndex}`,
      `--workspace-stage-dismiss-cursor:${dismissCursor}`,
      style,
    ].filter(Boolean).join('; '),
  );
</script>

<div class={`workspace-stage-shell ${className}`} class:entering={entering} class:ready={ready} style={shellStyle}>
  {@render periphery?.()}

  {#if ondismiss}
    <button
      type="button"
      class="workspace-stage-edge-dismiss"
      tabindex="-1"
      aria-label={dismissLabel}
      onclick={ondismiss}
    ></button>
  {/if}

  <div class={`workspace-stage-frame ${frameClassName}`}>
    {@render children?.()}
  </div>
</div>

<style>
  .workspace-stage-shell {
    --workspace-stage-frame-width: clamp(1040px, 78vw, 1760px);
    position: absolute;
    inset: 0;
    z-index: var(--workspace-stage-z-index);
    display: flex;
    align-items: flex-start;
    justify-content: center;
    box-sizing: border-box;
    padding: clamp(18px, 2.4vw, 30px);
    pointer-events: auto;
    opacity: 1;
  }

  .workspace-stage-frame {
    position: relative;
    z-index: 3;
    width: min(100%, var(--workspace-stage-frame-width));
    height: calc(100% - 6px);
    max-height: 100%;
    overflow: visible;
    isolation: isolate;
    pointer-events: none;
    transform-origin: 50% 54%;
    opacity: 0;
    transform: translate3d(0, 10px, 0) scale(0.985);
    will-change: opacity, transform;
    transition:
      opacity 180ms cubic-bezier(0.22, 1, 0.36, 1),
      transform 340ms cubic-bezier(0.18, 0.95, 0.32, 1);
  }

  .workspace-stage-frame > :global(*) {
    pointer-events: auto;
  }

  .workspace-stage-shell.ready .workspace-stage-frame {
    opacity: 1;
    transform: translate3d(0, 0, 0) scale(1);
  }

  .workspace-stage-edge-dismiss {
    position: absolute;
    inset: 0;
    z-index: 1;
    border: 0;
    padding: 0;
    margin: 0;
    background: transparent;
    cursor: var(--workspace-stage-dismiss-cursor);
  }

  .workspace-stage-shell.entering {
    animation: workspace-stage-presence 260ms cubic-bezier(0.22, 1, 0.36, 1) both;
  }

  @keyframes workspace-stage-presence {
    0% {
      opacity: 0;
    }
    100% {
      opacity: 1;
    }
  }

  @media (max-width: 900px) {
    .workspace-stage-shell {
      padding: 6px 10px 4px;
    }

    .workspace-stage-frame {
      width: calc(100% - 18px);
      height: calc(100% - 6px);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .workspace-stage-shell {
      animation: none !important;
    }

    .workspace-stage-frame {
      transition: none;
      opacity: 1;
      transform: none;
      filter: none;
    }
  }
</style>
