<script lang="ts">
  import { tick } from 'svelte';

  import {
    ConstellationIcon,
    ConstellationIconButton,
  } from '$lib/components/constellation';
  import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    attachmentCanEmbed,
    type AttachmentPreviewKind,
  } from '$lib/utils/attachmentPreview';

  type ImagePan = {
    x: number;
    y: number;
  };

  type DragState = {
    pointerId: number;
    clientX: number;
    clientY: number;
    panX: number;
    panY: number;
  };

  const MIN_IMAGE_ZOOM = 1;
  const MAX_IMAGE_ZOOM = 4;
  const IMAGE_ZOOM_STEP = 0.25;

  let {
    url,
    label,
    detail = '',
    kind = 'file',
    openUrl = '',
    fallbackIcon = 'file',
    className = '',
    onClose,
  }: {
    url: string;
    label: string;
    detail?: string;
    kind?: AttachmentPreviewKind;
    openUrl?: string;
    fallbackIcon?: ConstellationIconName;
    className?: string;
    onClose?: () => void;
  } = $props();

  let dialogEl: HTMLDivElement | undefined = $state();
  let previewIdentity = $state('');
  let imageZoom = $state(1);
  let imagePan: ImagePan = $state({ x: 0, y: 0 });
  let imageDrag: DragState | null = $state(null);

  const isImage = $derived(kind === 'image');
  const isEmbeddable = $derived(attachmentCanEmbed(kind));
  const resolvedOpenUrl = $derived(openUrl || url);
  const zoomPercent = $derived(`${Math.round(imageZoom * 100)}%`);
  const canZoomOut = $derived(isImage && imageZoom > MIN_IMAGE_ZOOM);
  const canZoomIn = $derived(isImage && imageZoom < MAX_IMAGE_ZOOM);
  const layerClass = $derived(
    ['attachment-preview-dialog-layer', className].filter(Boolean).join(' '),
  );
  const frameClass = $derived(
    [
      'attachment-preview-dialog-frame',
      `is-${kind}`,
      imageZoom > MIN_IMAGE_ZOOM ? 'is-zoomed' : '',
      imageDrag ? 'is-dragging' : '',
    ].filter(Boolean).join(' '),
  );
  const imageStyle = $derived(
    [
      `--preview-image-scale:${imageZoom}`,
      `--preview-image-pan-x:${imagePan.x}px`,
      `--preview-image-pan-y:${imagePan.y}px`,
    ].join('; '),
  );

  function portalToBody(node: HTMLElement) {
    if (typeof document === 'undefined') return {};

    document.body.appendChild(node);
    return {
      destroy() {
        node.remove();
      },
    };
  }

  $effect(() => {
    const nextIdentity = `${kind}:${url}`;
    if (!url || nextIdentity === previewIdentity) return;

    previewIdentity = nextIdentity;
    resetImageView();
    tick().then(() => dialogEl?.focus());
  });

  function clamp(value: number, min: number, max: number) {
    return Math.min(max, Math.max(min, value));
  }

  function setImageZoom(nextZoom: number) {
    imageZoom = Math.round(clamp(nextZoom, MIN_IMAGE_ZOOM, MAX_IMAGE_ZOOM) * 100) / 100;
    if (imageZoom <= MIN_IMAGE_ZOOM) {
      imagePan = { x: 0, y: 0 };
      imageDrag = null;
    }
  }

  function zoomImageBy(delta: number) {
    setImageZoom(imageZoom + delta);
  }

  function resetImageView() {
    imageZoom = MIN_IMAGE_ZOOM;
    imagePan = { x: 0, y: 0 };
    imageDrag = null;
  }

  function closePreview() {
    onClose?.();
  }

  function openExternal() {
    if (!resolvedOpenUrl || typeof window === 'undefined') return;
    window.open(resolvedOpenUrl, '_blank', 'noopener,noreferrer');
  }

  function handleKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closePreview();
      return;
    }

    if (!isImage) return;

    if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      zoomImageBy(IMAGE_ZOOM_STEP);
      return;
    }

    if (event.key === '-' || event.key === '_') {
      event.preventDefault();
      zoomImageBy(-IMAGE_ZOOM_STEP);
      return;
    }

    if (event.key === '0') {
      event.preventDefault();
      resetImageView();
    }
  }

  function handleImageWheel(event: WheelEvent) {
    if (!isImage) return;

    event.preventDefault();
    zoomImageBy(event.deltaY < 0 ? IMAGE_ZOOM_STEP : -IMAGE_ZOOM_STEP);
  }

  function handleImageDoubleClick(event: MouseEvent) {
    if (!isImage) return;

    event.preventDefault();
    if (imageZoom > MIN_IMAGE_ZOOM) {
      resetImageView();
      return;
    }
    setImageZoom(2);
  }

  function handleImagePointerDown(event: PointerEvent) {
    if (!isImage || imageZoom <= MIN_IMAGE_ZOOM) return;

    event.preventDefault();
    imageDrag = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      panX: imagePan.x,
      panY: imagePan.y,
    };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }

  function handleImagePointerMove(event: PointerEvent) {
    if (!imageDrag || event.pointerId !== imageDrag.pointerId) return;

    imagePan = {
      x: imageDrag.panX + event.clientX - imageDrag.clientX,
      y: imageDrag.panY + event.clientY - imageDrag.clientY,
    };
  }

  function handleImagePointerEnd(event: PointerEvent) {
    if (!imageDrag || event.pointerId !== imageDrag.pointerId) return;

    (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    imageDrag = null;
  }
</script>

<div
  class={layerClass}
  bind:this={dialogEl}
  use:portalToBody
  role="dialog"
  aria-modal="true"
  aria-label={`Attachment preview: ${label}`}
  tabindex="-1"
  onkeydown={handleKeydown}
>
  <button
    type="button"
    class="attachment-preview-dialog-backdrop"
    aria-label="Close attachment preview"
    onclick={closePreview}
  ></button>

  <div class="attachment-preview-dialog-panel">
    <div class="attachment-preview-dialog-toolbar">
      <div class="attachment-preview-dialog-meta">
        <strong>{label}</strong>
        {#if detail}
          <span>{detail}</span>
        {/if}
      </div>

      <div class="attachment-preview-dialog-actions">
        {#if isImage}
          <div class="attachment-preview-dialog-zoom-controls" aria-label="Image zoom controls">
            <ConstellationIconButton
              label="Zoom out"
              title="Zoom out"
              variant="secondary"
              size="md"
              disabled={!canZoomOut}
              onclick={() => zoomImageBy(-IMAGE_ZOOM_STEP)}
            >
              <ConstellationIcon name="zoom-out" size={16} stroke={1.9} />
            </ConstellationIconButton>

            <span class="attachment-preview-dialog-zoom-level" aria-live="polite">{zoomPercent}</span>

            <ConstellationIconButton
              label="Zoom in"
              title="Zoom in"
              variant="secondary"
              size="md"
              disabled={!canZoomIn}
              onclick={() => zoomImageBy(IMAGE_ZOOM_STEP)}
            >
              <ConstellationIcon name="zoom-in" size={16} stroke={1.9} />
            </ConstellationIconButton>

            <ConstellationIconButton
              label="Fit image"
              title="Fit image"
              variant="secondary"
              size="md"
              disabled={!canZoomOut}
              onclick={resetImageView}
            >
              <ConstellationIcon name="zoom-reset" size={16} stroke={1.9} />
            </ConstellationIconButton>
          </div>
        {/if}

        <ConstellationIconButton
          label="Open attachment"
          title="Open attachment"
          variant="secondary"
          size="md"
          onclick={openExternal}
        >
          <ConstellationIcon name="external-link" size={16} stroke={1.9} />
        </ConstellationIconButton>

        <ConstellationIconButton
          label="Close preview"
          title="Close preview"
          variant="secondary"
          size="md"
          onclick={closePreview}
        >
          <ConstellationIcon name="close" size={16} stroke={1.9} />
        </ConstellationIconButton>
      </div>
    </div>

    <div
      class={frameClass}
      role="group"
      aria-label={isImage ? 'Image preview canvas' : 'Attachment preview content'}
      onwheel={handleImageWheel}
      onpointerdown={handleImagePointerDown}
      onpointermove={handleImagePointerMove}
      onpointerup={handleImagePointerEnd}
      onpointercancel={handleImagePointerEnd}
      ondblclick={handleImageDoubleClick}
    >
      {#if kind === 'image'}
        <img
          class="attachment-preview-dialog-media"
          src={url}
          alt={label}
          draggable="false"
          style={imageStyle}
        />
      {:else if kind === 'video'}
        <!-- svelte-ignore a11y_media_has_caption -->
        <video
          class="attachment-preview-dialog-media"
          src={url}
          controls
          playsinline
          preload="metadata"
        ></video>
      {:else if isEmbeddable}
        <iframe
          src={url}
          title={label}
          referrerpolicy="no-referrer"
          loading="lazy"
        ></iframe>
      {:else}
        <div class="attachment-preview-dialog-fallback">
          <span class="attachment-preview-dialog-fallback-icon" aria-hidden="true">
            <ConstellationIcon name={fallbackIcon} size={34} stroke={1.6} />
          </span>
          <strong>{label}</strong>
          {#if detail}
            <span>{detail}</span>
          {/if}
          <button type="button" class="attachment-preview-dialog-open" onclick={openExternal}>
            Open attachment
          </button>
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .attachment-preview-dialog-layer {
    --preview-backdrop-background: rgba(6, 9, 16, 0.76);
    --preview-panel-border: rgba(255, 255, 255, 0.1);
    --preview-panel-background:
      linear-gradient(180deg, rgba(16, 20, 30, 0.96), rgba(8, 12, 19, 0.94)),
      rgba(8, 12, 19, 0.94);
    --preview-panel-shadow:
      0 28px 90px rgba(0, 0, 0, 0.46),
      0 0 0 1px rgba(255, 255, 255, 0.03) inset;
    --preview-toolbar-border: rgba(255, 255, 255, 0.08);
    --preview-meta-title: rgba(246, 248, 253, 0.94);
    --preview-meta-text: rgba(240, 240, 250, 0.56);
    --preview-frame-background:
      linear-gradient(135deg, rgba(255, 255, 255, 0.035), transparent 45%),
      rgba(0, 0, 0, 0.28);
    --preview-zoom-text: rgba(236, 240, 248, 0.78);
    --preview-fallback-text: rgba(240, 240, 250, 0.7);
    --preview-action-border: rgba(255, 255, 255, 0.08);
    --preview-action-background: rgba(255, 255, 255, 0.03);
    --preview-action-background-hover: rgba(255, 255, 255, 0.06);
    --preview-action-text: rgba(229, 234, 244, 0.84);

    position: fixed;
    inset: 0;
    z-index: var(--constellation-layer-modal, 1000);
    display: grid;
    place-items: center;
    box-sizing: border-box;
    padding: clamp(12px, 2.4vw, 28px);
    color: var(--constellation-color-text-primary);
  }

  :global(:root[data-color-scheme='light']) .attachment-preview-dialog-layer {
    --preview-backdrop-background: rgba(234, 241, 247, 0.78);
    --preview-panel-border: rgba(24, 35, 49, 0.1);
    --preview-panel-background:
      linear-gradient(180deg, rgba(252, 254, 255, 0.98), rgba(241, 247, 251, 0.96)),
      rgba(247, 250, 253, 0.96);
    --preview-panel-shadow:
      0 28px 90px rgba(24, 35, 49, 0.2),
      0 0 0 1px rgba(255, 255, 255, 0.6) inset;
    --preview-toolbar-border: rgba(24, 35, 49, 0.08);
    --preview-meta-title: rgba(17, 24, 35, 0.92);
    --preview-meta-text: rgba(78, 91, 108, 0.6);
    --preview-frame-background:
      linear-gradient(135deg, rgba(83, 121, 184, 0.05), transparent 48%),
      rgba(17, 24, 35, 0.06);
    --preview-zoom-text: rgba(45, 57, 73, 0.76);
    --preview-fallback-text: rgba(78, 91, 108, 0.62);
    --preview-action-border: rgba(24, 35, 49, 0.08);
    --preview-action-background: rgba(255, 255, 255, 0.52);
    --preview-action-background-hover: rgba(255, 255, 255, 0.72);
    --preview-action-text: rgba(45, 57, 73, 0.84);
  }

  .attachment-preview-dialog-layer:focus {
    outline: none;
  }

  .attachment-preview-dialog-backdrop {
    position: absolute;
    inset: 0;
    padding: 0;
    border: 0;
    background: var(--preview-backdrop-background);
    cursor: zoom-out;
    backdrop-filter: blur(16px) saturate(1.04);
    -webkit-backdrop-filter: blur(16px) saturate(1.04);
  }

  .attachment-preview-dialog-panel {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    width: min(1380px, 100%);
    height: min(940px, 100%);
    box-sizing: border-box;
    overflow: hidden;
    border: 1px solid var(--preview-panel-border);
    border-radius: var(--constellation-radius-panel, 16px);
    background: var(--preview-panel-background);
    box-shadow: var(--preview-panel-shadow);
  }

  .attachment-preview-dialog-toolbar {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 14px;
    min-width: 0;
    padding: 10px 10px 10px 14px;
    border-bottom: 1px solid var(--preview-toolbar-border);
  }

  .attachment-preview-dialog-meta {
    display: grid;
    min-width: 0;
    gap: 3px;
  }

  .attachment-preview-dialog-meta strong,
  .attachment-preview-dialog-meta span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attachment-preview-dialog-meta strong {
    color: var(--preview-meta-title);
    font-size: 13px;
    font-weight: 620;
  }

  .attachment-preview-dialog-meta span {
    color: var(--preview-meta-text);
    font-size: 11px;
  }

  .attachment-preview-dialog-actions,
  .attachment-preview-dialog-zoom-controls {
    display: inline-flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 8px;
  }

  .attachment-preview-dialog-zoom-controls {
    padding-right: 2px;
  }

  .attachment-preview-dialog-zoom-level {
    min-width: 44px;
    color: var(--preview-zoom-text);
    font-size: 11px;
    font-weight: 620;
    line-height: 1;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }

  .attachment-preview-dialog-frame {
    display: flex;
    min-width: 0;
    min-height: 0;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: var(--preview-frame-background);
  }

  .attachment-preview-dialog-frame.is-image {
    cursor: zoom-in;
    touch-action: none;
  }

  .attachment-preview-dialog-frame.is-zoomed {
    cursor: grab;
  }

  .attachment-preview-dialog-frame.is-dragging {
    cursor: grabbing;
  }

  .attachment-preview-dialog-media {
    display: block;
    width: auto;
    height: auto;
    max-width: calc(100% - 24px);
    max-height: calc(100% - 24px);
    object-fit: contain;
  }

  img.attachment-preview-dialog-media {
    transform: translate3d(var(--preview-image-pan-x, 0), var(--preview-image-pan-y, 0), 0)
      scale(var(--preview-image-scale, 1));
    transform-origin: center;
    user-select: none;
    transition: transform 120ms ease;
    will-change: transform;
  }

  .attachment-preview-dialog-frame.is-dragging img.attachment-preview-dialog-media {
    transition: none;
  }

  video.attachment-preview-dialog-media {
    width: min(100%, 1180px);
    background: #000;
  }

  .attachment-preview-dialog-frame iframe {
    display: block;
    width: 100%;
    height: 100%;
    min-height: 420px;
    border: 0;
    background: #fff;
  }

  .attachment-preview-dialog-fallback {
    display: grid;
    place-items: center;
    gap: 10px;
    min-width: min(420px, 100%);
    padding: 42px 28px;
    color: var(--preview-fallback-text);
    text-align: center;
  }

  .attachment-preview-dialog-fallback-icon {
    display: inline-flex;
    width: 58px;
    height: 58px;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--preview-action-border);
    border-radius: 18px;
    background: var(--preview-action-background);
    color: var(--preview-action-text);
  }

  .attachment-preview-dialog-fallback strong {
    max-width: 100%;
    overflow: hidden;
    color: var(--preview-meta-title);
    font-size: 14px;
    font-weight: 640;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attachment-preview-dialog-fallback span {
    max-width: 280px;
    color: var(--preview-fallback-text);
    font-size: 12px;
    line-height: 1.45;
  }

  .attachment-preview-dialog-open {
    margin-top: 4px;
    padding: 8px 12px;
    border: 1px solid var(--preview-action-border);
    border-radius: 10px;
    background: var(--preview-action-background);
    color: var(--preview-action-text);
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    font-weight: 620;
  }

  .attachment-preview-dialog-open:hover {
    background: var(--preview-action-background-hover);
  }

  @media (max-width: 720px) {
    .attachment-preview-dialog-layer {
      padding: 8px;
    }

    .attachment-preview-dialog-panel {
      width: 100%;
      height: 100%;
    }

    .attachment-preview-dialog-toolbar {
      grid-template-columns: minmax(0, 1fr);
      gap: 8px;
      padding: 8px;
    }

    .attachment-preview-dialog-actions {
      justify-content: flex-end;
    }

    .attachment-preview-dialog-zoom-controls {
      gap: 6px;
    }

    .attachment-preview-dialog-zoom-level {
      min-width: 38px;
    }
  }
</style>
