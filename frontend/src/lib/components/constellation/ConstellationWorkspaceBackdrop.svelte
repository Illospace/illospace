<script lang="ts">
  import type { Snippet } from 'svelte';

  type Props = {
    className?: string;
    canvasClassName?: string;
    toolbarClassName?: string;
    centerToolbarClassName?: string;
    composerClassName?: string;
    canvasUtilityClassName?: string;
    overlaysClassName?: string;
    toolbar?: Snippet;
    centerToolbar?: Snippet;
    canvas?: Snippet;
    composer?: Snippet;
    canvasUtility?: Snippet;
    overlays?: Snippet;
  };

  let {
    className = '',
    canvasClassName = '',
    toolbarClassName = '',
    centerToolbarClassName = '',
    composerClassName = '',
    canvasUtilityClassName = '',
    overlaysClassName = '',
    toolbar,
    centerToolbar,
    canvas,
    composer,
    canvasUtility,
    overlays,
  }: Props = $props();

  const rootClass = $derived(['constellation-workspace-backdrop', className].filter(Boolean).join(' '));
  const canvasSlotClass = $derived(
    ['constellation-workspace-backdrop-canvas', canvasClassName].filter(Boolean).join(' '),
  );
  const toolbarSlotClass = $derived(
    ['constellation-workspace-backdrop-toolbar-slot', toolbarClassName].filter(Boolean).join(' '),
  );
  const centerToolbarSlotClass = $derived(
    ['constellation-workspace-backdrop-center-toolbar-slot', centerToolbarClassName].filter(Boolean).join(' '),
  );
  const composerSlotClass = $derived(
    ['constellation-workspace-backdrop-composer-slot', composerClassName].filter(Boolean).join(' '),
  );
  const canvasUtilitySlotClass = $derived(
    ['constellation-workspace-backdrop-utility-slot', canvasUtilityClassName].filter(Boolean).join(' '),
  );
  const overlaysSlotClass = $derived(
    ['constellation-workspace-backdrop-overlays', overlaysClassName].filter(Boolean).join(' '),
  );
</script>

<section
  class={rootClass}
  data-design-component="ConstellationWorkspaceBackdrop"
  data-design-composition="ConstellationWorkspaceScreen"
>
  <div class="constellation-workspace-backdrop-underlay" aria-hidden="true">
    <div class="constellation-workspace-backdrop-surface"></div>
    <div class="constellation-workspace-backdrop-tide"></div>
    <div class="constellation-workspace-backdrop-deep-field"></div>
    <svg
      class="constellation-workspace-backdrop-waves"
      viewBox="0 0 1440 900"
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      <g class="constellation-workspace-backdrop-wave-set constellation-workspace-backdrop-wave-set-a">
        <path d="M -140 94 C 40 34 160 154 340 94 S 640 34 820 94 S 1120 154 1300 94 S 1580 34 1760 94" />
        <path d="M -160 206 C 20 146 150 266 330 206 S 650 146 830 206 S 1130 266 1310 206 S 1570 146 1750 206" />
        <path d="M -120 318 C 60 258 170 378 350 318 S 650 258 830 318 S 1130 378 1310 318 S 1590 258 1770 318" />
        <path d="M -150 432 C 30 372 150 492 330 432 S 650 372 830 432 S 1130 492 1310 432 S 1580 372 1760 432" />
      </g>
      <g class="constellation-workspace-backdrop-wave-set constellation-workspace-backdrop-wave-set-b">
        <path d="M -180 148 C 0 106 160 190 340 148 S 660 106 840 148 S 1160 190 1340 148 S 1660 106 1840 148" />
        <path d="M -170 260 C 10 218 150 302 330 260 S 650 218 830 260 S 1150 302 1330 260 S 1650 218 1830 260" />
        <path d="M -190 374 C -10 332 160 416 340 374 S 660 332 840 374 S 1160 416 1340 374 S 1660 332 1840 374" />
        <path d="M -170 488 C 10 446 150 530 330 488 S 650 446 830 488 S 1150 530 1330 488 S 1650 446 1830 488" />
      </g>
    </svg>
    <div class="constellation-workspace-backdrop-rays"></div>
    <div class="constellation-workspace-backdrop-caustics"></div>
    <div class="constellation-workspace-backdrop-scene-glow"></div>
    <div class="constellation-workspace-backdrop-scene-warmth"></div>
  </div>

  <div class={canvasSlotClass}>
    {#if canvas}
      {@render canvas()}
    {/if}
  </div>

  {#if toolbar}
    <div class={toolbarSlotClass}>
      {@render toolbar()}
    </div>
  {/if}

  {#if centerToolbar}
    <div class={centerToolbarSlotClass}>
      {@render centerToolbar()}
    </div>
  {/if}

  {#if canvasUtility}
    <aside class={canvasUtilitySlotClass}>
      {@render canvasUtility()}
    </aside>
  {/if}

  {#if composer}
    <div class={composerSlotClass}>
      {@render composer()}
    </div>
  {/if}

  {#if overlays}
    <div class={overlaysSlotClass}>
      {@render overlays()}
    </div>
  {/if}
</section>

<style>
  .constellation-workspace-backdrop {
    --constellation-workspace-backdrop-shell-gap: 16px;
    --constellation-workspace-backdrop-toolbar-top: var(--constellation-workspace-backdrop-shell-gap);
    --constellation-workspace-backdrop-toolbar-right: var(--constellation-workspace-backdrop-shell-gap);
    --constellation-workspace-backdrop-composer-bottom: var(--constellation-workspace-backdrop-shell-gap);
    --constellation-workspace-backdrop-composer-width: min(640px, calc(100% - 56px));
    --constellation-workspace-backdrop-utility-top: 96px;
    --constellation-workspace-backdrop-utility-bottom: 112px;
    --constellation-workspace-backdrop-utility-width: min(340px, calc(100% - 48px));
    --constellation-workspace-backdrop-utility-z-index: 2;
    --constellation-workspace-background: var(
      --constellation-workspace-theme-background,
      radial-gradient(circle at 54% 18%, rgba(255, 255, 255, 0.04), transparent 10%),
      radial-gradient(circle at 50% 50%, rgba(255, 255, 255, 0.015), transparent 42%),
      linear-gradient(180deg, rgba(0, 0, 0, 0.98), rgba(4, 7, 13, 0.94))
    );
    --constellation-workspace-star-color-a: var(
      --constellation-workspace-theme-star-color-a,
      rgba(240, 240, 250, 0.52)
    );
    --constellation-workspace-star-color-b: var(
      --constellation-workspace-theme-star-color-b,
      color-mix(in srgb, var(--constellation-color-spectral) 18%, transparent)
    );
    --constellation-workspace-star-color-c: var(
      --constellation-workspace-theme-star-color-c,
      color-mix(in srgb, var(--constellation-color-amber) 16%, transparent)
    );
    --constellation-workspace-star-opacity: var(--constellation-workspace-theme-star-opacity, 0.28);
    --constellation-workspace-scene-glow: var(
      --constellation-workspace-theme-scene-glow,
      radial-gradient(circle, color-mix(in srgb, var(--constellation-color-spectral) 28%, transparent), transparent 72%)
    );
    --constellation-workspace-scene-warmth: var(
      --constellation-workspace-theme-scene-warmth,
      radial-gradient(circle, color-mix(in srgb, var(--constellation-color-amber) 24%, transparent), transparent 74%)
    );
    --constellation-workspace-surface-ripples: var(--constellation-workspace-theme-surface-ripples, none);
    --constellation-workspace-surface-opacity: var(--constellation-workspace-theme-surface-opacity, 0);
    --constellation-workspace-color-tide: var(--constellation-workspace-theme-color-tide, none);
    --constellation-workspace-color-tide-opacity: var(--constellation-workspace-theme-color-tide-opacity, 0);
    --constellation-workspace-color-tide-blend-mode: var(
      --constellation-workspace-theme-color-tide-blend-mode,
      normal
    );
    --constellation-workspace-color-current: var(--constellation-workspace-theme-color-current, none);
    --constellation-workspace-color-current-alt: var(--constellation-workspace-theme-color-current-alt, none);
    --constellation-workspace-color-current-opacity: var(--constellation-workspace-theme-color-current-opacity, 0);
    --constellation-workspace-wave-color: var(--constellation-workspace-theme-wave-color, rgba(255, 255, 255, 0.64));
    --constellation-workspace-wave-opacity: var(--constellation-workspace-theme-wave-opacity, 0);
    --constellation-workspace-wave-stroke-width: var(--constellation-workspace-theme-wave-stroke-width, 1px);
    --constellation-workspace-rays: var(--constellation-workspace-theme-rays, none);
    --constellation-workspace-rays-opacity: var(--constellation-workspace-theme-rays-opacity, 0);
    --constellation-workspace-caustics: var(--constellation-workspace-theme-caustics, none);
    --constellation-workspace-caustics-opacity: var(--constellation-workspace-theme-caustics-opacity, 0);
    --constellation-workspace-caustics-size: var(--constellation-workspace-theme-caustics-size, auto);
    --constellation-workspace-caustics-blend-mode: var(--constellation-workspace-theme-caustics-blend-mode, normal);
    --constellation-workspace-deep-field-opacity: var(--constellation-workspace-theme-deep-field-opacity, 0);
    --constellation-workspace-deep-field-nebula: var(--constellation-workspace-theme-deep-field-nebula, none);
    --constellation-workspace-deep-field-stars: var(--constellation-workspace-theme-deep-field-stars, none);
    --constellation-workspace-deep-field-dust: var(--constellation-workspace-theme-deep-field-dust, none);
    --constellation-workspace-water-animation-state: var(
      --constellation-workspace-theme-water-animation-state,
      paused
    );

    position: relative;
    width: 100%;
    height: 100%;
    min-height: max(620px, 100%);
    overflow: hidden;
    isolation: isolate;
    background: var(--constellation-workspace-background);
  }

  .constellation-workspace-backdrop::before {
    content: '';
    position: absolute;
    inset: 0;
    z-index: 0;
    background-image:
      radial-gradient(var(--constellation-workspace-star-color-a) 0.7px, transparent 0.7px),
      radial-gradient(var(--constellation-workspace-star-color-b) 0.8px, transparent 0.8px),
      radial-gradient(var(--constellation-workspace-star-color-c) 0.8px, transparent 0.8px);
    background-position:
      0 0,
      32px 84px,
      120px 24px;
    background-size:
      210px 210px,
      300px 300px,
      360px 360px;
    opacity: var(--constellation-workspace-star-opacity);
    pointer-events: none;
  }

  .constellation-workspace-backdrop-underlay,
  .constellation-workspace-backdrop-overlays {
    position: absolute;
    inset: 0;
  }

  .constellation-workspace-backdrop-underlay {
    pointer-events: none;
    z-index: 0;
  }

  .constellation-workspace-backdrop-scene-glow,
  .constellation-workspace-backdrop-scene-warmth,
  .constellation-workspace-backdrop-surface,
  .constellation-workspace-backdrop-tide,
  .constellation-workspace-backdrop-deep-field,
  .constellation-workspace-backdrop-waves,
  .constellation-workspace-backdrop-rays,
  .constellation-workspace-backdrop-caustics {
    position: absolute;
    pointer-events: none;
  }

  .constellation-workspace-backdrop-scene-glow,
  .constellation-workspace-backdrop-scene-warmth {
    border-radius: 999px;
    filter: blur(18px);
  }

  .constellation-workspace-backdrop-surface {
    inset: -14% -8% auto;
    height: 42%;
    background: var(--constellation-workspace-surface-ripples);
    opacity: var(--constellation-workspace-surface-opacity);
    transform-origin: 50% 0%;
    animation: constellation-workspace-surface-drift 18s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-tide {
    inset: -24% -18%;
    background: var(--constellation-workspace-color-tide);
    background-position:
      0% 20%,
      100% 16%,
      54% 88%,
      28% 72%;
    background-size:
      122% 118%,
      116% 112%,
      128% 124%,
      108% 104%;
    opacity: var(--constellation-workspace-color-tide-opacity);
    mix-blend-mode: var(--constellation-workspace-color-tide-blend-mode);
    filter: blur(18px) saturate(1.1);
    overflow: hidden;
    transform-origin: 50% 48%;
    -webkit-mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.88), rgba(0, 0, 0, 0.72) 58%, transparent 98%);
    mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.88), rgba(0, 0, 0, 0.72) 58%, transparent 98%);
    animation: constellation-workspace-tide-colors 14s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-tide::before,
  .constellation-workspace-backdrop-tide::after {
    content: '';
    position: absolute;
    inset: -16% -10%;
    background-size: 220% 100%;
    opacity: var(--constellation-workspace-color-current-opacity);
    filter: blur(18px);
    mix-blend-mode: soft-light;
    transform-origin: 50% 42%;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-tide::before {
    background-image: var(--constellation-workspace-color-current);
    animation: constellation-workspace-current-sweep-a 11s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-tide::after {
    background-image: var(--constellation-workspace-color-current-alt);
    opacity: calc(var(--constellation-workspace-color-current-opacity) * 0.72);
    animation: constellation-workspace-current-sweep-b 15s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-deep-field {
    inset: 0;
    overflow: hidden;
    opacity: var(--constellation-workspace-deep-field-opacity);
    mix-blend-mode: screen;
    background: var(--constellation-workspace-deep-field-nebula);
  }

  .constellation-workspace-backdrop-deep-field::before,
  .constellation-workspace-backdrop-deep-field::after {
    content: '';
    position: absolute;
    inset: -4%;
    pointer-events: none;
  }

  .constellation-workspace-backdrop-deep-field::before {
    background-image: var(--constellation-workspace-deep-field-stars);
    background-position:
      18px 28px,
      92px 10px,
      160px 140px,
      0 0;
    background-size:
      154px 154px,
      263px 263px,
      391px 391px,
      520px 520px;
    opacity: 0.82;
  }

  .constellation-workspace-backdrop-deep-field::after {
    background-image: var(--constellation-workspace-deep-field-dust);
    background-position:
      12% 18%,
      76% 24%,
      50% 82%;
    background-size:
      420px 320px,
      520px 380px,
      680px 440px;
    opacity: 0.74;
  }

  .constellation-workspace-backdrop-waves {
    inset: -9% -10%;
    width: 120%;
    height: 116%;
    color: var(--constellation-workspace-wave-color);
    opacity: var(--constellation-workspace-wave-opacity);
    mix-blend-mode: soft-light;
    filter: blur(0.35px);
    transform-origin: 50% 22%;
    -webkit-mask-image:
      radial-gradient(ellipse 88% 58% at 50% 20%, rgba(0, 0, 0, 0.92), transparent 76%),
      linear-gradient(180deg, rgba(0, 0, 0, 0.86), rgba(0, 0, 0, 0.58) 58%, transparent 94%);
    mask-image:
      radial-gradient(ellipse 88% 58% at 50% 20%, rgba(0, 0, 0, 0.92), transparent 76%),
      linear-gradient(180deg, rgba(0, 0, 0, 0.86), rgba(0, 0, 0, 0.58) 58%, transparent 94%);
    animation: constellation-workspace-wave-field 18s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-wave-set {
    fill: none;
    stroke: currentColor;
    stroke-linecap: round;
    stroke-width: var(--constellation-workspace-wave-stroke-width);
    vector-effect: non-scaling-stroke;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-wave-set-a {
    opacity: 0.72;
    animation: constellation-workspace-wave-phase-a 24s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-wave-set-b {
    opacity: 0.42;
    animation: constellation-workspace-wave-phase-b 30s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-rays {
    inset: -6% -10% 0;
    background: var(--constellation-workspace-rays);
    opacity: var(--constellation-workspace-rays-opacity);
    mix-blend-mode: soft-light;
    -webkit-mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.92), rgba(0, 0, 0, 0.52) 44%, transparent 86%);
    mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.92), rgba(0, 0, 0, 0.52) 44%, transparent 86%);
    animation: constellation-workspace-ray-breathe 26s ease-in-out infinite alternate;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-caustics {
    inset: -10%;
    background: var(--constellation-workspace-caustics);
    background-size: var(--constellation-workspace-caustics-size);
    opacity: var(--constellation-workspace-caustics-opacity);
    mix-blend-mode: var(--constellation-workspace-caustics-blend-mode);
    filter: blur(0.5px);
    -webkit-mask-image: linear-gradient(180deg, transparent 0%, rgba(0, 0, 0, 0.82) 14%, rgba(0, 0, 0, 0.68) 62%, transparent 94%);
    mask-image: linear-gradient(180deg, transparent 0%, rgba(0, 0, 0, 0.82) 14%, rgba(0, 0, 0, 0.68) 62%, transparent 94%);
    transform-origin: 50% 24%;
    animation: constellation-workspace-caustic-drift 34s linear infinite;
    animation-play-state: var(--constellation-workspace-water-animation-state);
  }

  .constellation-workspace-backdrop-scene-glow {
    top: 92px;
    left: 360px;
    width: 220px;
    height: 220px;
    background: var(--constellation-workspace-scene-glow);
  }

  .constellation-workspace-backdrop-scene-warmth {
    left: 44%;
    top: 42%;
    width: 420px;
    height: 320px;
    background: var(--constellation-workspace-scene-warmth);
  }

  .constellation-workspace-backdrop-canvas {
    position: relative;
    z-index: 1;
    min-height: 100%;
    height: 100%;
  }

  .constellation-workspace-backdrop-toolbar-slot,
  .constellation-workspace-backdrop-center-toolbar-slot,
  .constellation-workspace-backdrop-composer-slot,
  .constellation-workspace-backdrop-utility-slot {
    position: absolute;
  }

  .constellation-workspace-backdrop-toolbar-slot {
    top: var(--constellation-workspace-backdrop-toolbar-top);
    right: var(--constellation-workspace-backdrop-toolbar-right);
    z-index: 20;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    gap: 10px;
    pointer-events: none;
  }

  .constellation-workspace-backdrop-toolbar-slot > :global(*) {
    pointer-events: auto;
  }

  .constellation-workspace-backdrop-center-toolbar-slot {
    top: var(--constellation-workspace-backdrop-toolbar-top);
    left: 50%;
    z-index: 20;
    display: flex;
    justify-content: center;
    pointer-events: none;
    transform: translateX(-50%);
  }

  .constellation-workspace-backdrop-center-toolbar-slot > :global(*) {
    pointer-events: auto;
  }

  .constellation-workspace-backdrop-composer-slot {
    left: 50%;
    bottom: var(--constellation-workspace-backdrop-composer-bottom);
    z-index: 22;
    width: var(--constellation-workspace-backdrop-composer-width);
    transform: translateX(-50%);
  }

  .constellation-workspace-backdrop-utility-slot {
    top: var(--constellation-workspace-backdrop-utility-top);
    right: var(--constellation-workspace-backdrop-toolbar-right);
    bottom: var(--constellation-workspace-backdrop-utility-bottom);
    z-index: var(--constellation-workspace-backdrop-utility-z-index);
    width: var(--constellation-workspace-backdrop-utility-width);
    display: flex;
    justify-content: flex-end;
    align-items: stretch;
  }

  .constellation-workspace-backdrop-utility-slot > :global(*) {
    width: 100%;
    min-height: 0;
  }

  .constellation-workspace-backdrop-overlays {
    z-index: 30;
    pointer-events: none;
  }

  .constellation-workspace-backdrop-overlays > :global(*) {
    pointer-events: auto;
  }

  .constellation-workspace-backdrop-utility-slot > :global(.canvas-wrapper) {
    flex: 1 1 auto;
    width: 100%;
    max-width: none;
    min-width: 0;
    height: 100%;
  }

  @media (max-width: 980px) {
    .constellation-workspace-backdrop {
      --constellation-workspace-backdrop-shell-gap: 16px;
      --constellation-workspace-backdrop-composer-width: min(calc(100% - 24px), 560px);
      --constellation-workspace-backdrop-utility-top: 82px;
      --constellation-workspace-backdrop-utility-bottom: 104px;
      --constellation-workspace-backdrop-utility-width: calc(100% - 32px);
    }

    .constellation-workspace-backdrop-scene-glow {
      left: 26%;
    }

    .constellation-workspace-backdrop-scene-warmth {
      left: 34%;
      width: 320px;
      height: 260px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .constellation-workspace-backdrop-surface,
    .constellation-workspace-backdrop-tide,
    .constellation-workspace-backdrop-tide::before,
    .constellation-workspace-backdrop-tide::after,
    .constellation-workspace-backdrop-waves,
    .constellation-workspace-backdrop-wave-set,
    .constellation-workspace-backdrop-rays,
    .constellation-workspace-backdrop-caustics {
      animation: none;
    }
  }

  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-surface,
  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-tide,
  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-tide::before,
  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-tide::after,
  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-waves,
  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-wave-set,
  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-rays,
  :global(:root[data-color-scheme='light']) .constellation-workspace-backdrop-caustics {
    animation: none;
  }

  @keyframes constellation-workspace-tide-colors {
    0% {
      background-position:
        0% 20%,
        100% 16%,
        54% 88%,
        28% 72%;
      transform: translate3d(-5.2%, -1.6%, 0) scale(1.04) rotate(-0.35deg);
    }
    50% {
      background-position:
        16% 26%,
        82% 22%,
        62% 76%,
        40% 64%;
      transform: translate3d(4.6%, 1.2%, 0) scale(1.08) rotate(0.24deg);
    }
    100% {
      background-position:
        26% 18%,
        70% 30%,
        44% 82%,
        52% 68%;
      transform: translate3d(-1.4%, 2.4%, 0) scale(1.06) rotate(0.38deg);
    }
  }

  @keyframes constellation-workspace-current-sweep-a {
    from {
      background-position: 0% 50%;
      transform: translate3d(-8%, -1.4%, 0) skewY(-1.2deg) scale(1.02);
    }
    to {
      background-position: 100% 48%;
      transform: translate3d(6%, 1.6%, 0) skewY(1deg) scale(1.045);
    }
  }

  @keyframes constellation-workspace-current-sweep-b {
    from {
      background-position: 96% 50%;
      transform: translate3d(7%, 2.2%, 0) skewY(1.1deg) scale(1.03);
    }
    to {
      background-position: 0% 48%;
      transform: translate3d(-7%, -1.2%, 0) skewY(-1deg) scale(1.06);
    }
  }

  @keyframes constellation-workspace-wave-field {
    from {
      transform: translate3d(-1.4%, -0.5%, 0) scale(1.015) rotate(-0.16deg);
    }
    to {
      transform: translate3d(1.3%, 0.7%, 0) scale(1.035) rotate(0.14deg);
    }
  }

  @keyframes constellation-workspace-wave-phase-a {
    from {
      transform: translate3d(-64px, -6px, 0);
    }
    to {
      transform: translate3d(58px, 8px, 0);
    }
  }

  @keyframes constellation-workspace-wave-phase-b {
    from {
      transform: translate3d(56px, 8px, 0);
    }
    to {
      transform: translate3d(-72px, -8px, 0);
    }
  }

  @keyframes constellation-workspace-surface-drift {
    from {
      transform: translate3d(-1.2%, -0.4%, 0) scale(1.01);
    }
    to {
      transform: translate3d(1.1%, 0.7%, 0) scale(1.035);
    }
  }

  @keyframes constellation-workspace-ray-breathe {
    from {
      transform: translate3d(-0.6%, -0.4%, 0) scale(1.01);
    }
    to {
      transform: translate3d(0.7%, 0.6%, 0) scale(1.035);
    }
  }

  @keyframes constellation-workspace-caustic-drift {
    from {
      background-position:
        0 0,
        46px -28px,
        -32px 24px,
        0 0;
      transform: translate3d(-1.4%, -0.6%, 0) rotate(-0.35deg) scale(1.02);
    }
    to {
      background-position:
        140px 72px,
        -92px 58px,
        84px -40px,
        0 0;
      transform: translate3d(1.4%, 0.9%, 0) rotate(0.35deg) scale(1.04);
    }
  }
</style>
