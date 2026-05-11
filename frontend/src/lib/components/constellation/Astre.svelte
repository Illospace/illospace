<script lang="ts">
  import type {
    ConstellationActivity,
    ConstellationAstrePresence,
    ConstellationScale,
    ConstellationTone,
  } from './constellationTypes';

  type SemanticZoomLevel = 'detail' | 'summary' | 'symbol' | 'glyph';
  type ArchiveDotLayer = 'outer' | 'inner';
  type ArchiveDotVariant = 'a' | 'b' | 'c';

  type ArchiveDotWaypoint = {
    top: string;
    left: string;
  };

  type ArchiveDotPreset = {
    layer: ArchiveDotLayer;
    waypoints: [ArchiveDotWaypoint, ArchiveDotWaypoint, ArchiveDotWaypoint, ArchiveDotWaypoint];
    size: number;
    variant: ArchiveDotVariant;
    delay: string;
  };

  const archiveDotPresets: ArchiveDotPreset[] = [
    {
      layer: 'outer',
      waypoints: [
        { top: '12%', left: '86%' },
        { top: '30%', left: '60%' },
        { top: '76%', left: '78%' },
        { top: '88%', left: '28%' },
      ],
      size: 4,
      variant: 'a',
      delay: '-4.2s',
    },
    {
      layer: 'inner',
      waypoints: [
        { top: '76%', left: '74%' },
        { top: '36%', left: '66%' },
        { top: '24%', left: '30%' },
        { top: '64%', left: '22%' },
      ],
      size: 3,
      variant: 'b',
      delay: '-11.6s',
    },
    {
      layer: 'outer',
      waypoints: [
        { top: '102%', left: '72%' },
        { top: '70%', left: '94%' },
        { top: '34%', left: '56%' },
        { top: '18%', left: '18%' },
      ],
      size: 3,
      variant: 'c',
      delay: '-17.8s',
    },
    {
      layer: 'inner',
      waypoints: [
        { top: '24%', left: '22%' },
        { top: '20%', left: '64%' },
        { top: '58%', left: '76%' },
        { top: '74%', left: '38%' },
      ],
      size: 3,
      variant: 'b',
      delay: '-8.4s',
    },
    {
      layer: 'outer',
      waypoints: [
        { top: '92%', left: '10%' },
        { top: '56%', left: '18%' },
        { top: '24%', left: '44%' },
        { top: '68%', left: '94%' },
      ],
      size: 3,
      variant: 'a',
      delay: '-14.1s',
    },
  ];

  let {
    letter,
    owner,
    tone = 'spectral',
    scale = 'standard',
    semanticLevel = 'detail',
    activity = 'idle',
    presence = 'online',
    archivedCount = 3,
    animated = true,
    interactive = false,
    className = '',
    style = '',
    onpointerenter,
    onpointerleave,
    onclick,
  }: {
    letter: string;
    owner: string;
    tone?: ConstellationTone;
    scale?: ConstellationScale;
    semanticLevel?: SemanticZoomLevel;
    activity?: ConstellationActivity;
    presence?: ConstellationAstrePresence;
    archivedCount?: number;
    animated?: boolean;
    interactive?: boolean;
    className?: string;
    style?: string;
    onpointerenter?: (event: PointerEvent) => void;
    onpointerleave?: (event: PointerEvent) => void;
    onclick?: (event: MouseEvent) => void;
  } = $props();

  const archivedDots = $derived(
    archiveDotPresets.slice(0, Math.max(0, Math.min(archivedCount, archiveDotPresets.length))),
  );
  const outerArchivedDots = $derived(archivedDots.filter((dot) => dot.layer === 'outer'));
  const innerArchivedDots = $derived(archivedDots.filter((dot) => dot.layer === 'inner'));
  const ownerLabel = $derived(owner.trim().toUpperCase());
  const showOwnerLabel = $derived(scale !== 'compact' && semanticLevel === 'detail' && ownerLabel.length > 0);

  const rootClass = $derived(
    [
      'constellation-astre',
      `constellation-astre-${tone}`,
      `constellation-astre-${scale}`,
      `constellation-astre-semantic-${semanticLevel}`,
      `constellation-astre-${activity}`,
      `constellation-astre-presence-${presence}`,
      animated ? 'is-animated' : '',
      interactive ? 'constellation-astre-interactive' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );

  function archiveDotVariantClass(variant: ArchiveDotVariant) {
    if (variant === 'a') return 'constellation-astre-archive-dot-a';
    if (variant === 'b') return 'constellation-astre-archive-dot-b';
    return 'constellation-astre-archive-dot-c';
  }

  function archiveDotStyle(dot: ArchiveDotPreset) {
    const [point0, point1, point2, point3] = dot.waypoints;

    return [
      `top: ${point0.top}`,
      `left: ${point0.left}`,
      `width: ${dot.size}px`,
      `height: ${dot.size}px`,
      `animation-delay: ${dot.delay}`,
      `--archive-top-0: ${point0.top}`,
      `--archive-left-0: ${point0.left}`,
      `--archive-top-1: ${point1.top}`,
      `--archive-left-1: ${point1.left}`,
      `--archive-top-2: ${point2.top}`,
      `--archive-left-2: ${point2.left}`,
      `--archive-top-3: ${point3.top}`,
      `--archive-left-3: ${point3.left}`,
    ].join('; ');
  }
</script>

{#snippet astreBody()}
  <div class="constellation-astre-halo"></div>
  <div class="constellation-astre-ring"></div>

  {#if outerArchivedDots.length > 0}
    <div class="constellation-astre-archive-outer-field" aria-hidden="true">
      {#each outerArchivedDots as dot, index}
        <span
          class={`constellation-astre-archive-dot ${archiveDotVariantClass(dot.variant)}`}
          style={archiveDotStyle(dot)}
          aria-hidden="true"
        ></span>
      {/each}
    </div>
  {/if}

  <div class="constellation-astre-core">
    {#if innerArchivedDots.length > 0}
      <div class="constellation-astre-archive-inner-field" aria-hidden="true">
        {#each innerArchivedDots as dot, index}
          <span
            class={`constellation-astre-archive-dot constellation-astre-archive-dot-inner ${archiveDotVariantClass(dot.variant)}`}
            style={archiveDotStyle(dot)}
            aria-hidden="true"
          ></span>
        {/each}
      </div>
    {/if}

    <span class="constellation-astre-letter">{letter}</span>
    {#if showOwnerLabel}
      <span class="constellation-astre-owner">{ownerLabel}</span>
    {/if}
  </div>
  <span class="constellation-astre-presence-dot" aria-hidden="true"></span>
{/snippet}

{#if interactive}
  <button
    type="button"
    class={rootClass}
    aria-label={owner}
    {style}
    onpointerenter={onpointerenter}
    onpointerleave={onpointerleave}
    onclick={onclick}
  >
    {@render astreBody()}
  </button>
{:else}
  <div class={rootClass} aria-label={owner} {style}>
    {@render astreBody()}
  </div>
{/if}

<style>
  .constellation-astre {
    position: absolute;
    display: grid;
    place-items: center;
    padding: 0;
    border: 0;
    appearance: none;
    background: transparent;
    color: inherit;
    font: inherit;
    text-align: inherit;
    isolation: isolate;
    transform: translate(-50%, -50%);
    pointer-events: none;
    --astre-halo-pulse-duration: var(--constellation-motion-halo-pulse-duration);
    --astre-core-drift-duration: var(--constellation-motion-drift-duration);
    --astre-ring-drift-duration: var(--constellation-motion-ring-drift-duration);
    --astre-ring-drift-slow-duration: var(--constellation-motion-ring-drift-slow-duration);
    --astre-ring-drift-quick-duration: var(--constellation-motion-ring-drift-quick-duration);
    --astre-outer-ring-opacity: 0.32;
    --astre-halo-rest-opacity: 0.58;
    --astre-halo-inner-opacity: 0;
    --astre-halo-pulse-min: 0.44;
    --astre-halo-pulse-max: 0.68;
    --astre-ring-opacity: 0.84;
    --astre-ring-filter: saturate(1.1) brightness(1.06);
    --astre-archive-opacity: 0.88;
    --astre-archive-filter: saturate(1.14) brightness(1.08);
    --astre-archive-drift-a-duration: 28s;
    --astre-archive-drift-b-duration: 34s;
    --astre-archive-drift-c-duration: 40s;
    --astre-core-opacity: 1;
    --astre-core-filter: none;
    --astre-tone-color: var(--constellation-color-spectral);
    --astre-before-border: rgba(141, 183, 255, 0.24);
    --astre-halo-border: rgba(141, 183, 255, 0.18);
    --astre-halo-inner-border: rgba(141, 183, 255, 0.12);
    --astre-ring-shadow: var(--constellation-shadow-astre-spectral);
    --astre-core-background:
      radial-gradient(circle at 42% 26%, color-mix(in srgb, var(--astre-tone-color) 14%, rgba(255, 242, 218, 0.028)) 0%, transparent 18%),
      radial-gradient(circle at 50% 54%, color-mix(in srgb, var(--astre-tone-color) 9%, rgba(22, 19, 18, 0.99)) 0%, rgba(5, 7, 12, 0.99) 78%);
    --astre-core-color: var(--constellation-color-spectral-owner);
    --astre-core-border-color: rgba(255, 234, 196, 0.18);
    --astre-core-inner-stroke: rgba(255, 244, 226, 0.055);
    --astre-core-glow-strong: rgba(141, 183, 255, 0.38);
    --astre-core-glow-soft: rgba(141, 183, 255, 0.2);
    --astre-core-sheen-opacity: 0.42;
    --astre-core-sheen-hot: color-mix(in srgb, var(--astre-tone-color) 14%, rgba(255, 246, 225, 0.04));
    --astre-core-sheen-top: rgba(255, 236, 202, 0.026);
    --astre-ring-border: color-mix(in srgb, var(--astre-rim-soft) 64%, rgba(255, 244, 220, 0.12));
    --astre-core-transform: none;
    --astre-core-shadow:
      inset 0 0 0 1px var(--astre-core-inner-stroke),
      inset 0 18px 32px rgba(255, 236, 200, 0.034),
      inset 0 -38px 52px rgba(0, 0, 0, 0.34),
      0 0 30px var(--astre-core-glow-strong),
      0 0 78px var(--astre-core-glow-soft);
    --astre-emphasis-ring-filter: saturate(1.3) brightness(1.16);
    --astre-emphasis-archive-filter: saturate(1.24) brightness(1.16);
    --astre-emphasis-core-filter: saturate(1.28) brightness(1.08);
    --astre-emphasis-core-transform: scale(1.05);
    --astre-emphasis-ring-border: color-mix(in srgb, var(--astre-rim-hot) 58%, rgba(255, 244, 220, 0.18));
    --astre-emphasis-ring-shadow:
      0 0 20px color-mix(in srgb, var(--astre-tone-color) 36%, transparent),
      0 0 58px color-mix(in srgb, var(--astre-tone-color) 18%, transparent);
    --astre-emphasis-core-shadow:
      inset 0 0 0 1px var(--astre-core-inner-stroke),
      inset 0 18px 32px rgba(255, 236, 200, 0.026),
      inset 0 -38px 52px rgba(0, 0, 0, 0.38),
      0 0 34px var(--astre-core-glow-strong),
      0 0 86px var(--astre-core-glow-soft);
    --astre-rim-hot: color-mix(in srgb, var(--astre-tone-color) 52%, #ffe8bd 48%);
    --astre-rim-soft: color-mix(in srgb, var(--astre-tone-color) 34%, rgba(255, 235, 204, 0.36));
    --astre-rim-dim: color-mix(in srgb, var(--astre-tone-color) 14%, transparent);
    --astre-rim-opacity: 1;
    --astre-scale: 1;
    --astre-diffuse-x: 42%;
    --astre-diffuse-y: 28%;
    --astre-frame-radius: 50%;
    --astre-before-inset: -22px;
    --astre-before-transform: none;
    --astre-halo-inset: -36px;
    --astre-halo-before-inset: 0;
    --astre-halo-before-transform: none;
    --astre-letter-offset-x: 0px;
    --astre-letter-offset-y: 0px;
    transform: translate(-50%, -50%) scale(var(--astre-scale));
    transform-origin: center;
    transition: transform var(--constellation-motion-hover-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-astre-interactive {
    pointer-events: auto;
    cursor: pointer;
  }

  .constellation-astre::before {
    content: '';
    position: absolute;
    inset: var(--astre-before-inset);
    border-radius: var(--astre-frame-radius);
    border: 1px dotted var(--astre-before-border);
    opacity: var(--astre-outer-ring-opacity);
    transform: var(--astre-before-transform);
    pointer-events: none;
  }

  .constellation-astre-halo,
  .constellation-astre-ring,
  .constellation-astre-core {
    position: absolute;
    inset: 0;
    border-radius: var(--astre-frame-radius);
  }

  .constellation-astre-presence-dot {
    display: none;
  }

  .constellation-astre-presence-offline .constellation-astre-presence-dot {
    background: rgba(180, 184, 196, 0.72);
    box-shadow:
      0 0 0 3px rgba(5, 8, 14, 0.3),
      0 0 10px rgba(210, 214, 226, 0.1);
    opacity: 0.54;
  }

  .constellation-astre-halo {
    inset: var(--astre-halo-inset);
    border: 0;
    background: radial-gradient(
      circle at var(--astre-diffuse-x) var(--astre-diffuse-y),
      color-mix(in srgb, var(--astre-tone-color) 30%, transparent) 0%,
      color-mix(in srgb, var(--astre-tone-color) 12%, transparent) 38%,
      transparent 76%
    );
    filter: blur(24px) saturate(1.22);
    opacity: var(--astre-halo-rest-opacity);
    transform: translateZ(0);
    transition:
      opacity var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      filter var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-astre-halo::before {
    display: none;
  }

  .constellation-astre-ring {
    inset: -1px;
    border: 1px solid var(--astre-ring-border);
    box-shadow: var(--astre-ring-shadow);
    opacity: var(--astre-ring-opacity);
    filter: var(--astre-ring-filter);
    transition:
      opacity var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      border-color var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      filter var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      box-shadow var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-astre-archive-outer-field {
    position: absolute;
    inset: -18px;
    pointer-events: none;
  }

  .constellation-astre-archive-inner-field {
    position: absolute;
    inset: 0;
    border-radius: inherit;
    pointer-events: none;
  }

  .constellation-astre-archive-dot {
    position: absolute;
    border-radius: 50%;
    transform: translate(-50%, -50%);
    background: var(--astre-tone-color);
    box-shadow: 0 0 8px color-mix(in srgb, var(--astre-tone-color) 72%, transparent);
    opacity: var(--astre-archive-opacity);
    filter: var(--astre-archive-filter);
  }

  .constellation-astre-archive-dot-inner {
    opacity: calc(var(--astre-archive-opacity) * 0.78);
  }

  .constellation-astre-core {
    display: flex;
    overflow: hidden;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 12px;
    font-family: var(--constellation-font-display);
    font-weight: 700;
    border: 1px solid var(--astre-core-border-color);
    background: var(--astre-core-background);
    color: var(--astre-core-color);
    opacity: var(--astre-core-opacity);
    filter: var(--astre-core-filter);
    transform: var(--astre-core-transform);
    box-shadow: var(--astre-core-shadow);
    transition:
      transform var(--constellation-motion-hover-duration) var(--constellation-motion-ease-lift),
      filter var(--constellation-motion-hover-duration) var(--constellation-motion-ease-lift),
      box-shadow var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-astre-core::before,
  .constellation-astre-core::after {
    content: '';
    position: absolute;
    inset: 0;
    border-radius: inherit;
    pointer-events: none;
  }

  .constellation-astre-core::before {
    background:
      radial-gradient(circle at var(--astre-diffuse-x) var(--astre-diffuse-y), var(--astre-core-sheen-hot) 0%, transparent 18%),
      linear-gradient(180deg, var(--astre-core-sheen-top), transparent 34%);
    opacity: var(--astre-core-sheen-opacity);
  }

  .constellation-astre-core::after {
    padding: 1px;
    background: conic-gradient(
      from -48deg,
      var(--astre-rim-hot) 0deg,
      var(--astre-rim-soft) 54deg,
      transparent 130deg,
      transparent 248deg,
      var(--astre-rim-dim) 306deg,
      var(--astre-rim-hot) 360deg
    );
    box-shadow:
      inset 0 0 30px rgba(0, 0, 0, 0.26),
      inset 0 -22px 32px rgba(0, 0, 0, 0.34);
    filter: drop-shadow(0 0 8px color-mix(in srgb, var(--astre-rim-hot) 54%, transparent));
    opacity: var(--astre-rim-opacity);
    mask:
      linear-gradient(#000 0 0) content-box,
      linear-gradient(#000 0 0);
    mask-composite: exclude;
    -webkit-mask:
      linear-gradient(#000 0 0) content-box,
      linear-gradient(#000 0 0);
    -webkit-mask-composite: xor;
  }

  .constellation-astre-letter {
    display: block;
    min-width: 0.72em;
    position: relative;
    z-index: 1;
    text-align: center;
    line-height: 0.84;
    transform: translate(var(--astre-letter-offset-x), calc(var(--astre-letter-offset-y) + 5px));
  }

  .constellation-astre-owner {
    position: relative;
    z-index: 1;
    display: block;
    max-width: 78%;
    overflow: hidden;
    color: color-mix(in srgb, var(--astre-core-color) 78%, rgba(255, 244, 220, 0.72));
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 720;
    letter-spacing: 0;
    line-height: 1;
    text-align: center;
    text-overflow: ellipsis;
    text-shadow: 0 0 10px color-mix(in srgb, var(--astre-tone-color) 28%, transparent);
    white-space: nowrap;
  }

  .constellation-astre-spectral {
    --astre-tone-color: var(--constellation-color-spectral);
  }

  .constellation-astre-amber {
    --astre-tone-color: var(--constellation-color-amber);
    --astre-before-border: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 24%, transparent);
    --astre-halo-border: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 18%, transparent);
    --astre-halo-inner-border: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 12%, transparent);
    --astre-ring-shadow: var(--constellation-shadow-astre-amber);
    --astre-core-color: var(--constellation-color-amber-owner);
    --astre-core-border-color: rgba(255, 227, 172, 0.28);
    --astre-core-inner-stroke: rgba(255, 227, 172, 0.38);
    --astre-core-glow-strong: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 34%, transparent);
    --astre-core-glow-soft: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 16%, transparent);
  }

  .constellation-astre-hero .constellation-astre-letter {
    font-size: var(--constellation-type-astre-hero);
  }

  .constellation-astre-standard .constellation-astre-letter {
    font-size: var(--constellation-type-astre-standard);
  }

  .constellation-astre-compact .constellation-astre-letter {
    font-size: 32px;
  }

  .constellation-astre-compact {
    --astre-frame-radius: 50%;
    --astre-before-inset: -8px;
    --astre-before-transform: none;
    --astre-halo-inset: -5px;
    --astre-halo-before-inset: -3px;
    --astre-halo-before-transform: none;
    --astre-letter-offset-x: 0px;
    --astre-letter-offset-y: 0px;
  }

  .constellation-astre-working {
    --astre-halo-pulse-duration: 3.5s;
    --astre-core-drift-duration: 4.4s;
    --astre-ring-drift-duration: 9.6s;
    --astre-ring-drift-slow-duration: 13.8s;
    --astre-ring-drift-quick-duration: 6.8s;
    --astre-archive-drift-a-duration: 22s;
    --astre-archive-drift-b-duration: 27s;
    --astre-archive-drift-c-duration: 32s;
    --astre-outer-ring-opacity: 0.42;
    --astre-halo-rest-opacity: 0.74;
    --astre-halo-inner-opacity: 0;
    --astre-halo-pulse-min: 0.58;
    --astre-halo-pulse-max: 0.86;
    --astre-ring-filter: saturate(1.22) brightness(1.12);
    --astre-archive-opacity: 0.94;
    --astre-archive-filter: saturate(1.22) brightness(1.14);
    --astre-core-filter: saturate(1.22) brightness(1.03);
  }

  .constellation-astre-disconnected {
    --astre-outer-ring-opacity: 0.2;
    --astre-halo-rest-opacity: 0.18;
    --astre-halo-inner-opacity: 0.2;
    --astre-halo-pulse-min: 0.18;
    --astre-halo-pulse-max: 0.28;
    --astre-ring-opacity: 0.48;
    --astre-ring-filter: saturate(0.72) brightness(0.82);
    --astre-archive-opacity: 0.42;
    --astre-archive-filter: saturate(0.72) brightness(0.82);
    --astre-core-opacity: 0.78;
    --astre-core-filter: saturate(0.72) brightness(0.82);
  }

  .constellation-astre-disconnected .constellation-astre-presence-dot {
    opacity: 0.56;
  }

  .constellation-astre-emphasis {
    --astre-scale: 1.1;
    --astre-outer-ring-opacity: 0.46;
    --astre-halo-rest-opacity: 0.82;
    --astre-halo-inner-opacity: 0.34;
    --astre-halo-pulse-min: 0.64;
    --astre-halo-pulse-max: 0.92;
    --astre-ring-opacity: 1;
    --astre-ring-filter: var(--astre-emphasis-ring-filter);
    --astre-archive-opacity: 0.98;
    --astre-archive-filter: var(--astre-emphasis-archive-filter);
    --astre-core-opacity: 1;
    --astre-core-filter: var(--astre-emphasis-core-filter);
    --astre-rim-opacity: 1;
  }

  .constellation-astre-emphasis .constellation-astre-halo {
    filter: blur(24px) saturate(1.16);
  }

  .constellation-astre-emphasis .constellation-astre-ring {
    border-color: var(--astre-emphasis-ring-border);
    box-shadow: var(--astre-emphasis-ring-shadow);
  }

  .constellation-astre-emphasis .constellation-astre-core {
    transform: var(--astre-emphasis-core-transform);
    box-shadow: var(--astre-emphasis-core-shadow);
  }

  .constellation-astre-drop-target {
    --astre-outer-ring-opacity: 0.48;
    --astre-halo-rest-opacity: 0.86;
    --astre-halo-pulse-min: 0.68;
    --astre-halo-pulse-max: 0.94;
    --astre-ring-opacity: 1;
    --astre-ring-filter: saturate(1.34) brightness(1.2);
    --astre-core-filter: saturate(1.34) brightness(1.07);
  }

  .constellation-astre-drop-target::before {
    border-style: solid;
  }

  .constellation-astre-drop-target .constellation-astre-ring {
    box-shadow:
      0 0 24px color-mix(in srgb, var(--astre-tone-color) 46%, transparent),
      0 0 72px color-mix(in srgb, var(--astre-tone-color) 24%, transparent),
      0 0 110px color-mix(in srgb, var(--astre-tone-color) 12%, transparent);
  }

  .constellation-astre-drop-target .constellation-astre-core {
    transform: scale(1.055);
  }

  .is-animated .constellation-astre-halo {
    animation: constellation-astre-halo-pulse var(--astre-halo-pulse-duration) var(--constellation-motion-ease-float) infinite;
  }

  .is-animated .constellation-astre-archive-dot-a {
    animation: constellation-astre-dot-voyage var(--astre-archive-drift-a-duration) var(--constellation-motion-ease-float) infinite;
  }

  .is-animated .constellation-astre-archive-dot-b {
    animation: constellation-astre-dot-voyage var(--astre-archive-drift-b-duration) var(--constellation-motion-ease-float) infinite;
  }

  .is-animated .constellation-astre-archive-dot-c {
    animation: constellation-astre-dot-voyage var(--astre-archive-drift-c-duration) var(--constellation-motion-ease-float) infinite;
  }

  .is-animated.constellation-astre-presence-online .constellation-astre-presence-dot {
    animation: constellation-astre-presence-breathe 3.6s var(--constellation-motion-ease-float) infinite;
  }

  .constellation-astre-disconnected.is-animated::before,
  .constellation-astre-disconnected.is-animated .constellation-astre-halo,
  .constellation-astre-disconnected.is-animated .constellation-astre-halo::before,
  .constellation-astre-disconnected.is-animated .constellation-astre-ring,
  .constellation-astre-disconnected.is-animated .constellation-astre-archive-dot,
  .constellation-astre-disconnected.is-animated .constellation-astre-core {
    animation: none;
  }

  .constellation-astre:hover {
    --astre-scale: 1.1 !important;
    --astre-outer-ring-opacity: 0.46 !important;
    --astre-halo-rest-opacity: 0.82 !important;
    --astre-halo-inner-opacity: 0.34 !important;
    --astre-halo-pulse-min: 0.64 !important;
    --astre-halo-pulse-max: 0.92 !important;
    --astre-ring-opacity: 1 !important;
    --astre-ring-filter: var(--astre-emphasis-ring-filter) !important;
    --astre-archive-opacity: 1 !important;
    --astre-archive-filter: var(--astre-emphasis-archive-filter) !important;
    --astre-core-filter: var(--astre-emphasis-core-filter) !important;
    --astre-rim-opacity: 1 !important;
  }

  .constellation-astre:hover .constellation-astre-ring {
    border-color: var(--astre-emphasis-ring-border);
    box-shadow: var(--astre-emphasis-ring-shadow);
  }

  .constellation-astre:hover .constellation-astre-core {
    transform: var(--astre-emphasis-core-transform);
    box-shadow: var(--astre-emphasis-core-shadow);
  }

  @keyframes constellation-astre-halo-pulse {
    0%,
    100% {
      opacity: var(--astre-halo-pulse-min);
    }

    50% {
      opacity: var(--astre-halo-pulse-max);
    }
  }

  @keyframes constellation-astre-ring-outer-drift {
    0%,
    100% {
      transform: translate3d(0, 0, 0) rotate(-8deg) scale(1);
    }

    14% {
      transform: translate3d(2px, -2px, 0) rotate(-12deg) scale(0.94, 0.96);
    }

    24% {
      transform: translate3d(-1px, 1px, 0) rotate(-6deg) scale(1.03, 1.04);
    }

    43% {
      transform: translate3d(-3px, 1px, 0) rotate(-4deg) scale(0.98, 1.01);
    }

    61% {
      transform: translate3d(2px, 2px, 0) rotate(-10deg) scale(0.92, 0.95);
    }

    71% {
      transform: translate3d(1px, -1px, 0) rotate(-7deg) scale(1.02, 1.01);
    }

    86% {
      transform: translate3d(-1px, -2px, 0) rotate(-5deg) scale(0.99, 0.97);
    }
  }

  @keyframes constellation-astre-ring-middle-drift {
    0%,
    100% {
      transform: translate3d(0, 0, 0) rotate(0deg) scale(1);
    }

    18% {
      transform: translate3d(-2px, 1px, 0) rotate(-4deg) scale(0.91, 0.95);
    }

    30% {
      transform: translate3d(1px, -1px, 0) rotate(1deg) scale(1.04, 1.02);
    }

    52% {
      transform: translate3d(2px, 2px, 0) rotate(-1deg) scale(0.96, 0.98);
    }

    69% {
      transform: translate3d(-1px, -2px, 0) rotate(3deg) scale(0.89, 0.93);
    }

    81% {
      transform: translate3d(1px, 1px, 0) rotate(0deg) scale(1.03, 1.01);
    }
  }

  @keyframes constellation-astre-ring-inner-drift {
    0%,
    100% {
      transform: translate3d(0, 0, 0) rotate(11deg) scale(1);
    }

    15% {
      transform: translate3d(2px, -1px, 0) rotate(8deg) scale(0.88, 0.92);
    }

    27% {
      transform: translate3d(-1px, 1px, 0) rotate(13deg) scale(1.05, 1.02);
    }

    49% {
      transform: translate3d(-2px, 2px, 0) rotate(15deg) scale(0.94, 0.97);
    }

    67% {
      transform: translate3d(1px, 1px, 0) rotate(9deg) scale(0.9, 0.94);
    }

    78% {
      transform: translate3d(0, -1px, 0) rotate(12deg) scale(1.03, 1.01);
    }

    89% {
      transform: translate3d(-1px, -2px, 0) rotate(14deg) scale(0.97, 1);
    }
  }

  @keyframes constellation-astre-ring-near-drift {
    0%,
    100% {
      transform: translate3d(0, 0, 0) rotate(0deg) scale(1);
    }

    12% {
      transform: translate3d(1px, 0, 0) rotate(-1deg) scale(0.97, 0.99);
    }

    22% {
      transform: translate3d(0, -1px, 0) rotate(1deg) scale(1.02, 1.01);
    }

    38% {
      transform: translate3d(-1px, 2px, 0) rotate(1.5deg) scale(0.95, 0.98);
    }

    51% {
      transform: translate3d(2px, -1px, 0) rotate(-2deg) scale(1.01, 1.03);
    }

    72% {
      transform: translate3d(-2px, 1px, 0) rotate(0.5deg) scale(0.96, 0.97);
    }

    84% {
      transform: translate3d(1px, 1px, 0) rotate(-0.5deg) scale(1.02, 1);
    }
  }

  @keyframes constellation-astre-core-drift {
    0%,
    100% {
      transform: translateY(0);
    }

    50% {
      transform: translateY(-2px);
    }
  }

  @keyframes constellation-astre-presence-breathe {
    0%,
    100% {
      transform: translate(26%, -22%) scale(1);
      opacity: 0.76;
    }

    50% {
      transform: translate(26%, -22%) scale(1.16);
      opacity: 1;
    }
  }

  @keyframes constellation-astre-dot-voyage {
    0%,
    100% {
      top: var(--archive-top-0);
      left: var(--archive-left-0);
      transform: translate(-50%, -50%) scale(1);
    }

    28% {
      top: var(--archive-top-1);
      left: var(--archive-left-1);
      transform: translate(-50%, -50%) scale(0.94);
    }

    57% {
      top: var(--archive-top-2);
      left: var(--archive-left-2);
      transform: translate(-50%, -50%) scale(1.06);
    }

    82% {
      top: var(--archive-top-3);
      left: var(--archive-left-3);
      transform: translate(-50%, -50%) scale(0.97);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .is-animated::before,
    .is-animated .constellation-astre-halo,
    .is-animated .constellation-astre-halo::before,
    .is-animated .constellation-astre-ring,
    .is-animated .constellation-astre-archive-dot,
    .is-animated .constellation-astre-core,
    .is-animated .constellation-astre-presence-dot {
      animation: none !important;
    }
  }
</style>
