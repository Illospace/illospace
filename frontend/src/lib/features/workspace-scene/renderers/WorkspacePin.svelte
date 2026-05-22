<script lang="ts">
  type SemanticZoomLevel = 'detail' | 'summary' | 'symbol' | 'glyph';

  let {
    pinId,
    label,
    accent = '#57CFA0',
    active = false,
    moving = false,
    canEdit = false,
    canMove = false,
    semanticLevel = 'detail',
    style = '',
    activate,
    hover,
    unhover,
    beginDrag,
    moveDrag,
    endDrag,
  }: {
    pinId?: string;
    label: string;
    accent?: string;
    active?: boolean;
    moving?: boolean;
    canEdit?: boolean;
    canMove?: boolean;
    semanticLevel?: SemanticZoomLevel;
    style?: string;
    activate?: (event: MouseEvent) => void;
    hover?: () => void;
    unhover?: () => void;
    beginDrag?: (event: PointerEvent) => boolean | void;
    moveDrag?: (event: PointerEvent) => boolean | void;
    endDrag?: (event: PointerEvent) => boolean | void;
  } = $props();

  let activePointerId: number | null = null;
  let pointerMoved = false;
  let suppressNextActivate = false;

  function handleActivate(event: MouseEvent) {
    if (suppressNextActivate) {
      event.preventDefault();
      event.stopPropagation();
      suppressNextActivate = false;
      return;
    }
    activate?.(event);
  }

  function releasePointerCapture(event: PointerEvent) {
    const target = event.currentTarget;
    if (target instanceof HTMLElement && target.hasPointerCapture(event.pointerId)) {
      target.releasePointerCapture(event.pointerId);
    }
  }

  function handlePointerDown(event: PointerEvent) {
    if (event.button !== 0) return;
    const didStart = beginDrag?.(event);
    if (didStart === false) return;

    activePointerId = event.pointerId;
    pointerMoved = false;

    const target = event.currentTarget;
    if (target instanceof HTMLElement) {
      target.setPointerCapture(event.pointerId);
    }
  }

  function handlePointerMove(event: PointerEvent) {
    if (activePointerId !== event.pointerId) return;
    const didMove = moveDrag?.(event);
    if (didMove) {
      pointerMoved = true;
      suppressNextActivate = true;
    }
  }

  function handlePointerUp(event: PointerEvent) {
    if (activePointerId !== event.pointerId) return;
    const didMove = endDrag?.(event) || pointerMoved;
    if (didMove) {
      suppressNextActivate = true;
    }
    releasePointerCapture(event);
    activePointerId = null;
    pointerMoved = false;
  }

  function handlePointerCancel(event: PointerEvent) {
    if (activePointerId !== event.pointerId) return;
    endDrag?.(event);
    releasePointerCapture(event);
    activePointerId = null;
    pointerMoved = false;
    suppressNextActivate = true;
  }
</script>

<button
  type="button"
  class="cortex-workspace-pin"
  class:cortex-workspace-pin--semantic-summary={semanticLevel === 'summary'}
  class:cortex-workspace-pin--semantic-symbol={semanticLevel === 'symbol'}
  class:cortex-workspace-pin--semantic-glyph={semanticLevel === 'glyph'}
  class:is-active={active}
  class:is-moving={moving}
  class:can-edit={canEdit}
  class:can-move={canMove}
  style={`--workspace-pin-accent:${accent};${style}`}
  aria-label={label}
  title={canMove ? label : undefined}
  data-cortex-workspace-pin-id={pinId}
  onclick={handleActivate}
  onpointerdown={handlePointerDown}
  onpointerenter={() => hover?.()}
  onpointermove={handlePointerMove}
  onpointerup={handlePointerUp}
  onpointercancel={handlePointerCancel}
  onpointerleave={() => unhover?.()}
  onfocus={() => hover?.()}
  onblur={() => unhover?.()}
>
  <span class="cortex-workspace-pin__halo" aria-hidden="true"></span>
  <span class="cortex-workspace-pin__body">
    <span class="cortex-workspace-pin__shine" aria-hidden="true"></span>
    <span class="cortex-workspace-pin__label">{label}</span>
  </span>
</button>

<style>
  .cortex-workspace-pin {
    --workspace-pin-accent: #57CFA0;
    --workspace-pin-body-background: color-mix(
      in srgb,
      var(--workspace-pin-accent) 12%,
      var(--constellation-thread-message-user-shell-base)
    );
    --workspace-pin-body-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--workspace-pin-accent) 42%, var(--constellation-surface-panel-border)),
      0 0 18px color-mix(in srgb, var(--workspace-pin-accent) 16%, transparent),
      var(--constellation-surface-panel-shadow);
    --workspace-pin-label-color: var(--constellation-color-text-primary);
    --workspace-pin-label-shadow: 0 0 10px color-mix(in srgb, var(--workspace-pin-accent) 24%, transparent);
    position: absolute;
    left: 0;
    top: 0;
    display: grid;
    place-items: center;
    width: 92px;
    height: 92px;
    border: 0;
    border-radius: 999px;
    padding: 0;
    background: transparent;
    pointer-events: auto;
    transform: translate(-50%, -50%);
    transform-origin: center;
    color: color-mix(in srgb, var(--workspace-pin-accent) 66%, var(--constellation-color-text-primary));
    cursor: default;
    filter:
      drop-shadow(0 0 15px color-mix(in srgb, var(--workspace-pin-accent) 20%, transparent))
      drop-shadow(0 8px 22px rgba(0, 0, 0, 0.22));
    transition:
      filter 180ms ease,
      transform 190ms var(--constellation-motion-ease-lift),
      opacity 160ms ease;
  }

  :global(:root[data-color-scheme='light']) .cortex-workspace-pin {
    --workspace-pin-body-background: color-mix(
      in srgb,
      var(--workspace-pin-accent) 9%,
      var(--constellation-thread-message-user-shell-base)
    );
    --workspace-pin-body-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--workspace-pin-accent) 38%, var(--constellation-surface-panel-border)),
      0 0 16px color-mix(in srgb, var(--workspace-pin-accent) 13%, transparent),
      var(--constellation-surface-panel-shadow);
    --workspace-pin-label-color: color-mix(in srgb, var(--workspace-pin-accent) 28%, var(--constellation-color-text-primary));
    --workspace-pin-label-shadow: none;
  }

  .cortex-workspace-pin.can-move {
    cursor: grab;
  }

  .cortex-workspace-pin.is-moving {
    cursor: grab;
  }

  .cortex-workspace-pin.can-move:active,
  .cortex-workspace-pin.is-moving:active {
    cursor: grabbing;
  }

  .cortex-workspace-pin:hover,
  .cortex-workspace-pin:focus-visible,
  .cortex-workspace-pin.is-active {
    transform: translate(-50%, -50%) scale(1.1);
    filter:
      drop-shadow(0 0 24px color-mix(in srgb, var(--workspace-pin-accent) 38%, transparent))
      drop-shadow(0 14px 30px rgba(0, 0, 0, 0.28));
    outline: none;
  }

  .cortex-workspace-pin__halo,
  .cortex-workspace-pin__body,
  .cortex-workspace-pin__shine {
    position: absolute;
    display: block;
  }

  .cortex-workspace-pin__halo {
    inset: -10px;
    border-radius: inherit;
    background: radial-gradient(circle, color-mix(in srgb, var(--workspace-pin-accent) 18%, transparent), transparent 66%);
    opacity: 0.7;
    transition: opacity 180ms ease;
  }

  .cortex-workspace-pin__body {
    inset: 9px;
    display: grid;
    place-items: center;
    border-radius: 999px;
    background: var(--workspace-pin-body-background);
    box-shadow: var(--workspace-pin-body-shadow);
    overflow: hidden;
  }

  .cortex-workspace-pin__body::after {
    display: none;
  }

  .cortex-workspace-pin__shine {
    display: none;
  }

  .cortex-workspace-pin__label {
    position: relative;
    z-index: 1;
    display: -webkit-box;
    max-width: 58px;
    overflow: hidden;
    color: var(--workspace-pin-label-color);
    font-size: 11px;
    font-weight: 760;
    letter-spacing: 0;
    line-height: 1.08;
    overflow-wrap: anywhere;
    text-align: center;
    text-shadow: var(--workspace-pin-label-shadow);
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    line-clamp: 3;
  }

  .cortex-workspace-pin--semantic-summary .cortex-workspace-pin__label {
    max-width: 54px;
    font-size: 10px;
    line-height: 1.04;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .cortex-workspace-pin--semantic-symbol .cortex-workspace-pin__label,
  .cortex-workspace-pin--semantic-glyph .cortex-workspace-pin__label {
    display: none;
    opacity: 0;
    transform: scale(0.72);
  }

</style>
