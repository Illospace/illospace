<script lang="ts">
  import WorkspaceStageShell from '$lib/components/layout/WorkspaceStageShell.svelte';
  import type { Snippet } from 'svelte';

  type ThreadEdgeSide = 'top' | 'right' | 'bottom' | 'left';
  type ThreadSignalKind = 'attention' | 'reply' | 'progress' | 'risk';

  export type ThreadPeripherySignal = {
    side: ThreadEdgeSide;
    offset: number;
    strength: number;
    color: string;
    rgb: string;
    pulseMs: number;
    kind: ThreadSignalKind;
    count: number;
    related: boolean;
    span: number;
    opacity: number;
  };

  function normalizeOriginCoord(value: number | string | null | undefined, fallback: string) {
    if (typeof value === 'number') return `${value}px`;
    return value ?? fallback;
  }

  function hexToRgb(hex: string) {
    const clean = hex.replace('#', '');
    const normalized =
      clean.length === 3
        ? clean
            .split('')
            .map((char) => `${char}${char}`)
            .join('')
        : clean;

    const value = Number.parseInt(normalized, 16);
    if (Number.isNaN(value)) {
      return '87, 207, 160';
    }

    return `${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}`;
  }

  let {
    entering = false,
    ready = false,
    accentColor = '#57CFA0',
    accentRgb,
    origin = { x: '50%', y: '56%' },
    peripherySignals = [],
    ondismiss,
    children,
  }: {
    entering?: boolean;
    ready?: boolean;
    accentColor?: string;
    accentRgb?: string;
    origin?: { x: number | string; y: number | string } | null;
    peripherySignals?: ThreadPeripherySignal[];
    ondismiss?: () => void;
    children?: Snippet;
  } = $props();

  const shellStyle = $derived.by(() => {
    const resolvedAccentRgb = accentRgb ?? (accentColor.startsWith('#') ? hexToRgb(accentColor) : '87, 207, 160');
    const parts = [
      `--thread-accent:${accentColor}`,
      `--thread-accent-rgb:${resolvedAccentRgb}`,
      `--thread-origin-x:${normalizeOriginCoord(origin?.x, '50%')}`,
      `--thread-origin-y:${normalizeOriginCoord(origin?.y, '56%')}`,
    ];
    return parts.join('; ');
  });
  const workspaceShellStyle = '--workspace-stage-frame-width: var(--thread-stage-frame-width)';

  function handleDismiss() {
    ondismiss?.();
  }
</script>

<div class="thread-stage-shell thread-shell-presence" style={shellStyle}>
  <WorkspaceStageShell
    className="thread-stage-workspace"
    frameClassName="thread-stage-frame"
    {entering}
    {ready}
    style={workspaceShellStyle}
    dismissLabel="Leave thread"
    dismissCursor="zoom-out"
    ondismiss={handleDismiss}
  >
    {#snippet periphery()}
      {#if peripherySignals.length > 0}
        <div class="thread-periphery-layer" aria-hidden="true">
          {#each peripherySignals as cue, index (`${cue.side}-${Math.round(cue.offset)}-${index}`)}
            <div
              class={`thread-edge-signal side-${cue.side} kind-${cue.kind} ${cue.related ? 'is-related' : ''}`}
              style={`--signal-offset:${cue.offset}%; --signal-color:${cue.color}; --signal-rgb:${cue.rgb}; --signal-strength:${cue.strength}; --signal-duration:${cue.pulseMs}ms; --signal-span:${cue.span}px; --signal-opacity:${cue.opacity};`}
            >
              <div class="thread-edge-aura"></div>
              <div class="thread-edge-core"></div>
              {#if cue.count > 1}
                <div class="thread-edge-trace"></div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    {/snippet}

    {@render children?.()}
  </WorkspaceStageShell>
</div>

<style>
  .thread-stage-shell {
    --thread-stage-frame-width: clamp(1040px, 78vw, 1760px);
    position: absolute;
    inset: 0;
    z-index: 25;
  }

  .thread-stage-shell :global(.thread-stage-frame) {
    width: min(100%, var(--thread-stage-frame-width));
  }

  .thread-periphery-layer {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 2;
  }

  .thread-edge-signal {
    position: absolute;
    pointer-events: none;
    opacity: var(--signal-opacity);
  }

  .thread-edge-signal.side-left,
  .thread-edge-signal.side-right {
    width: 148px;
    height: var(--signal-span);
    top: calc(var(--signal-offset) - (var(--signal-span) / 2));
  }

  .thread-edge-signal.side-top,
  .thread-edge-signal.side-bottom {
    width: var(--signal-span);
    height: 148px;
    left: calc(var(--signal-offset) - (var(--signal-span) / 2));
  }

  .thread-edge-signal.side-left { left: 0; }
  .thread-edge-signal.side-right { right: 0; }
  .thread-edge-signal.side-top { top: 0; }
  .thread-edge-signal.side-bottom { bottom: 0; }

  .thread-edge-aura,
  .thread-edge-core,
  .thread-edge-trace {
    position: absolute;
    pointer-events: none;
  }

  .thread-edge-aura {
    inset: 0;
    filter: blur(18px);
    animation: thread-edge-breathe var(--signal-duration) ease-in-out infinite;
  }

  .thread-edge-core {
    animation: thread-edge-breathe var(--signal-duration) ease-in-out infinite;
  }

  .thread-edge-trace {
    opacity: 0.46;
    filter: blur(10px);
    animation: thread-edge-whisper calc(var(--signal-duration) * 1.35) ease-in-out infinite;
  }

  .thread-edge-signal.side-left .thread-edge-aura {
    background: radial-gradient(circle at 0% 50%, rgba(var(--signal-rgb), 0.22), rgba(var(--signal-rgb), 0.1) 28%, transparent 74%);
  }

  .thread-edge-signal.side-right .thread-edge-aura {
    background: radial-gradient(circle at 100% 50%, rgba(var(--signal-rgb), 0.22), rgba(var(--signal-rgb), 0.1) 28%, transparent 74%);
  }

  .thread-edge-signal.side-top .thread-edge-aura {
    background: radial-gradient(circle at 50% 0%, rgba(var(--signal-rgb), 0.2), rgba(var(--signal-rgb), 0.09) 28%, transparent 74%);
  }

  .thread-edge-signal.side-bottom .thread-edge-aura {
    background: radial-gradient(circle at 50% 100%, rgba(var(--signal-rgb), 0.2), rgba(var(--signal-rgb), 0.09) 28%, transparent 74%);
  }

  .thread-edge-signal.side-left .thread-edge-core,
  .thread-edge-signal.side-right .thread-edge-core {
    top: 16%;
    bottom: 16%;
    width: 3px;
    border-radius: 999px;
    background: linear-gradient(180deg, transparent, rgba(var(--signal-rgb), 0.42), transparent);
  }

  .thread-edge-signal.side-left .thread-edge-core {
    left: 4px;
  }

  .thread-edge-signal.side-right .thread-edge-core {
    right: 4px;
  }

  .thread-edge-signal.side-top .thread-edge-core,
  .thread-edge-signal.side-bottom .thread-edge-core {
    left: 16%;
    right: 16%;
    height: 3px;
    border-radius: 999px;
    background: linear-gradient(90deg, transparent, rgba(var(--signal-rgb), 0.42), transparent);
  }

  .thread-edge-signal.side-top .thread-edge-core {
    top: 4px;
  }

  .thread-edge-signal.side-bottom .thread-edge-core {
    bottom: 4px;
  }

  .thread-edge-signal.side-left .thread-edge-trace,
  .thread-edge-signal.side-right .thread-edge-trace {
    top: 24%;
    bottom: 24%;
    width: 1px;
    border-radius: 999px;
    background: linear-gradient(180deg, transparent, rgba(var(--signal-rgb), 0.28), transparent);
  }

  .thread-edge-signal.side-left .thread-edge-trace {
    left: 12px;
  }

  .thread-edge-signal.side-right .thread-edge-trace {
    right: 12px;
  }

  .thread-edge-signal.side-top .thread-edge-trace,
  .thread-edge-signal.side-bottom .thread-edge-trace {
    left: 24%;
    right: 24%;
    height: 1px;
    border-radius: 999px;
    background: linear-gradient(90deg, transparent, rgba(var(--signal-rgb), 0.28), transparent);
  }

  .thread-edge-signal.side-top .thread-edge-trace {
    top: 12px;
  }

  .thread-edge-signal.side-bottom .thread-edge-trace {
    bottom: 12px;
  }

  .thread-edge-signal.is-related .thread-edge-core {
    box-shadow: 0 0 18px rgba(var(--signal-rgb), 0.2);
  }

  .thread-edge-signal.kind-progress .thread-edge-aura {
    animation-duration: calc(var(--signal-duration) * 1.2);
  }

  .thread-edge-signal.kind-risk .thread-edge-core {
    filter: saturate(0.82);
  }

  @keyframes thread-edge-breathe {
    0%, 100% {
      opacity: calc(0.42 + (var(--signal-strength) * 0.2));
    }
    50% {
      opacity: calc(0.7 + (var(--signal-strength) * 0.24));
    }
  }

  @keyframes thread-edge-whisper {
    0%, 100% {
      opacity: 0.2;
    }
    50% {
      opacity: 0.46;
    }
  }

  @media (max-width: 900px) {
    .thread-edge-signal.side-left,
    .thread-edge-signal.side-right {
      width: 108px;
    }

    .thread-edge-signal.side-top,
    .thread-edge-signal.side-bottom {
      height: 108px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .thread-edge-aura,
    .thread-edge-core,
    .thread-edge-trace {
      animation: none !important;
    }
  }
</style>
