<script lang="ts">
  import { fade } from 'svelte/transition';
  import { Astre, SignalBlob } from '$lib/components/constellation';
  import WorkspaceAppObject from '$lib/features/workspace-scene/renderers/WorkspaceAppObject.svelte';
  import WorkspacePin from '$lib/features/workspace-scene/renderers/WorkspacePin.svelte';
  import {
    orbitLaneFade,
    primitiveOrbitLaneDotStyle,
    primitiveOrbitLaneRingStyle,
    primitiveOrbitLaneSpokeStyle,
    primitiveOrbitLaneStyle,
    type CortexThemeMode,
    type PrimitiveAppVisual,
    type PrimitiveAstreVisual,
    type PrimitiveBlobVisual,
    type PrimitiveOrbitLaneVisual,
    type PrimitivePinVisual,
    type SemanticZoomLevel,
  } from '$lib/utils/cortexOrbitPrimitives';

  let {
    overlayEl = $bindable<HTMLDivElement | undefined>(),
    overlayStyle,
    orbitLaneVisuals,
    astreVisuals,
    pinVisuals,
    appVisuals,
    blobVisuals,
    semanticZoomLevel,
    themeMode,
    movingPinId,
    blobText,
    astreClass,
    astreStyle,
    pinActive,
    pinStyle,
    appStyle,
    blobStyle,
    setHoveredAstre,
    clearHoveredAstre,
    setHoveredPin,
    clearHoveredPin,
    activateAstre,
    activatePin,
    beginPinDrag,
    movePinDrag,
    endPinDrag,
    activateApp,
    beginAppDrag,
    moveAppDrag,
    endAppDrag,
    activateBlob,
    editBlob,
    popBlob,
    hoverBlob,
    unhoverBlob,
    beginBlobDrag,
    moveBlobDrag,
    endBlobDrag,
  }: {
    overlayEl?: HTMLDivElement;
    overlayStyle: string;
    orbitLaneVisuals: PrimitiveOrbitLaneVisual[];
    astreVisuals: PrimitiveAstreVisual[];
    pinVisuals: PrimitivePinVisual[];
    appVisuals: PrimitiveAppVisual[];
    blobVisuals: PrimitiveBlobVisual[];
    semanticZoomLevel: SemanticZoomLevel;
    themeMode: CortexThemeMode;
    movingPinId: string | null;
    blobText: (blob: PrimitiveBlobVisual) => string;
    astreClass: (astre: PrimitiveAstreVisual) => string;
    astreStyle: (astre: PrimitiveAstreVisual) => string;
    pinActive: (pin: PrimitivePinVisual) => boolean;
    pinStyle: (pin: PrimitivePinVisual) => string;
    appStyle: (app: PrimitiveAppVisual) => string;
    blobStyle: (blob: PrimitiveBlobVisual) => string;
    setHoveredAstre: (id: string | null) => void;
    clearHoveredAstre: (id: string) => void;
    setHoveredPin: (id: string) => void;
    clearHoveredPin: (id: string) => void;
    activateAstre: (astre: PrimitiveAstreVisual, event: MouseEvent) => void;
    activatePin: (pin: PrimitivePinVisual, event: MouseEvent) => void;
    beginPinDrag: (pin: PrimitivePinVisual, event: PointerEvent) => void;
    movePinDrag: (pin: PrimitivePinVisual, event: PointerEvent) => void;
    endPinDrag: (pin: PrimitivePinVisual, event: PointerEvent) => void;
    activateApp: (app: PrimitiveAppVisual, event: MouseEvent) => void;
    beginAppDrag: (app: PrimitiveAppVisual, event: PointerEvent) => void;
    moveAppDrag: (app: PrimitiveAppVisual, event: PointerEvent) => void;
    endAppDrag: (app: PrimitiveAppVisual, event: PointerEvent) => void;
    activateBlob: (blob: PrimitiveBlobVisual) => void;
    editBlob: (blob: PrimitiveBlobVisual) => void;
    popBlob: (id: string) => void;
    hoverBlob: (blob: PrimitiveBlobVisual) => void;
    unhoverBlob: () => void;
    beginBlobDrag: (blob: PrimitiveBlobVisual, event: PointerEvent) => void;
    moveBlobDrag: (blob: PrimitiveBlobVisual, event: PointerEvent) => void;
    endBlobDrag: (blob: PrimitiveBlobVisual, event: PointerEvent) => void;
  } = $props();
</script>

<div
  class="cortex-orbit-primitives"
  data-cortex-workspace-render-owner="workspace-scene-primitives"
  bind:this={overlayEl}
  style={overlayStyle}
>
  {#each orbitLaneVisuals as lane (lane.id)}
    <div class="cortex-orbit-lane-system" style={primitiveOrbitLaneStyle(lane)} transition:fade={orbitLaneFade(320)}>
      {#each lane.spokes as spoke (spoke.id)}
        <span class="cortex-orbit-lane-spoke" style={primitiveOrbitLaneSpokeStyle(lane, spoke, themeMode)} transition:fade={orbitLaneFade(260)} aria-hidden="true"></span>
      {/each}

      {#each lane.rings as ring (ring.id)}
        <span class="cortex-orbit-lane-ring" style={primitiveOrbitLaneRingStyle(lane, ring, themeMode)} transition:fade={orbitLaneFade(360)} aria-hidden="true"></span>
      {/each}

      {#each lane.dots as dot (dot.id)}
        <span
          class={`cortex-orbit-lane-dot cortex-orbit-lane-dot-${dot.state} cortex-orbit-lane-dot-cue-${dot.cue}`}
          style={primitiveOrbitLaneDotStyle(lane, dot, themeMode)}
          transition:fade={orbitLaneFade(280)}
          aria-hidden="true"
        ></span>
      {/each}
    </div>
  {/each}

  {#each astreVisuals as astre (astre.id)}
    <Astre
      letter={astre.letter}
      owner={astre.owner}
      tone={astre.tone}
      scale={astre.scale}
      semanticLevel={semanticZoomLevel}
      activity={astre.activity}
      presence={astre.presence}
      animated={true}
      interactive={true}
      archivedCount={astre.archivedCount}
      className={astreClass(astre)}
      style={astreStyle(astre)}
      onpointerenter={() => {
        setHoveredAstre(astre.id);
      }}
      onpointerleave={() => {
        clearHoveredAstre(astre.id);
      }}
      onclick={(event) => {
        activateAstre(astre, event);
      }}
    />
  {/each}

  {#each pinVisuals as pin (pin.id)}
    <WorkspacePin
      pinId={pin.pinId}
      label={pin.label}
      accent={pin.accent}
      active={pinActive(pin)}
      moving={movingPinId === pin.pinId}
      canEdit={pin.canEdit}
      canMove={pin.canMove}
      semanticLevel={semanticZoomLevel}
      activate={(event) => activatePin(pin, event)}
      hover={() => setHoveredPin(pin.id)}
      unhover={() => clearHoveredPin(pin.id)}
      beginDrag={(event) => beginPinDrag(pin, event)}
      moveDrag={(event) => movePinDrag(pin, event)}
      endDrag={(event) => endPinDrag(pin, event)}
      style={pinStyle(pin)}
    />
  {/each}

  {#each appVisuals as app (app.id)}
    <WorkspaceAppObject
      appId={app.id}
      name={app.name}
      description={app.description}
      rendererKey={app.rendererKey}
      visualSpec={app.visualSpec}
      stateKey={app.stateKey}
      accent={app.accent}
      active={app.active}
      semanticLevel={semanticZoomLevel}
      style={appStyle(app)}
      activate={(event) => activateApp(app, event)}
      beginDrag={(event) => beginAppDrag(app, event)}
      moveDrag={(event) => moveAppDrag(app, event)}
      endDrag={(event) => endAppDrag(app, event)}
    />
  {/each}
</div>

{#each blobVisuals as blob (blob.id)}
  <SignalBlob
    text={blobText(blob)}
    label={blob.text}
    tone={blob.tone}
    shape={blob.shape}
    scale={blob.scale}
    semanticLevel={semanticZoomLevel}
    state={blob.state}
    cue={blob.cue}
    presence={blob.presence}
    treatment={blob.treatment}
    icon={blob.icon}
    attachmentCount={blob.attachmentCount}
    animated={true}
    interactive={true}
    dataId={blob.id}
    activate={() => activateBlob(blob)}
    edit={() => editBlob(blob)}
    longPress={() => popBlob(blob.id)}
    longPressThresholdMs={blob.state === 'done' ? 500 : 1000}
    hover={() => hoverBlob(blob)}
    unhover={unhoverBlob}
    beginDrag={(event) => beginBlobDrag(blob, event)}
    moveDrag={(event) => moveBlobDrag(blob, event)}
    endDrag={(event) => endBlobDrag(blob, event)}
    style={blobStyle(blob)}
  />
{/each}

<style>
  .cortex-orbit-primitives {
    position: absolute;
    inset: 0;
    z-index: 2;
    pointer-events: none;
    transform-origin: 0 0;
    will-change: transform;
  }

  .cortex-orbit-lane-system {
    position: absolute;
    pointer-events: none;
    transform: translate(0, 0);
    transform-origin: center;
    z-index: 1;
    color: var(--orbit-lane-accent);
  }

  .cortex-orbit-lane-ring,
  .cortex-orbit-lane-dot,
  .cortex-orbit-lane-spoke {
    position: absolute;
    pointer-events: none;
  }

  .cortex-orbit-lane-ring {
    left: 0;
    top: 0;
    border-radius: 999px;
    border: var(--orbit-ring-weight, 1px) dashed color-mix(in srgb, var(--orbit-lane-accent) var(--orbit-ring-alpha, 54%), rgba(235, 241, 252, 0.16));
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--orbit-lane-accent) 10%, transparent),
      0 0 16px color-mix(in srgb, var(--orbit-lane-accent) 12%, transparent);
    transform: translate(0, 0);
    transform-origin: center;
    scale: 1;
    transition:
      left 680ms cubic-bezier(0.22, 1, 0.36, 1),
      top 680ms cubic-bezier(0.22, 1, 0.36, 1),
      width 680ms cubic-bezier(0.22, 1, 0.36, 1),
      height 680ms cubic-bezier(0.22, 1, 0.36, 1),
      opacity 460ms ease,
      scale 680ms cubic-bezier(0.22, 1, 0.36, 1),
      border-color 260ms ease,
      box-shadow 260ms ease;
    will-change: left, top, width, height, opacity, scale;
    @starting-style {
      opacity: 0;
      scale: 0.72;
    }
  }

  .cortex-orbit-lane-dot {
    border-radius: 999px;
    background:
      radial-gradient(circle, rgba(255, 255, 255, 0.98) 0 18%, color-mix(in srgb, var(--orbit-lane-accent) 46%, rgba(255, 255, 255, 0.9)) 28%, color-mix(in srgb, var(--orbit-lane-accent) 36%, transparent) 58%, transparent 74%);
    box-shadow:
      0 0 5px color-mix(in srgb, var(--orbit-lane-accent) 78%, rgba(255, 255, 255, 0.4)),
      0 0 14px color-mix(in srgb, var(--orbit-lane-accent) 48%, transparent),
      0 0 26px color-mix(in srgb, var(--orbit-lane-accent) 18%, transparent);
    filter: saturate(1.18);
    mix-blend-mode: screen;
    transform: translate(-50%, -50%);
    scale: 1;
    transition:
      left 620ms cubic-bezier(0.22, 1, 0.36, 1),
      top 620ms cubic-bezier(0.22, 1, 0.36, 1),
      width 520ms cubic-bezier(0.22, 1, 0.36, 1),
      height 520ms cubic-bezier(0.22, 1, 0.36, 1),
      opacity 420ms ease,
      scale 520ms cubic-bezier(0.22, 1, 0.36, 1);
    will-change: left, top, width, height, opacity, scale;
    @starting-style {
      opacity: 0;
      scale: 0.48;
    }
  }

  .cortex-orbit-lane-dot::before,
  .cortex-orbit-lane-dot::after {
    content: '';
    position: absolute;
    left: 50%;
    top: 50%;
    pointer-events: none;
    border-radius: 999px;
    background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.78), transparent);
    filter: blur(0.2px);
    opacity: 0.54;
    transform: translate(-50%, -50%);
  }

  .cortex-orbit-lane-dot::before {
    width: var(--orbit-dot-flare, 10px);
    height: 1px;
  }

  .cortex-orbit-lane-dot::after {
    width: 1px;
    height: var(--orbit-dot-flare, 10px);
    background: linear-gradient(180deg, transparent, rgba(255, 255, 255, 0.66), transparent);
  }

  .cortex-orbit-lane-dot-working {
    box-shadow:
      0 0 16px color-mix(in srgb, var(--orbit-lane-accent) 68%, transparent),
      0 0 28px color-mix(in srgb, var(--orbit-lane-accent) 32%, transparent);
    animation: cortex-orbit-signal-pulse 2.4s ease-in-out infinite;
  }

  .cortex-orbit-lane-dot-cue-attention {
    background: color-mix(in srgb, var(--orbit-lane-accent) 68%, white 32%);
    box-shadow:
      0 0 15px color-mix(in srgb, var(--orbit-lane-accent) 62%, transparent);
  }

  .cortex-orbit-lane-dot-cue-risk {
    background: #db6e82;
    box-shadow: 0 0 16px rgba(219, 110, 130, 0.56);
  }

  .cortex-orbit-lane-spoke {
    height: 1px;
    background-image: linear-gradient(
      90deg,
      color-mix(in srgb, var(--orbit-lane-accent) 58%, rgba(235, 241, 252, 0.22)) 0 28%,
      transparent 28% 100%
    );
    background-size: 8px 1px;
    filter: drop-shadow(0 0 7px color-mix(in srgb, var(--orbit-lane-accent) 28%, transparent));
    transform: translate(-50%, -50%) rotate(var(--orbit-spoke-rotation, 0deg));
    transform-origin: center center;
    transition:
      left 660ms cubic-bezier(0.22, 1, 0.36, 1),
      top 660ms cubic-bezier(0.22, 1, 0.36, 1),
      width 680ms cubic-bezier(0.22, 1, 0.36, 1),
      transform 660ms cubic-bezier(0.22, 1, 0.36, 1),
      opacity 360ms ease;
    will-change: left, top, width, transform, opacity;
    @starting-style {
      opacity: 0;
      width: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .cortex-orbit-lane-system,
    .cortex-orbit-lane-ring,
    .cortex-orbit-lane-dot,
    .cortex-orbit-lane-spoke {
      transition: none;
    }
  }

  @keyframes cortex-orbit-signal-pulse {
    0%,
    100% {
      transform: translate(-50%, -50%) scale(0.92);
      opacity: 0.72;
    }

    50% {
      transform: translate(-50%, -50%) scale(1.16);
      opacity: 1;
    }
  }
</style>
