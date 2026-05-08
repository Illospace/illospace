<script lang="ts">
  import type { ConstellationSignalState } from './constellationTypes';

  export type ConstellationSignalStatusIndicatorState = ConstellationSignalState | 'unread';
  export type ConstellationSignalStatusIndicatorPlacement = 'inline' | 'anchor';

  let {
    state = 'idle',
    placement = 'inline',
    animated = true,
    label,
    className = '',
    style = '',
  }: {
    state?: ConstellationSignalStatusIndicatorState;
    placement?: ConstellationSignalStatusIndicatorPlacement;
    animated?: boolean;
    label?: string;
    className?: string;
    style?: string;
  } = $props();

  const resolvedState = $derived(state === 'done' ? 'unread' : state);
  const resolvedLabel = $derived(
    label ??
      (resolvedState === 'working'
        ? 'Illo is working'
        : resolvedState === 'unread'
          ? 'Unread thread'
          : 'Idle thread'),
  );
  const rootClass = $derived(
    [
      'constellation-signal-status-indicator',
      `constellation-signal-status-indicator-${resolvedState}`,
      `constellation-signal-status-indicator-placement-${placement}`,
      animated ? 'is-animated' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<span class={rootClass} {style} aria-label={resolvedLabel} title={resolvedLabel} role="img">
  {#if resolvedState === 'working'}
    <span class="constellation-signal-status-indicator-spinner" aria-hidden="true"></span>
  {:else if resolvedState === 'unread'}
    <span class="constellation-signal-status-indicator-unread-beacon" aria-hidden="true"></span>
  {:else}
    <span class="constellation-signal-status-indicator-idle-dot" aria-hidden="true"></span>
  {/if}
</span>

<style>
  .constellation-signal-status-indicator {
    --constellation-signal-status-size: 16px;
    --constellation-signal-status-idle-size: 7px;
    --constellation-signal-status-color: var(
      --blob-unread-color,
      var(--thread-accent, var(--constellation-color-spectral))
    );
    --constellation-signal-status-idle-color: var(
      --constellation-thread-header-status-idle-dot,
      var(--constellation-thread-header-activity-dot, var(--constellation-color-text-muted))
    );
    --constellation-signal-status-idle-ring: var(
      --constellation-thread-header-status-idle-ring,
      var(--constellation-thread-header-activity-ring, rgba(240, 240, 250, 0.12))
    );

    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: var(--constellation-signal-status-size);
    height: var(--constellation-signal-status-size);
    flex: 0 0 var(--constellation-signal-status-size);
    color: var(--constellation-signal-status-color);
    pointer-events: none;
  }

  .constellation-signal-status-indicator-placement-anchor {
    position: absolute;
    top: 7px;
    right: 7px;
    z-index: 2;
    transform: translate(38%, -34%);
  }

  .constellation-signal-status-indicator-idle-dot {
    width: var(--constellation-signal-status-idle-size);
    height: var(--constellation-signal-status-idle-size);
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-signal-status-idle-color);
    box-shadow: 0 0 0 1px var(--constellation-signal-status-idle-ring);
  }

  .constellation-signal-status-indicator-unread-beacon {
    width: var(--constellation-signal-status-size);
    height: var(--constellation-signal-status-size);
    border-radius: var(--constellation-radius-pill);
    background:
      radial-gradient(circle at 42% 35%, rgba(255, 255, 255, 0.36), transparent 34%),
      color-mix(in srgb, var(--constellation-signal-status-color) 74%, rgba(240, 240, 250, 0.22));
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--constellation-signal-status-color) 34%, rgba(240, 240, 250, 0.18)),
      0 0 14px color-mix(in srgb, var(--constellation-signal-status-color) 48%, transparent);
    opacity: 0.92;
  }

  .constellation-signal-status-indicator-spinner {
    width: var(--constellation-signal-status-size);
    height: var(--constellation-signal-status-size);
    box-sizing: border-box;
    border-radius: var(--constellation-radius-pill);
    border: 4px solid transparent;
    border-top-color: color-mix(in srgb, var(--constellation-signal-status-color) 74%, rgba(240, 240, 250, 0.22));
    border-right-color: color-mix(in srgb, var(--constellation-signal-status-color) 74%, rgba(240, 240, 250, 0.22));
    background: transparent;
    filter: drop-shadow(0 0 14px color-mix(in srgb, var(--constellation-signal-status-color) 48%, transparent));
    opacity: 1;
    transform: rotate(0deg);
  }

  .is-animated .constellation-signal-status-indicator-unread-beacon {
    animation: constellation-signal-status-indicator-unread-beacon 3.6s ease-in-out infinite;
  }

  .is-animated .constellation-signal-status-indicator-spinner {
    animation: constellation-signal-status-indicator-spinner-sync-spin 1.25s ease-in-out infinite;
  }

  @keyframes constellation-signal-status-indicator-unread-beacon {
    0%,
    100% {
      opacity: 0.68;
      box-shadow:
        inset 0 0 0 1px color-mix(in srgb, var(--constellation-signal-status-color) 28%, rgba(240, 240, 250, 0.14)),
        0 0 10px color-mix(in srgb, var(--constellation-signal-status-color) 36%, transparent);
    }

    50% {
      opacity: 1;
      box-shadow:
        inset 0 0 0 1px color-mix(in srgb, var(--constellation-signal-status-color) 38%, rgba(240, 240, 250, 0.18)),
        0 0 20px color-mix(in srgb, var(--constellation-signal-status-color) 62%, transparent);
    }
  }

  @keyframes constellation-signal-status-indicator-spinner-sync-spin {
    0% {
      filter: drop-shadow(0 0 12px color-mix(in srgb, var(--constellation-signal-status-color) 42%, transparent));
      transform: rotate(0deg) scale(1);
    }

    16% {
      filter: drop-shadow(0 0 18px color-mix(in srgb, var(--constellation-signal-status-color) 66%, transparent));
      transform: rotate(118deg) scale(1.1);
    }

    30% {
      filter: drop-shadow(0 0 12px color-mix(in srgb, var(--constellation-signal-status-color) 46%, transparent));
      transform: rotate(174deg) scale(0.96);
    }

    46% {
      filter: drop-shadow(0 0 16px color-mix(in srgb, var(--constellation-signal-status-color) 58%, transparent));
      transform: rotate(268deg) scale(1.06);
    }

    64% {
      filter: drop-shadow(0 0 13px color-mix(in srgb, var(--constellation-signal-status-color) 48%, transparent));
      transform: rotate(312deg) scale(1);
    }

    100% {
      filter: drop-shadow(0 0 12px color-mix(in srgb, var(--constellation-signal-status-color) 42%, transparent));
      transform: rotate(360deg) scale(1);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .is-animated .constellation-signal-status-indicator-unread-beacon,
    .is-animated .constellation-signal-status-indicator-spinner {
      animation: none;
    }
  }
</style>
