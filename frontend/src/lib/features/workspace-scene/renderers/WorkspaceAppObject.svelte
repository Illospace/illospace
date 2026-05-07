<script lang="ts">
  import { onMount } from 'svelte';

  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import {
    inlineThumbnailSource,
    numberFrom,
    structuredThumbnailSpec as deriveStructuredThumbnailSpec,
  } from '$lib/utils/generatedWorkspaceAppContract';

  type AppPreview = {
    eyebrow: string;
    primary: string | null;
    primaryLabel: string;
    secondary: string;
    progress: number;
  };

  type SemanticZoomLevel = 'detail' | 'summary' | 'symbol' | 'glyph';

  let {
    appId = '',
    name,
    description = '',
    rendererKey = '',
    visualSpec = {},
    stateKey = 'default',
    accent = 'var(--positive)',
    style = '',
    active = false,
    semanticLevel = 'detail',
    activate,
    beginDrag,
    moveDrag,
    endDrag,
  }: {
    appId?: string;
    name: string;
    description?: string | null;
    rendererKey?: string;
    visualSpec?: Record<string, any>;
    stateKey?: string;
    accent?: string;
    style?: string;
    active?: boolean;
    semanticLevel?: SemanticZoomLevel;
    activate?: (event: MouseEvent) => void;
    beginDrag?: (event: PointerEvent) => boolean | void;
    moveDrag?: (event: PointerEvent) => boolean | void;
    endDrag?: (event: PointerEvent) => boolean | void;
  } = $props();

  let activePointerId: number | null = null;
  let pointerMoved = false;
  let suppressNextActivate = false;

  const cachedState = $derived(workspaceApps.cachedState(appId, stateKey));
  const preview = $derived(buildPreview());
  const progressDegrees = $derived(Math.round(Math.max(0, Math.min(100, preview.progress)) * 3.6));
  const thumbnailSpec = $derived(deriveStructuredThumbnailSpec(visualSpec, name, previewSpec()));
  const thumbnailSource = $derived(inlineThumbnailSource(visualSpec));
  const thumbnailSrcdoc = $derived(thumbnailSource ? buildThumbnailSrcdoc(thumbnailSource) : '');
  const appInitial = $derived(String(name || 'A').trim().slice(0, 1).toUpperCase() || 'A');
  const compactMetric = $derived(compactMetricLabel(preview.primary));
  const hasCompactMetric = $derived(compactMetric !== null);
  const buttonLabel = $derived(hasCompactMetric
    ? `Open ${name}: ${preview.primary} ${preview.primaryLabel}, ${preview.secondary}`
    : `Open ${name}: ${preview.secondary}`,
  );

  function previewSpec() {
    return (visualSpec?.preview ?? {}) as Record<string, any>;
  }

  function jsonForScript(value: unknown) {
    return JSON.stringify(value).replace(/</g, '\\u003c');
  }

  function buildThumbnailSrcdoc(source: string) {
    return `<!doctype html>
      <html>
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <style>
            * { box-sizing: border-box; }
            html, body {
              width: 100%;
              height: 100%;
              margin: 0;
              overflow: hidden;
              color: rgba(246, 248, 252, 0.94);
              background: transparent;
              font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            }
            body { display: grid; place-items: stretch; }
            img, svg, canvas, video { max-width: 100%; max-height: 100%; }
          </style>
          <script>
            window.__ILLO_THUMBNAIL__ = ${jsonForScript({
              app: { id: appId, name, description, rendererKey },
              state: cachedState || {},
              visualSpec: visualSpec || {},
            })};
          <\/script>
        </head>
        <body>${source}</body>
      </html>`;
  }

  function collectionCount(value: unknown) {
    if (Array.isArray(value)) return value.length;
    if (value && typeof value === 'object') return Object.keys(value).length;
    return null;
  }

  function stateCount(): number | null {
    const direct =
      cachedState?.count
      ?? cachedState?.total
      ?? cachedState?.open
      ?? cachedState?.items
      ?? cachedState?.records
      ?? cachedState?.tasks;
    const collection = collectionCount(direct);
    if (collection !== null) return collection;
    const numeric = Number(direct);
    return Number.isFinite(numeric) ? numeric : null;
  }

  function firstDefinedValue(...values: unknown[]) {
    return values.find((value) => value !== undefined && value !== null && value !== '');
  }

  function compactNumber(value: number) {
    const sign = value < 0 ? '-' : '';
    const abs = Math.abs(value);
    const trim = (next: string) => next.replace(/\.0(?=[KMGT]?$)/, '');
    if (abs < 1000) return `${Math.round(value)}`;
    if (abs < 10_000) return `${sign}${trim((abs / 1000).toFixed(1))}K`;
    if (abs < 1_000_000) return `${sign}${Math.round(abs / 1000)}K`;
    if (abs < 10_000_000) return `${sign}${trim((abs / 1_000_000).toFixed(1))}M`;
    if (abs < 1_000_000_000) return `${sign}${Math.round(abs / 1_000_000)}M`;
    if (abs < 10_000_000_000) return `${sign}${trim((abs / 1_000_000_000).toFixed(1))}B`;
    return `${sign}${Math.round(abs / 1_000_000_000)}B`;
  }

  function compactMetricLabel(value: unknown): string | null {
    if (value === undefined || value === null || value === '') return null;
    const numeric = typeof value === 'number'
      ? value
      : typeof value === 'string' && value.trim() !== ''
        ? Number(value)
        : Number.NaN;
    if (Number.isFinite(numeric)) return compactNumber(numeric);

    const text = String(value).replace(/\s+/g, ' ').trim();
    if (!text || text.length > 5) return null;
    return text.toUpperCase();
  }

  function genericPreview(): AppPreview {
    const spec = previewSpec();
    const thumbnail = deriveStructuredThumbnailSpec(visualSpec, name, spec);
    if (thumbnail) {
      const thumbnailPrimary = firstDefinedValue(thumbnail.value, thumbnail.status);
      return {
        eyebrow: thumbnail.label,
        primary: thumbnailPrimary === undefined ? null : String(thumbnailPrimary),
        primaryLabel: thumbnail.unit || 'status',
        secondary: thumbnail.secondary || 'live surface',
        progress: thumbnail.progress,
      };
    }
    const value = firstDefinedValue(spec.primary_value, spec.count, stateCount());
    const progress = numberFrom(spec.progress, 42);

    return {
      eyebrow: String(spec.title || name || 'App'),
      primary: value === undefined ? null : String(value),
      primaryLabel: value === undefined ? '' : String(spec.primary_unit || 'items'),
      secondary: String(spec.secondary || 'live surface'),
      progress,
    };
  }

  function buildPreview(): AppPreview {
    if (thumbnailSource) {
      return {
        eyebrow: String(previewSpec().title || name || 'App'),
        primary: null,
        primaryLabel: 'generated thumbnail',
        secondary: 'live app',
        progress: 0,
      };
    }
    return genericPreview();
  }

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

  onMount(() => {
    void workspaceApps.loadStateQueued(appId, stateKey, { silent: true, delayMs: 700 });
  });
</script>

<button
  type="button"
  class={`cortex-workspace-app-object cortex-workspace-app-object--semantic-${semanticLevel}`}
  class:is-active={active}
  class:cortex-workspace-app-object--no-metric={!hasCompactMetric}
  style={`--workspace-app-accent:${accent};${style}`}
  aria-label={buttonLabel}
  title={description || `Open ${name}`}
  onclick={handleActivate}
  onpointerdown={handlePointerDown}
  onpointermove={handlePointerMove}
  onpointerup={handlePointerUp}
  onpointercancel={handlePointerCancel}
>
  <span class="cortex-workspace-app-object__halo" aria-hidden="true"></span>
  <span
    class="cortex-workspace-app-object__body"
    style={`--preview-progress:${progressDegrees}deg`}
    aria-hidden="true"
  >
    {#if thumbnailSpec}
      <span class="cortex-workspace-app-object__thumbnail" aria-hidden="true">
        <span class="cortex-workspace-app-object__topline">
          <span class="cortex-workspace-app-object__status"></span>
          <span class="cortex-workspace-app-object__eyebrow">{thumbnailSpec.label}</span>
        </span>
        {#if hasCompactMetric}
          <span class="cortex-workspace-app-object__metric">
            <span class="cortex-workspace-app-object__metric-value">{compactMetric}</span>
          </span>
        {/if}
        {#if thumbnailSpec.unit || thumbnailSpec.secondary}
          <span class="cortex-workspace-app-object__thumbnail-caption">
            {thumbnailSpec.unit || thumbnailSpec.secondary}
          </span>
        {/if}
      </span>
    {:else if thumbnailSrcdoc}
      <iframe
        class="cortex-workspace-app-object__thumbnail-frame"
        title={`${name} thumbnail`}
        sandbox="allow-scripts"
        srcdoc={thumbnailSrcdoc}
        tabindex="-1"
      ></iframe>
    {:else}
      <span class="cortex-workspace-app-object__topline">
        <span class="cortex-workspace-app-object__status"></span>
        <span class="cortex-workspace-app-object__eyebrow">{preview.eyebrow}</span>
      </span>

      {#if hasCompactMetric}
        <span class="cortex-workspace-app-object__metric">
          <span class="cortex-workspace-app-object__metric-value">{compactMetric}</span>
        </span>
      {/if}
    {/if}
    <span class="cortex-workspace-app-object__glyph-mark">{appInitial}</span>
  </span>
</button>

<style>
.cortex-workspace-app-object {
  --workspace-app-accent: var(--positive);
  --preview-progress: 90deg;
  --workspace-app-shell: var(--constellation-thread-message-user-shell-base);
  --workspace-app-text: var(--constellation-color-text-primary);
  --workspace-app-muted: var(--constellation-color-text-secondary);
  --workspace-app-subtle: var(--constellation-color-text-muted);
  --workspace-app-hover-shadow: var(--constellation-surface-panel-hover-shadow);
  --workspace-app-counter-scale: 1;
  --workspace-app-rotate-x: 5deg;
  --workspace-app-rotate-y: -7deg;
  --workspace-app-lift: 0px;
  --workspace-app-state-scale: 1;
  position: absolute;
  left: 0;
  top: 0;
  width: 88px;
  min-height: 76px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--workspace-app-text);
  cursor: grab;
  pointer-events: auto;
  touch-action: none;
  transform: translate(-50%, -50%);
  transform-origin: center;
  z-index: var(--workspace-app-z, 4);
  opacity: var(--workspace-app-opacity, 1);
  animation: cortex-workspace-app-idle-float var(--workspace-app-float-duration, 15.5s) ease-in-out infinite;
  animation-delay: var(--workspace-app-float-delay, -4s);
  transition:
  opacity 180ms ease,
  filter 180ms ease,
  z-index 0ms linear 0ms;
}

.cortex-workspace-app-object__halo {
  position: absolute;
  left: 50%;
  top: 50%;
  width: 72px;
  height: 58px;
  border-radius: 42% 58% 47% 53%;
  background:
  radial-gradient(circle at 50% 42%, color-mix(in srgb, var(--workspace-app-accent) 30%, transparent), transparent 68%);
  filter: blur(16px);
  opacity: 0.5;
  transform: translate(-50%, -50%);
  transition:
  opacity 180ms ease,
  transform 220ms cubic-bezier(0.22, 1, 0.36, 1);
}

.cortex-workspace-app-object__body {
  position: relative;
  display: grid;
  width: 72px;
  min-height: 62px;
  align-content: center;
  gap: 6px;
  padding: 7px 8px;
  border-radius: 13px;
  background:
  var(--constellation-surface-panel-highlight),
  color-mix(in srgb, var(--workspace-app-accent) 10%, var(--workspace-app-shell));
  box-shadow:
  inset 0 0 0 1px color-mix(in srgb, var(--workspace-app-accent) 20%, var(--constellation-surface-panel-border)),
  var(--constellation-surface-nested-shadow),
  var(--constellation-surface-panel-shadow),
  0 0 18px color-mix(in srgb, var(--workspace-app-accent) 14%, transparent);
  overflow: hidden;
  transform-origin: center bottom;
  transform:
  scale(var(--workspace-app-counter-scale, 1))
  perspective(480px)
  rotateX(var(--workspace-app-rotate-x, 5deg))
  rotateY(var(--workspace-app-rotate-y, -7deg))
  translateY(var(--workspace-app-lift, 0px))
  scale(var(--workspace-app-state-scale, 1));
  transition:
  transform 220ms cubic-bezier(0.22, 1, 0.36, 1),
  box-shadow 180ms ease,
  width 190ms cubic-bezier(0.22, 1, 0.36, 1),
  min-height 190ms cubic-bezier(0.22, 1, 0.36, 1),
  padding 190ms cubic-bezier(0.22, 1, 0.36, 1),
  border-radius 190ms cubic-bezier(0.22, 1, 0.36, 1),
  gap 190ms cubic-bezier(0.22, 1, 0.36, 1);
}

.cortex-workspace-app-object__body::before {
  content: '';
  position: absolute;
  inset: 1px 14px auto;
  height: 1px;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--workspace-app-accent) 50%, white), transparent);
  opacity: 0.62;
}

.cortex-workspace-app-object__thumbnail-frame {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
  border-radius: inherit;
  background: transparent;
  pointer-events: none;
  opacity: 1;
  transition: opacity 140ms ease;
}

.cortex-workspace-app-object__glyph-mark {
  position: absolute;
  left: 50%;
  top: 50%;
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  margin: auto;
  border-radius: 999px;
  background:
  radial-gradient(circle at 50% 42%, color-mix(in srgb, var(--constellation-color-text-primary) 18%, transparent), transparent 54%),
  color-mix(in srgb, var(--workspace-app-accent) 28%, var(--workspace-app-shell));
  color: color-mix(in srgb, var(--workspace-app-accent) 72%, var(--constellation-color-text-primary));
  font-family: var(--constellation-font-mono);
  font-size: 14px;
  font-weight: 800;
  line-height: 1;
  opacity: 0;
  transform: translate(-50%, -50%) scale(0.72);
  transition:
  opacity 140ms ease,
  transform 190ms cubic-bezier(0.22, 1, 0.36, 1);
  box-shadow:
  inset 0 0 0 1px color-mix(in srgb, var(--workspace-app-accent) 22%, var(--constellation-surface-panel-border)),
  0 0 14px color-mix(in srgb, var(--workspace-app-accent) 22%, transparent);
}

.cortex-workspace-app-object__topline,
.cortex-workspace-app-object__metric {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 0;
  opacity: 1;
  transform: scale(1);
  transition:
  opacity 140ms ease,
  transform 190ms cubic-bezier(0.22, 1, 0.36, 1);
}

.cortex-workspace-app-object__thumbnail {
  position: relative;
  display: grid;
  min-width: 0;
  gap: 5px;
  place-items: center;
}

.cortex-workspace-app-object__topline {
  gap: 4px;
  justify-content: center;
}

.cortex-workspace-app-object__status {
  width: 5px;
  height: 5px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: color-mix(in srgb, var(--workspace-app-accent) 70%, white);
  box-shadow: 0 0 10px color-mix(in srgb, var(--workspace-app-accent) 46%, transparent);
}

.cortex-workspace-app-object__eyebrow {
  font-family: var(--constellation-font-mono);
  font-size: 7px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.cortex-workspace-app-object__eyebrow {
  overflow: hidden;
  flex: 0 1 auto;
  max-width: 52px;
  color: var(--workspace-app-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cortex-workspace-app-object__metric {
  justify-content: center;
  min-height: 28px;
}

.cortex-workspace-app-object__metric-value {
  display: block;
  max-width: 54px;
  overflow: hidden;
  color: var(--workspace-app-text);
  font-family: var(--constellation-font-mono);
  font-size: 18px;
  font-variant-numeric: tabular-nums;
  font-weight: 760;
  letter-spacing: 0;
  line-height: 1;
  text-overflow: ellipsis;
  text-shadow: 0 0 12px color-mix(in srgb, var(--workspace-app-accent) 22%, transparent);
  white-space: nowrap;
}

.cortex-workspace-app-object__thumbnail-caption {
  overflow: hidden;
  max-width: 52px;
  color: var(--workspace-app-subtle);
  font-family: var(--constellation-font-mono);
  font-size: 6.5px;
  font-weight: 650;
  letter-spacing: 0.08em;
  text-overflow: ellipsis;
  text-transform: uppercase;
  white-space: nowrap;
}

.cortex-workspace-app-object:hover,
.cortex-workspace-app-object:focus-visible,
.cortex-workspace-app-object.is-active {
  filter: saturate(1.08);
}

.cortex-workspace-app-object:hover,
.cortex-workspace-app-object:focus-visible {
  z-index: 14;
}

.cortex-workspace-app-object:active {
  cursor: grabbing;
}

.cortex-workspace-app-object.is-active {
  z-index: 12;
}

.cortex-workspace-app-object:hover .cortex-workspace-app-object__body,
.cortex-workspace-app-object:focus-visible .cortex-workspace-app-object__body {
  --workspace-app-rotate-x: 1deg;
  --workspace-app-rotate-y: -2deg;
  --workspace-app-lift: -7px;
  --workspace-app-state-scale: 1.28;
  box-shadow:
  inset 0 0 0 1px color-mix(in srgb, var(--workspace-app-accent) 32%, var(--constellation-surface-panel-hover-border)),
  var(--workspace-app-hover-shadow),
  0 0 44px color-mix(in srgb, var(--workspace-app-accent) 26%, transparent);
}

.cortex-workspace-app-object.is-active .cortex-workspace-app-object__body {
  --workspace-app-rotate-x: 2deg;
  --workspace-app-rotate-y: -3deg;
  --workspace-app-lift: -4px;
  --workspace-app-state-scale: 1.1;
  box-shadow:
  inset 0 0 0 1px color-mix(in srgb, var(--workspace-app-accent) 30%, var(--constellation-surface-panel-hover-border)),
  var(--workspace-app-hover-shadow),
  0 0 38px color-mix(in srgb, var(--workspace-app-accent) 24%, transparent);
}

.cortex-workspace-app-object:hover .cortex-workspace-app-object__halo,
.cortex-workspace-app-object:focus-visible .cortex-workspace-app-object__halo {
  opacity: 0.74;
  transform: translate(-50%, -50%) scale(1.34);
}

.cortex-workspace-app-object.is-active .cortex-workspace-app-object__halo {
  opacity: 0.68;
  transform: translate(-50%, -50%) scale(1.18);
}

.cortex-workspace-app-object:focus-visible {
  outline: 2px solid var(--constellation-control-focus-ring);
  outline-offset: 6px;
  border-radius: 28px;
}

.cortex-workspace-app-object--semantic-summary .cortex-workspace-app-object__body {
  width: 68px;
  min-height: 58px;
  gap: 4px;
  padding: 6px 7px;
}

.cortex-workspace-app-object--semantic-summary .cortex-workspace-app-object__metric {
  min-height: 23px;
}

.cortex-workspace-app-object--semantic-summary .cortex-workspace-app-object__metric-value {
  max-width: 48px;
  font-size: 15px;
}

.cortex-workspace-app-object--semantic-summary .cortex-workspace-app-object__thumbnail-caption {
  display: none;
}

.cortex-workspace-app-object--semantic-symbol .cortex-workspace-app-object__body,
.cortex-workspace-app-object--semantic-glyph .cortex-workspace-app-object__body {
  width: 56px;
  min-height: 52px;
  align-content: center;
  padding: 0;
  border-radius: 999px;
  clip-path: none;
}

.cortex-workspace-app-object--semantic-symbol .cortex-workspace-app-object__topline,
.cortex-workspace-app-object--semantic-symbol .cortex-workspace-app-object__metric,
.cortex-workspace-app-object--semantic-symbol .cortex-workspace-app-object__thumbnail-caption,
.cortex-workspace-app-object--semantic-glyph .cortex-workspace-app-object__topline,
.cortex-workspace-app-object--semantic-glyph .cortex-workspace-app-object__metric,
.cortex-workspace-app-object--semantic-glyph .cortex-workspace-app-object__thumbnail-caption {
  opacity: 0;
  transform: scale(0.72);
}

.cortex-workspace-app-object--semantic-symbol .cortex-workspace-app-object__thumbnail-frame,
.cortex-workspace-app-object--semantic-glyph .cortex-workspace-app-object__thumbnail-frame {
  opacity: 0;
}

.cortex-workspace-app-object--semantic-symbol .cortex-workspace-app-object__glyph-mark,
.cortex-workspace-app-object--semantic-glyph .cortex-workspace-app-object__glyph-mark,
.cortex-workspace-app-object--no-metric .cortex-workspace-app-object__glyph-mark {
  opacity: 1;
  transform: translate(-50%, -50%) scale(1);
}

.cortex-workspace-app-object--no-metric:not(.cortex-workspace-app-object--semantic-symbol):not(.cortex-workspace-app-object--semantic-glyph) .cortex-workspace-app-object__glyph-mark {
  top: calc(50% + 8px);
  opacity: 0.9;
  transform: translate(-50%, -50%) scale(0.82);
}

@keyframes cortex-workspace-app-idle-float {
  0%,
  100% {
    transform: translate(-50%, -50%) translate3d(0, 0, 0);
  }

  25% {
    transform: translate(-50%, -50%) translate3d(
    calc(var(--workspace-app-float-x, 4px) * 0.72),
    calc(var(--workspace-app-float-y, 3px) * -0.84),
    0
    );
  }

  50% {
    transform: translate(-50%, -50%) translate3d(
    calc(var(--workspace-app-float-x, 4px) * -0.42),
    calc(var(--workspace-app-float-y, 3px) * 0.58),
    0
    );
  }

  75% {
    transform: translate(-50%, -50%) translate3d(
    calc(var(--workspace-app-float-x, 4px) * -0.86),
    calc(var(--workspace-app-float-y, 3px) * -0.32),
    0
    );
  }
}

@media (prefers-reduced-motion: reduce) {
  .cortex-workspace-app-object {
    animation: none;
  }
}

</style>
