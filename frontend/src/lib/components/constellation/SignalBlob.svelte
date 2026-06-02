<script lang="ts">
  import ConstellationIcon, { type ConstellationIconName } from './ConstellationIcon.svelte';
  import ConstellationSignalStatusIndicator from './ConstellationSignalStatusIndicator.svelte';
  import { signalBlobPointerUpAction } from './signalBlobActivation';
  import type {
    ConstellationScale,
    ConstellationShape,
    ConstellationSignalCue,
    ConstellationSignalIcon,
    ConstellationSignalPresence,
    ConstellationSignalState,
    ConstellationSignalTreatment,
    ConstellationTone,
  } from './constellationTypes';

  type SemanticZoomLevel = 'detail' | 'summary' | 'symbol' | 'glyph';

  const SIGNAL_ICON_MAP: Record<ConstellationSignalIcon, ConstellationIconName> = {
    thread: 'reply-thread',
    api: 'route',
    code: 'code',
    database: 'database',
    design: 'brush',
    document: 'document',
    image: 'image',
    test: 'test',
    tool: 'tool',
  };

  let {
    text,
    label = '',
    tone = 'spectral',
    shape = 'alpha',
    scale = 'standard',
    semanticLevel = 'detail',
    state = 'idle',
    cue = 'none',
    presence = 'none',
    treatment = 'bloom',
    icon = 'thread',
    attachmentCount = 0,
    badge = false,
    animated = true,
    interactive = false,
    activate,
    edit,
    longPress,
    longPressThresholdMs = 900,
    hover,
    unhover,
    beginDrag,
    moveDrag,
    endDrag,
    dataId,
    className = '',
    style = '',
  }: {
    text: string;
    label?: string;
    tone?: ConstellationTone;
    shape?: ConstellationShape;
    scale?: ConstellationScale;
    semanticLevel?: SemanticZoomLevel;
    state?: ConstellationSignalState;
    cue?: ConstellationSignalCue;
    presence?: ConstellationSignalPresence;
    treatment?: ConstellationSignalTreatment;
    icon?: ConstellationSignalIcon;
    attachmentCount?: number;
    badge?: boolean;
    animated?: boolean;
    interactive?: boolean;
    activate?: (event: MouseEvent | PointerEvent) => void;
    edit?: () => void;
    longPress?: () => void;
    longPressThresholdMs?: number;
    hover?: () => void;
    unhover?: () => void;
    beginDrag?: (event: PointerEvent) => boolean | void;
    moveDrag?: (event: PointerEvent) => boolean | void;
    endDrag?: (event: PointerEvent) => boolean | void;
    dataId?: string;
    className?: string;
    style?: string;
  } = $props();

  const showHalo = $derived(presence === 'inside');
  const iconName = $derived(SIGNAL_ICON_MAP[icon] ?? SIGNAL_ICON_MAP.thread);
  const fullLabel = $derived(label || text);
  const showWorkingIndicator = $derived(state === 'working');
  const showUnreadNotification = $derived(cue === 'attention' && !showWorkingIndicator);
  const statusVariant = $derived.by<'owner' | 'inside' | 'attention' | 'risk' | null>(() => {
    if (cue === 'risk') return 'risk';
    if (presence === 'inside') return 'inside';
    if (badge) return 'owner';
    return null;
  });

  const rootClass = $derived(
    [
      'constellation-signal-blob',
      `constellation-signal-blob-${tone}`,
      `constellation-signal-blob-${shape}`,
      `constellation-signal-blob-${scale}`,
      `constellation-signal-blob-semantic-${semanticLevel}`,
      `constellation-signal-blob-${state}`,
      presence === 'inside' ? 'constellation-signal-blob-presence-inside' : 'constellation-signal-blob-presence-none',
      attachmentCount > 0 ? 'constellation-signal-blob-has-attachments' : 'constellation-signal-blob-no-attachments',
      interactive ? 'constellation-signal-blob-interactive' : '',
      cue === 'attention'
        ? 'constellation-signal-blob-cue-attention'
        : cue === 'risk'
          ? 'constellation-signal-blob-cue-risk'
          : 'constellation-signal-blob-cue-none',
      `constellation-signal-blob-treatment-${treatment}`,
      animated ? 'is-animated' : '',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );

  let activePointerId: number | null = null;
  let pointerMoved = false;
  let suppressNextActivate = false;
  let longPressTimer: ReturnType<typeof setTimeout> | null = null;

  function clearLongPressTimer() {
    if (longPressTimer === null) return;
    clearTimeout(longPressTimer);
    longPressTimer = null;
  }

  function handleActivate(event: MouseEvent) {
    if (!interactive) return;
    if (suppressNextActivate) {
      event.preventDefault();
      event.stopPropagation();
      suppressNextActivate = false;
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    activate?.(event);
  }

  function handleEdit(event: MouseEvent) {
    if (!interactive || !edit) return;
    event.preventDefault();
    event.stopPropagation();
    clearLongPressTimer();
    suppressNextActivate = true;
    edit();
    window.setTimeout(() => {
      suppressNextActivate = false;
    }, 0);
  }

  function handleHover() {
    if (!interactive) return;
    hover?.();
  }

  function handleUnhover() {
    if (!interactive) return;
    unhover?.();
  }

  function releasePointerCapture(event: PointerEvent) {
    const target = event.currentTarget;
    if (target instanceof HTMLElement && target.hasPointerCapture(event.pointerId)) {
      target.releasePointerCapture(event.pointerId);
    }
  }

  function startLongPressTimer(event: PointerEvent) {
    clearLongPressTimer();
    if (!longPress) return;

    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      suppressNextActivate = true;
      pointerMoved = true;
      endDrag?.(event);
      releasePointerCapture(event);
      activePointerId = null;
      longPress();
    }, Math.max(250, longPressThresholdMs));
  }

  function handlePointerDown(event: PointerEvent) {
    if (!interactive || event.button !== 0) return;
    const didStart = beginDrag?.(event);
    if (didStart === false) return;

    activePointerId = event.pointerId;
    pointerMoved = false;
    startLongPressTimer(event);

    const target = event.currentTarget;
    if (target instanceof HTMLElement) {
      target.setPointerCapture(event.pointerId);
    }
  }

  function handlePointerMove(event: PointerEvent) {
    if (activePointerId !== event.pointerId) return;
    const didMove = moveDrag?.(event);
    if (didMove) {
      clearLongPressTimer();
      pointerMoved = true;
      suppressNextActivate = true;
    }
  }

  function handlePointerUp(event: PointerEvent) {
    if (activePointerId !== event.pointerId) return;
    clearLongPressTimer();
    const dragMoved = Boolean(endDrag?.(event));
    const action = signalBlobPointerUpAction({
      dragMoved,
      pointerMoved,
      canActivate: Boolean(activate),
    });
    if (action === 'suppress-click') {
      suppressNextActivate = true;
      event.preventDefault();
      event.stopPropagation();
      window.setTimeout(() => {
        suppressNextActivate = false;
      }, 0);
    } else if (action === 'activate') {
      suppressNextActivate = true;
      event.preventDefault();
      event.stopPropagation();
      activate?.(event);
      window.setTimeout(() => {
        suppressNextActivate = false;
      }, 0);
    }
    releasePointerCapture(event);
    activePointerId = null;
    pointerMoved = false;
  }

  function handlePointerCancel(event: PointerEvent) {
    if (activePointerId !== event.pointerId) return;
    clearLongPressTimer();
    endDrag?.(event);
    releasePointerCapture(event);
    activePointerId = null;
    pointerMoved = false;
  }
</script>

{#snippet blobContent()}
  <span class="constellation-signal-blob-body">
    {#if showHalo}
      <span class="constellation-signal-blob-halo" aria-hidden="true"></span>
    {/if}

    {#if statusVariant}
      <span
        class={`constellation-signal-blob-status-anchor constellation-signal-blob-status-${statusVariant}`}
        aria-hidden="true"
      >
        {#if statusVariant === 'inside'}
          <span></span>
          <span></span>
          <span></span>
        {:else if statusVariant === 'owner'}
          <span class="constellation-signal-blob-status-owner-dot"></span>
        {:else if statusVariant === 'attention'}
          <span class="constellation-signal-blob-status-attention-dot"></span>
        {:else}
          <span class="constellation-signal-blob-status-risk-dot"></span>
        {/if}
      </span>
    {/if}

    {#if showUnreadNotification}
      <ConstellationSignalStatusIndicator state="unread" placement="anchor" {animated} label="Unread thread" />
    {/if}

    {#if showWorkingIndicator}
      <ConstellationSignalStatusIndicator state="working" placement="anchor" {animated} label="Illo is working" />
    {/if}

    {#if attachmentCount > 0}
      <span class="constellation-signal-blob-attachment-badge" aria-hidden="true">
        <ConstellationIcon name="paperclip" size={12} stroke={2} />
        {#if attachmentCount > 1}
          <span>{attachmentCount}</span>
        {/if}
      </span>
    {/if}

    <div class="constellation-signal-blob-surface">
      {#if treatment === 'seed'}
        <span class="constellation-signal-blob-owner-seed" aria-hidden="true"></span>
      {/if}
      <span class="constellation-signal-blob-icon" aria-hidden="true">
        <ConstellationIcon name={iconName} size={18} stroke={1.85} />
      </span>
      <p class="constellation-signal-blob-text">{text}</p>
    </div>
  </span>
{/snippet}

{#if interactive}
  <button
    type="button"
    class={rootClass}
    {style}
    onclick={handleActivate}
    ondblclick={handleEdit}
    onpointerdown={handlePointerDown}
    onpointerenter={handleHover}
    onpointermove={handlePointerMove}
    onpointerup={handlePointerUp}
    onpointercancel={handlePointerCancel}
    onpointerleave={handleUnhover}
    onfocus={handleHover}
    onblur={handleUnhover}
    aria-label={fullLabel}
    title={fullLabel}
    data-constellation-signal-id={dataId}
  >
    {@render blobContent()}
  </button>
{:else}
  <article class={rootClass} {style} aria-label={fullLabel} title={fullLabel} data-constellation-signal-id={dataId}>
    {@render blobContent()}
  </article>
{/if}

<style>
  .constellation-signal-blob {
    --blob-shell: rgba(5, 8, 14, 0.99);
    --blob-core: var(--constellation-color-spectral-core);
    --blob-fill-working: color-mix(in srgb, var(--blob-shell) 66%, var(--blob-core) 34%);
    --blob-fill-idle: color-mix(in srgb, var(--blob-shell) 78%, var(--blob-core) 22%);
    --blob-fill-done: color-mix(in srgb, var(--blob-shell) 84%, var(--blob-core) 16%);
    --blob-fill-current: var(--blob-fill-idle);
    --blob-inner-stroke-strength: 7%;
    --blob-contour-strength: 58%;
    --blob-contour-opacity: 0.82;
    --blob-rim: color-mix(in srgb, var(--constellation-color-spectral) 50%, rgba(240, 240, 250, 0.18));
    --blob-rim-hot: color-mix(in srgb, var(--constellation-color-spectral) 74%, #dffdf4 26%);
    --blob-rim-soft: color-mix(in srgb, var(--constellation-color-spectral) 56%, rgba(240, 250, 248, 0.22));
    --blob-bloom: color-mix(in srgb, var(--constellation-color-spectral) 34%, transparent);
    --blob-shadow: color-mix(in srgb, var(--constellation-color-spectral) 46%, transparent);
    --blob-owner: var(--constellation-color-spectral-owner);
    --blob-seed: var(--constellation-color-spectral);
    --blob-state-bloom-opacity: 0.58;
    --blob-rim-glint-opacity: 1;
    --blob-status-shell: rgba(5, 9, 16, 0.94);
    --blob-status-border: rgba(240, 240, 250, 0.18);
    --blob-status-icon: rgba(240, 240, 250, 0.92);
    --blob-status-attention: color-mix(in srgb, var(--blob-owner) 76%, white 24%);
    --blob-status-risk: var(--color-danger, #c54a57);
    --blob-halo-border: color-mix(in srgb, var(--blob-owner) 58%, transparent);
    --blob-halo-glow: color-mix(in srgb, var(--blob-owner) 52%, transparent);
    --blob-halo-opacity: 0.34;
    --blob-halo-border-width: 1px;
    --blob-presence-bloom-opacity: 0;
    --blob-presence-bloom-scale: 1;
    --blob-unread-color: var(--blob-seed);
    position: absolute;
    isolation: isolate;
    transform: translate(-50%, -50%);
    pointer-events: none;
    appearance: none;
    padding: 0;
    border: 0;
    background: transparent;
    font: inherit;
    color: inherit;
    text-align: inherit;
    transition:
      width 190ms var(--constellation-motion-ease-lift),
      height 190ms var(--constellation-motion-ease-lift),
      opacity 160ms ease,
      filter 160ms ease;
  }

  .constellation-signal-blob-interactive {
    pointer-events: auto;
    cursor: pointer;
  }

  .constellation-signal-blob-body {
    position: absolute;
    inset: 0;
    display: block;
    transform-origin: center;
    pointer-events: none;
    will-change: transform;
  }

  .constellation-signal-blob-halo {
    position: absolute;
    inset: -18px;
    z-index: 0;
    pointer-events: none;
    border-radius: 50%;
    border: var(--blob-halo-border-width) solid var(--blob-halo-border);
    box-shadow: 0 0 28px var(--blob-halo-glow);
    opacity: var(--blob-halo-opacity);
    transform: scale(1);
    transform-origin: center;
  }

  .constellation-signal-blob-halo::before,
  .constellation-signal-blob-halo::after {
    content: '';
    position: absolute;
    border-radius: 999px;
    opacity: 0;
    transform: scale(1);
    transform-origin: center;
    transition:
      opacity var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      transform var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-signal-blob-halo::before {
    top: 16%;
    left: 12%;
    width: 64px;
    height: 40px;
    background: radial-gradient(
      circle at 46% 46%,
      color-mix(in srgb, var(--blob-owner) 58%, transparent) 0%,
      color-mix(in srgb, var(--blob-owner) 36%, transparent) 50%,
      transparent 100%
    );
    filter: blur(15px);
  }

  .constellation-signal-blob-halo::after {
    right: 10%;
    bottom: 14%;
    width: 52px;
    height: 30px;
    background: radial-gradient(
      circle at 48% 48%,
      color-mix(in srgb, var(--blob-owner) 52%, transparent) 0%,
      color-mix(in srgb, var(--blob-owner) 30%, transparent) 48%,
      transparent 100%
    );
    filter: blur(14px);
  }

  .constellation-signal-blob-surface {
    position: relative;
    z-index: 1;
    isolation: isolate;
    display: flex;
    width: 100%;
    height: 100%;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 5px;
    overflow: hidden;
    padding: 13px 16px 14px;
    border: 1px solid var(
      --constellation-signal-blob-surface-border,
      color-mix(in srgb, var(--blob-rim-soft) 82%, rgba(244, 247, 252, 0.14))
    );
    background: var(
      --constellation-signal-blob-surface-background,
      linear-gradient(145deg, rgba(255, 255, 255, 0.085), transparent 28%),
      radial-gradient(ellipse at 76% 78%, rgba(255, 255, 255, 0.036), transparent 45%),
      radial-gradient(ellipse at 52% 106%, rgba(0, 0, 0, 0.34), transparent 54%),
      var(--blob-fill-current)
    );
    color: var(--blob-owner);
    text-align: center;
    box-shadow: var(
      --constellation-signal-blob-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) var(--blob-inner-stroke-strength), transparent),
      inset 0 16px 24px rgba(255, 255, 255, 0.024),
      inset 0 -26px 36px rgba(0, 0, 0, 0.34),
      0 13px 24px rgba(0, 0, 0, 0.34),
      0 0 24px color-mix(in srgb, var(--blob-shadow) 92%, transparent),
      0 0 64px color-mix(in srgb, var(--blob-shadow) 52%, transparent)
    );
    transition:
      transform var(--constellation-motion-hover-duration) var(--constellation-motion-ease-lift),
      box-shadow var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      border-color var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      border-radius 190ms var(--constellation-motion-ease-lift),
      padding 190ms var(--constellation-motion-ease-lift),
      gap 190ms var(--constellation-motion-ease-lift);
  }

  .constellation-signal-blob-surface::before,
  .constellation-signal-blob-surface::after {
    content: '';
    position: absolute;
    pointer-events: none;
  }

  .constellation-signal-blob-surface::before {
    left: var(--blob-bloom-left, 8%);
    top: var(--blob-bloom-top, 10%);
    width: var(--blob-bloom-width, 84%);
    height: var(--blob-bloom-height, 72%);
    border-radius: 999px;
    background: var(
      --constellation-signal-blob-surface-bloom-background,
      radial-gradient(
      circle at 50% 48%,
      color-mix(in srgb, var(--blob-owner) 22%, transparent) 0%,
      color-mix(in srgb, var(--blob-owner) 9%, transparent) 44%,
      transparent 80%
      )
    );
    filter: var(--constellation-signal-blob-surface-bloom-filter, blur(22px) saturate(1.14));
    opacity: var(--blob-state-bloom-opacity);
    transition:
      opacity var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift),
      transform var(--constellation-motion-settle-duration) var(--constellation-motion-ease-lift);
  }

  .constellation-signal-blob-surface::after {
    inset: 0;
    border-radius: inherit;
    box-shadow: var(
      --constellation-signal-blob-surface-contour-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-rim) var(--blob-contour-strength), transparent),
      inset 0 0 22px color-mix(in srgb, var(--blob-rim-soft) 46%, transparent),
      0 0 18px color-mix(in srgb, var(--blob-rim-hot) 38%, transparent)
    );
    filter: var(
      --constellation-signal-blob-surface-contour-filter,
      drop-shadow(0 0 9px color-mix(in srgb, var(--blob-rim-hot) 58%, transparent))
    );
    opacity: var(--blob-contour-opacity);
    mix-blend-mode: var(--constellation-signal-blob-surface-contour-blend, screen);
  }

  .constellation-signal-blob-owner-seed,
  .constellation-signal-blob-status-anchor,
  .constellation-signal-blob-attachment-badge {
    position: absolute;
    z-index: 2;
  }

  .constellation-signal-blob-owner-seed {
    top: 18px;
    left: 20px;
    width: calc(9px * var(--blob-seed-scale, 1));
    height: calc(9px * var(--blob-seed-scale, 1));
    border-radius: var(--constellation-radius-pill);
    background: color-mix(in srgb, var(--blob-seed) 84%, white 16%);
    box-shadow:
      0 0 0 2px var(--constellation-color-badge-ring),
      0 0 16px color-mix(in srgb, var(--blob-seed) 56%, transparent);
    opacity: var(--blob-seed-opacity, 0.9);
  }

  .constellation-signal-blob-owner-seed::after {
    content: '';
    position: absolute;
    inset: 2px;
    border-radius: inherit;
    background: rgba(255, 255, 255, 0.22);
  }

  .constellation-signal-blob-text {
    position: relative;
    z-index: 2;
    margin: 0;
    max-width: 88%;
    font-family: var(--constellation-font-sans);
    font-size: 14px;
    font-weight: 500;
    line-height: 1.3;
    opacity: 1;
    transform: translateY(0) scale(1);
    transform-origin: center;
    transition:
      opacity 140ms ease,
      transform 180ms var(--constellation-motion-ease-lift),
      font-size 180ms var(--constellation-motion-ease-lift),
      line-height 180ms var(--constellation-motion-ease-lift);
  }

  .constellation-signal-blob-icon {
    position: relative;
    z-index: 2;
    display: grid;
    width: 20px;
    height: 20px;
    place-items: center;
    color: var(
      --constellation-signal-blob-icon-color,
      color-mix(in srgb, var(--blob-owner) 90%, rgba(240, 240, 250, 0.68))
    );
    filter: var(
      --constellation-signal-blob-icon-filter,
      drop-shadow(0 0 10px color-mix(in srgb, var(--blob-owner) 24%, transparent))
    );
    opacity: var(--constellation-signal-blob-icon-opacity, 0.9);
    transform: scale(1);
    transition:
      width 180ms var(--constellation-motion-ease-lift),
      height 180ms var(--constellation-motion-ease-lift),
      opacity 140ms ease,
      transform 180ms var(--constellation-motion-ease-lift);
  }

  .constellation-signal-blob-icon :global(svg) {
    display: block;
    transition:
      width 180ms var(--constellation-motion-ease-lift),
      height 180ms var(--constellation-motion-ease-lift);
  }

  .constellation-signal-blob-spectral {
    --blob-core: var(--constellation-color-spectral-core);
    --blob-rim: color-mix(in srgb, var(--constellation-color-spectral) 50%, rgba(240, 240, 250, 0.16));
    --blob-rim-hot: color-mix(in srgb, var(--constellation-color-spectral) 74%, #dffdf4 26%);
    --blob-rim-soft: color-mix(in srgb, var(--constellation-color-spectral) 56%, rgba(240, 250, 248, 0.22));
    --blob-bloom: color-mix(in srgb, var(--constellation-color-spectral) 28%, transparent);
    --blob-shadow: color-mix(in srgb, var(--constellation-color-spectral) 38%, transparent);
    --blob-owner: var(--constellation-color-spectral-owner);
    --blob-seed: var(--constellation-color-spectral);
  }

  .constellation-signal-blob-amber {
    --blob-core: var(--constellation-color-amber-core);
    --blob-rim: color-mix(in srgb, var(--constellation-color-amber) 52%, rgba(240, 240, 250, 0.16));
    --blob-rim-hot: color-mix(in srgb, var(--constellation-color-amber) 74%, #ffe8bd 26%);
    --blob-rim-soft: color-mix(in srgb, var(--constellation-color-amber) 56%, rgba(255, 235, 204, 0.22));
    --blob-bloom: color-mix(in srgb, var(--constellation-color-amber) 28%, transparent);
    --blob-shadow: color-mix(in srgb, var(--constellation-color-amber) 38%, transparent);
    --blob-owner: var(--constellation-color-amber-owner);
    --blob-seed: var(--constellation-color-amber);
  }

  .constellation-signal-blob-hero .constellation-signal-blob-surface {
    gap: 10px;
    padding: 22px 24px 21px;
  }

  .constellation-signal-blob-hero .constellation-signal-blob-text {
    font-size: var(--constellation-type-blob-hero);
    line-height: 1.36;
  }

  .constellation-signal-blob-compact .constellation-signal-blob-surface {
    gap: 5px;
    padding: 12px 13px 11px;
  }

  .constellation-signal-blob-compact .constellation-signal-blob-icon {
    width: 20px;
    height: 20px;
  }

  .constellation-signal-blob-compact .constellation-signal-blob-text {
    font-size: 13px;
    line-height: 1.28;
  }

  .constellation-signal-blob-semantic-summary .constellation-signal-blob-surface {
    gap: 4px;
    padding: 11px 13px 12px;
  }

  .constellation-signal-blob-semantic-summary .constellation-signal-blob-text {
    display: -webkit-box;
    overflow: hidden;
    max-width: 82%;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    font-size: 13px;
    line-height: 1.22;
    opacity: 0.92;
  }

  .constellation-signal-blob-semantic-symbol,
  .constellation-signal-blob-semantic-glyph {
    --blob-state-bloom-opacity: var(--constellation-signal-blob-symbol-state-bloom-opacity, 0.5);
    --blob-contour-opacity: var(--constellation-signal-blob-symbol-contour-opacity, 0.66);
  }

  .constellation-signal-blob-semantic-symbol .constellation-signal-blob-surface,
  .constellation-signal-blob-semantic-glyph .constellation-signal-blob-surface {
    gap: 0;
    padding: 0;
    border-radius: 999px;
  }

  .constellation-signal-blob-semantic-symbol .constellation-signal-blob-text,
  .constellation-signal-blob-semantic-glyph .constellation-signal-blob-text {
    position: absolute;
    width: min(84%, 72px);
    height: auto;
    overflow: hidden;
    opacity: 0;
    transform: translateY(8px) scale(0.72);
    white-space: nowrap;
  }

  .constellation-signal-blob-semantic-symbol .constellation-signal-blob-icon {
    width: 25px;
    height: 25px;
    opacity: 0.96;
    transform: scale(1.05);
  }

  .constellation-signal-blob-semantic-symbol .constellation-signal-blob-icon :global(svg) {
    width: 22px;
    height: 22px;
  }

  .constellation-signal-blob-semantic-glyph .constellation-signal-blob-icon {
    width: 22px;
    height: 22px;
    opacity: 0.96;
    transform: scale(1.02);
  }

  .constellation-signal-blob-semantic-glyph .constellation-signal-blob-attachment-badge {
    opacity: 0;
    pointer-events: none;
    transform: scale(0.72);
    transition:
      opacity 120ms ease,
      transform 180ms var(--constellation-motion-ease-lift);
  }

  .constellation-signal-blob-alpha .constellation-signal-blob-surface {
    border-radius: var(--constellation-shape-alpha);
  }

  .constellation-signal-blob-beta .constellation-signal-blob-surface {
    border-radius: var(--constellation-shape-beta);
  }

  .constellation-signal-blob-gamma .constellation-signal-blob-surface {
    border-radius: var(--constellation-shape-gamma);
  }

  .constellation-signal-blob-delta .constellation-signal-blob-surface {
    border-radius: var(--constellation-shape-delta);
  }

  .constellation-signal-blob-semantic-symbol .constellation-signal-blob-surface,
  .constellation-signal-blob-semantic-glyph .constellation-signal-blob-surface {
    border-radius: 999px;
  }

  .constellation-signal-blob-idle {
    --blob-fill-current: var(--blob-fill-idle);
    --blob-inner-stroke-strength: var(--constellation-signal-blob-idle-inner-stroke-strength, 7%);
    --blob-contour-strength: var(--constellation-signal-blob-idle-contour-strength, 58%);
    --blob-contour-opacity: var(--constellation-signal-blob-idle-contour-opacity, 0.76);
    --blob-state-bloom-opacity: var(--constellation-signal-blob-idle-state-bloom-opacity, 0.46);
    --blob-seed-opacity: 0.96;
  }

  .constellation-signal-blob-working {
    --blob-fill-current: color-mix(in srgb, var(--blob-shell) 66%, var(--blob-core) 34%);
    --blob-inner-stroke-strength: var(--constellation-signal-blob-working-inner-stroke-strength, 8%);
    --blob-contour-strength: var(--constellation-signal-blob-working-contour-strength, 64%);
    --blob-contour-opacity: var(--constellation-signal-blob-working-contour-opacity, 0.84);
    --blob-state-bloom-opacity: var(--constellation-signal-blob-working-state-bloom-opacity, 0.68);
    --constellation-signal-blob-work-pulse-animation: none;
    --blob-seed-opacity: 1;
    --blob-halo-opacity: var(--constellation-signal-blob-working-halo-opacity, 0.46);
    --blob-halo-border-width: 1px;
    --blob-halo-border: color-mix(in srgb, var(--blob-owner) 58%, white 10%);
    --blob-halo-glow: color-mix(in srgb, var(--blob-owner) 58%, transparent);
  }

  .constellation-signal-blob-done {
    --blob-fill-current: var(--blob-fill-done);
    --blob-inner-stroke-strength: var(--constellation-signal-blob-done-inner-stroke-strength, 5%);
    --blob-contour-strength: var(--constellation-signal-blob-done-contour-strength, 20%);
    --blob-contour-opacity: var(--constellation-signal-blob-done-contour-opacity, 0.24);
    --blob-state-bloom-opacity: var(--constellation-signal-blob-done-state-bloom-opacity, 0.22);
    --blob-seed-opacity: 0.8;
  }

  .constellation-signal-blob-working .constellation-signal-blob-surface {
    box-shadow: var(
      --constellation-signal-blob-working-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) var(--blob-inner-stroke-strength), transparent),
      inset 0 16px 24px rgba(255, 255, 255, 0.024),
      inset 0 -26px 36px rgba(0, 0, 0, 0.4),
      0 13px 24px rgba(0, 0, 0, 0.32),
      0 0 30px color-mix(in srgb, var(--blob-shadow) 100%, transparent),
      0 0 76px color-mix(in srgb, var(--blob-shadow) 62%, transparent)
    );
  }

  .constellation-signal-blob-working .constellation-signal-blob-surface::before {
    transform: scale(1.12);
  }

  .constellation-signal-blob-working .constellation-signal-blob-surface::after {
    box-shadow: var(
      --constellation-signal-blob-working-contour-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-rim) var(--blob-contour-strength), transparent),
      inset 0 0 20px color-mix(in srgb, var(--blob-owner) 12%, transparent),
      0 0 16px color-mix(in srgb, var(--blob-rim-hot) 32%, transparent)
    );
  }

  .constellation-signal-blob-done .constellation-signal-blob-surface {
    box-shadow: var(
      --constellation-signal-blob-done-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) var(--blob-inner-stroke-strength), transparent),
      inset 0 -12px 16px rgba(0, 0, 0, 0.1),
      0 14px 26px rgba(0, 0, 0, 0.16),
      0 0 14px color-mix(in srgb, var(--blob-shadow) 42%, transparent)
    );
  }

  .constellation-signal-blob-treatment-bloom {
    --blob-bloom-width: 56%;
    --blob-bloom-height: 48%;
    --blob-bloom-left: 10%;
    --blob-bloom-top: 10%;
    --blob-contour-opacity: var(--constellation-signal-blob-bloom-treatment-contour-opacity, 0.46);
  }

  .constellation-signal-blob-treatment-contour {
    --blob-bloom-width: 34%;
    --blob-bloom-height: 32%;
    --blob-bloom-left: 8%;
    --blob-bloom-top: 14%;
    --blob-contour-strength: 42%;
    --blob-contour-opacity: 0.58;
    --blob-seed-opacity: 0.76;
  }

  .constellation-signal-blob-treatment-contour .constellation-signal-blob-surface {
    box-shadow: var(
      --constellation-signal-blob-contour-treatment-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) 5%, transparent),
      0 18px 34px rgba(0, 0, 0, 0.2),
      0 0 16px color-mix(in srgb, var(--blob-shadow) 56%, transparent)
    );
  }

  .constellation-signal-blob-treatment-contour .constellation-signal-blob-surface::after {
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--blob-rim) var(--blob-contour-strength), transparent),
      inset 0 0 18px color-mix(in srgb, var(--blob-owner) 6%, transparent);
  }

  .constellation-signal-blob-treatment-seed {
    --blob-bloom-width: 26%;
    --blob-bloom-height: 24%;
    --blob-bloom-left: 18%;
    --blob-bloom-top: 18%;
    --blob-contour-opacity: 0.54;
    --blob-seed-scale: 1.22;
  }

  .constellation-signal-blob-treatment-seed .constellation-signal-blob-surface {
    background: var(
      --constellation-signal-blob-seed-treatment-background,
      color-mix(in srgb, rgba(5, 9, 16, 0.98) 90%, var(--blob-seed))
    );
  }

  .constellation-signal-blob-presence-inside {
    --blob-inner-stroke-strength: 5%;
    --blob-contour-strength: 44%;
    --blob-contour-opacity: 0.62;
    --blob-state-bloom-opacity: 0.32;
    --blob-halo-opacity: 0.24;
    --blob-halo-border: color-mix(in srgb, var(--blob-owner) 58%, white 10%);
    --blob-halo-glow: color-mix(in srgb, var(--blob-owner) 46%, transparent);
    --blob-presence-bloom-opacity: 0.2;
    --blob-presence-bloom-scale: 1.02;
  }

  .constellation-signal-blob-presence-inside .constellation-signal-blob-surface {
    box-shadow: var(
      --constellation-signal-blob-presence-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) var(--blob-inner-stroke-strength), transparent),
      inset 0 16px 24px rgba(255, 255, 255, 0.02),
      inset 0 -26px 36px rgba(0, 0, 0, 0.36),
      0 14px 28px rgba(0, 0, 0, 0.22),
      0 0 28px color-mix(in srgb, var(--blob-shadow) 48%, transparent)
    );
  }

  .constellation-signal-blob-presence-inside .constellation-signal-blob-halo::before,
  .constellation-signal-blob-presence-inside .constellation-signal-blob-halo::after {
    opacity: var(--blob-presence-bloom-opacity);
    transform: scale(var(--blob-presence-bloom-scale));
  }

  .constellation-signal-blob-working.constellation-signal-blob-presence-inside {
    --blob-inner-stroke-strength: 12%;
    --blob-contour-strength: 28%;
    --blob-contour-opacity: 0.3;
    --blob-state-bloom-opacity: 1;
    --blob-halo-opacity: 0.46;
    --blob-halo-border: color-mix(in srgb, var(--blob-owner) 82%, white 16%);
    --blob-halo-glow: color-mix(in srgb, var(--blob-owner) 84%, transparent);
    --blob-presence-bloom-opacity: 0.6;
    --blob-presence-bloom-scale: 1.08;
  }

  .constellation-signal-blob-working.constellation-signal-blob-presence-inside .constellation-signal-blob-surface {
    box-shadow: var(
      --constellation-signal-blob-presence-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) var(--blob-inner-stroke-strength), transparent),
      inset 0 -14px 18px rgba(0, 0, 0, 0.12),
      0 16px 30px rgba(0, 0, 0, 0.18),
      0 0 40px color-mix(in srgb, var(--blob-shadow) 100%, transparent)
    );
  }

  .constellation-signal-blob-working.constellation-signal-blob-presence-inside .constellation-signal-blob-surface::before {
    transform: scale(1.08);
  }

  .constellation-signal-blob-cue-attention {
    --blob-contour-strength: var(--constellation-signal-blob-attention-contour-strength, 44%);
    --blob-contour-opacity: var(--constellation-signal-blob-attention-contour-opacity, 0.48);
    --blob-state-bloom-opacity: var(--constellation-signal-blob-attention-state-bloom-opacity, 0.3);
  }

  .constellation-signal-blob-cue-attention .constellation-signal-blob-surface {
    box-shadow: var(
      --constellation-signal-blob-working-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) var(--blob-inner-stroke-strength), transparent),
      inset 0 16px 24px rgba(255, 255, 255, 0.024),
      inset 0 -26px 36px rgba(0, 0, 0, 0.4),
      0 13px 24px rgba(0, 0, 0, 0.32),
      0 0 18px color-mix(in srgb, var(--blob-shadow) 76%, transparent),
      0 0 44px color-mix(in srgb, var(--blob-shadow) 34%, transparent)
    );
  }

  .constellation-signal-blob-cue-risk {
    --blob-contour-strength: 26%;
    --blob-contour-opacity: 0.24;
    --blob-state-bloom-opacity: 0.5;
    --blob-halo-opacity: 0.44;
    --blob-halo-border: color-mix(in srgb, var(--blob-status-risk) 88%, transparent);
    --blob-halo-glow: color-mix(in srgb, var(--blob-status-risk) 74%, transparent);
  }

  .constellation-signal-blob-cue-risk .constellation-signal-blob-surface {
    box-shadow: var(
      --constellation-signal-blob-risk-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-status-risk) 8%, transparent),
      inset 0 -14px 18px rgba(0, 0, 0, 0.12),
      0 14px 26px rgba(0, 0, 0, 0.18),
      0 0 24px color-mix(in srgb, var(--blob-status-risk) 28%, transparent)
    );
  }

  .constellation-signal-blob-status-anchor {
    top: 8px;
    right: 8px;
    z-index: 3;
    pointer-events: none;
    transform: translate(42%, -42%);
  }

  .constellation-signal-blob-status-owner-dot {
    display: block;
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--blob-owner) 72%, white 28%);
    box-shadow:
      0 0 0 2px rgba(5, 9, 16, 0.92),
      0 0 14px color-mix(in srgb, var(--blob-owner) 62%, transparent);
  }

  .constellation-signal-blob-status-inside {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 3px;
    min-width: 24px;
    height: 16px;
    padding: 0 6px;
    border-radius: 999px;
    background: var(--blob-status-shell);
    border: 1px solid color-mix(in srgb, var(--blob-owner) 26%, var(--blob-status-border));
    box-shadow:
      inset 0 0 0 1px rgba(255, 255, 255, 0.04),
      0 10px 18px rgba(0, 0, 0, 0.24),
      0 0 18px color-mix(in srgb, var(--blob-owner) 24%, transparent);
    transform: translate(26%, -28%);
  }

  .constellation-signal-blob-status-inside span {
    width: 3px;
    height: 3px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--blob-owner) 72%, white 28%);
    box-shadow: 0 0 10px color-mix(in srgb, var(--blob-owner) 42%, transparent);
    opacity: 0.72;
  }

  .constellation-signal-blob-status-inside span:nth-child(2) {
    animation-delay: 120ms;
  }

  .constellation-signal-blob-status-inside span:nth-child(3) {
    animation-delay: 240ms;
  }

  .constellation-signal-blob-status-attention-dot,
  .constellation-signal-blob-status-risk-dot {
    display: block;
    position: relative;
    width: 14px;
    height: 14px;
    border-radius: 999px;
    background: currentColor;
    box-shadow:
      0 0 0 3px rgba(5, 9, 16, 0.92),
      0 0 18px color-mix(in srgb, currentColor 64%, transparent);
  }

  .constellation-signal-blob-status-attention-dot::after,
  .constellation-signal-blob-status-risk-dot::after {
    content: '';
    position: absolute;
    inset: 3px;
    border-radius: inherit;
    background: rgba(255, 255, 255, 0.18);
  }

  .constellation-signal-blob-status-attention {
    color: var(--blob-status-attention);
  }

  .constellation-signal-blob-status-risk {
    color: var(--blob-status-risk);
  }

  .constellation-signal-blob-attachment-badge {
    right: 10px;
    bottom: 10px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 3px;
    min-width: 24px;
    height: 22px;
    padding: 0 6px;
    border-radius: var(--constellation-radius-pill);
    border: 1px solid color-mix(in srgb, var(--blob-owner) 18%, rgba(240, 240, 250, 0.1));
    background: color-mix(in srgb, var(--blob-shell) 82%, var(--blob-seed) 18%);
    color: color-mix(in srgb, var(--blob-owner) 78%, rgba(240, 240, 250, 0.72));
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 650;
    line-height: 1;
    box-shadow:
      0 8px 18px rgba(0, 0, 0, 0.22),
      0 0 14px color-mix(in srgb, var(--blob-shadow) 72%, transparent);
  }

  .is-animated.constellation-signal-blob-working .constellation-signal-blob-body {
    animation: constellation-signal-blob-working-heartbeat 1.25s ease-in-out infinite;
  }

  .is-animated .constellation-signal-blob-surface {
    animation: constellation-signal-blob-drift var(--constellation-motion-drift-duration) var(--constellation-motion-ease-float) infinite;
  }

  .is-animated .constellation-signal-blob-status-inside span {
    animation: constellation-signal-blob-presence-dots 1.4s ease-in-out infinite;
  }

  .is-animated.constellation-signal-blob-presence-inside .constellation-signal-blob-halo::before,
  .is-animated.constellation-signal-blob-presence-inside .constellation-signal-blob-halo::after {
    animation: constellation-signal-blob-presence-bloom 3.8s ease-in-out infinite;
  }

  .is-animated.constellation-signal-blob-presence-inside .constellation-signal-blob-halo::after {
    animation-delay: 420ms;
  }

  .is-animated.constellation-signal-blob-working:not(.constellation-signal-blob-presence-inside) .constellation-signal-blob-halo {
    animation: constellation-signal-blob-work-halo 3.8s ease-in-out infinite;
  }

  .is-animated.constellation-signal-blob-working:not(.constellation-signal-blob-presence-inside) .constellation-signal-blob-surface {
    animation: var(
      --constellation-signal-blob-work-pulse-animation,
      constellation-signal-blob-work-pulse 5.6s ease-in-out infinite
    );
  }

  .is-animated .constellation-signal-blob-status-attention-dot {
    animation: constellation-signal-blob-cue-dot-pulse 2.2s ease-in-out infinite;
  }

  .is-animated .constellation-signal-blob-status-risk-dot {
    animation: constellation-signal-blob-cue-dot-pulse 2.5s ease-in-out infinite;
  }

  .constellation-signal-blob-interactive:is(:hover, :focus-visible) {
    --blob-contour-opacity: var(--constellation-signal-blob-hover-contour-opacity, 0.86);
    --blob-state-bloom-opacity: var(--constellation-signal-blob-hover-state-bloom-opacity, 0.52);
  }

  .constellation-signal-blob-interactive:is(:hover, :focus-visible) .constellation-signal-blob-surface {
    border-color: var(
      --constellation-signal-blob-hover-surface-border,
      color-mix(in srgb, var(--blob-rim-hot) 64%, rgba(244, 247, 252, 0.18))
    );
    box-shadow: var(
      --constellation-signal-blob-hover-surface-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) 8%, transparent),
      inset 0 16px 24px rgba(255, 255, 255, 0.034),
      inset 0 -26px 36px rgba(0, 0, 0, 0.38),
      0 16px 28px rgba(0, 0, 0, 0.36),
      0 0 24px color-mix(in srgb, var(--blob-shadow) 96%, transparent),
      0 0 62px color-mix(in srgb, var(--blob-shadow) 58%, transparent)
    );
  }

  .constellation-signal-blob-interactive:is(:hover, :focus-visible) .constellation-signal-blob-surface::before {
    transform: scale(1.16);
  }

  .constellation-signal-blob-interactive:is(:hover, :focus-visible) .constellation-signal-blob-surface::after {
    box-shadow: var(
      --constellation-signal-blob-hover-contour-shadow,
      inset 0 0 0 1px color-mix(in srgb, var(--blob-rim) 62%, transparent),
      inset 0 0 22px color-mix(in srgb, var(--blob-rim-soft) 46%, transparent),
      0 0 18px color-mix(in srgb, var(--blob-rim-hot) 42%, transparent)
    );
  }

  @keyframes constellation-signal-blob-drift {
    0%,
    100% {
      transform: translateY(0);
    }

    50% {
      transform: translateY(-3px);
    }
  }

  @keyframes constellation-signal-blob-work-pulse {
    0%,
    100% {
      transform: scale(1);
      box-shadow:
        inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) 7%, transparent),
        inset 0 16px 24px rgba(255, 255, 255, 0.024),
        inset 0 -26px 36px rgba(0, 0, 0, 0.4),
        0 13px 24px rgba(0, 0, 0, 0.32),
        0 0 18px color-mix(in srgb, var(--blob-shadow) 80%, transparent),
        0 0 40px color-mix(in srgb, var(--blob-shadow) 38%, transparent);
    }

    50% {
      transform: scale(1.01);
      box-shadow:
        inset 0 0 0 1px color-mix(in srgb, var(--blob-owner) 9%, transparent),
        inset 0 16px 24px rgba(255, 255, 255, 0.028),
        inset 0 -26px 36px rgba(0, 0, 0, 0.4),
        0 14px 26px rgba(0, 0, 0, 0.34),
        0 0 24px color-mix(in srgb, var(--blob-shadow) 98%, transparent),
        0 0 58px color-mix(in srgb, var(--blob-shadow) 52%, transparent);
    }
  }

  @keyframes constellation-signal-blob-work-halo {
    0%,
    9%,
    24%,
    42%,
    100% {
      transform: scale(1);
      opacity: 0.24;
      box-shadow:
        0 0 24px color-mix(in srgb, var(--blob-shadow) 52%, transparent),
        0 0 52px color-mix(in srgb, var(--blob-owner) 10%, transparent);
    }

    16% {
      transform: scale(1.068);
      opacity: 0.42;
      box-shadow:
        0 0 32px color-mix(in srgb, var(--blob-shadow) 68%, transparent),
        0 0 76px color-mix(in srgb, var(--blob-owner) 16%, transparent);
    }

    31% {
      transform: scale(1.048);
      opacity: 0.34;
      box-shadow:
        0 0 28px color-mix(in srgb, var(--blob-shadow) 62%, transparent),
        0 0 64px color-mix(in srgb, var(--blob-owner) 14%, transparent);
    }
  }

  @keyframes constellation-signal-blob-presence-bloom {
    0%,
    100% {
      opacity: calc(var(--blob-presence-bloom-opacity) * 0.84);
      transform: scale(calc(var(--blob-presence-bloom-scale) * 0.96));
    }

    50% {
      opacity: var(--blob-presence-bloom-opacity);
      transform: scale(var(--blob-presence-bloom-scale));
    }
  }

  @keyframes constellation-signal-blob-presence-dots {
    0%,
    100% {
      opacity: 0.38;
      transform: scale(0.9);
    }

    50% {
      opacity: 1;
      transform: scale(1.1);
    }
  }

  @keyframes constellation-signal-blob-cue-dot-pulse {
    0%,
    100% {
      box-shadow:
        0 0 0 3px rgba(5, 9, 16, 0.92),
        0 0 14px color-mix(in srgb, currentColor 52%, transparent);
    }

    50% {
      box-shadow:
        0 0 0 3px rgba(5, 9, 16, 0.92),
        0 0 24px color-mix(in srgb, currentColor 82%, transparent);
    }
  }

  @keyframes constellation-signal-blob-working-heartbeat {
    0%,
    100% {
      transform: scale(1);
      filter: saturate(1) brightness(1);
    }

    16% {
      transform: scale(1.09);
      filter: saturate(1.08) brightness(1.025);
    }

    30% {
      transform: scale(0.985);
      filter: saturate(1.02) brightness(1);
    }

    46% {
      transform: scale(1.055);
      filter: saturate(1.06) brightness(1.01);
    }

    64% {
      transform: scale(1);
      filter: saturate(1) brightness(1);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .is-animated .constellation-signal-blob-surface,
    .is-animated.constellation-signal-blob-working:not(.constellation-signal-blob-presence-inside) .constellation-signal-blob-surface,
    .is-animated.constellation-signal-blob-working:not(.constellation-signal-blob-presence-inside) .constellation-signal-blob-halo,
    .is-animated.constellation-signal-blob-presence-inside .constellation-signal-blob-halo::before,
    .is-animated.constellation-signal-blob-presence-inside .constellation-signal-blob-halo::after,
    .is-animated .constellation-signal-blob-status-inside span,
    .is-animated .constellation-signal-blob-status-attention-dot,
    .is-animated .constellation-signal-blob-status-risk-dot,
    .is-animated.constellation-signal-blob-working .constellation-signal-blob-body {
      animation: none;
    }
  }
</style>
