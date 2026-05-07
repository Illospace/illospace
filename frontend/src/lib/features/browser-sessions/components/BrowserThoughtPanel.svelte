<script lang="ts">
  import {
    ConstellationAddressField,
    ConstellationIcon,
    ConstellationIconButton,
  } from '$lib/components/constellation';
  import { cortex } from '$lib/stores/cortex.svelte';

  const browserUrl = $derived.by(() => cortex.browserSession?.current_url || '');
  const browserUrlLabel = $derived.by(() => {
    const value = browserUrl;
    if (!value) return '';
    try {
      const parsed = new URL(value);
      return parsed.host || parsed.origin || value;
    } catch {
      return value.replace(/^https?:\/\//, '') || '';
    }
  });
  const browserPageLabel = $derived.by(() => cortex.browserSession?.page_title || browserUrlLabel || 'Preview');
  const hasBrowserFrame = $derived.by(() => Boolean(cortex.browserFrame?.image_url));
  const browserStreamFailed = $derived.by(
    () => cortex.browserSession?.status === 'error' || Boolean(cortex.browserSession?.last_error),
  );
  const browserFieldStatus = $derived.by(() => {
    if (cortex.browserSession?.last_error || cortex.browserSession?.status === 'error') return 'error';
    if (cortex.browserFrame?.image_url) return 'connected';
    return 'loading';
  });
  const browserControlsDisabled = $derived.by(() => !cortex.browserSession?.id);

  let browserUrlDraft = $state('https://');
  let lastSyncedBrowserUrl = $state('');
  let viewportClientWidth = $state(0);
  let viewportClientHeight = $state(0);
  let lastViewportWheelAt = 0;

  const viewportKeyMap: Record<string, string> = {
    Backspace: 'Backspace',
    Delete: 'Delete',
    Enter: 'Enter',
    Escape: 'Escape',
    Tab: 'Tab',
    ArrowUp: 'ArrowUp',
    ArrowDown: 'ArrowDown',
    ArrowLeft: 'ArrowLeft',
    ArrowRight: 'ArrowRight',
    Home: 'Home',
    End: 'End',
    PageUp: 'PageUp',
    PageDown: 'PageDown',
    ' ': 'Space',
  };

  $effect(() => {
    if (browserUrl && browserUrl !== lastSyncedBrowserUrl) {
      browserUrlDraft = browserUrl;
      lastSyncedBrowserUrl = browserUrl;
    }
    if (!browserUrl && !cortex.browserSession && !lastSyncedBrowserUrl) {
      browserUrlDraft ||= 'https://';
    }
  });

  function normalizeBrowserTarget(value: string): string {
    const target = (value || '').trim();
    if (!target || target === 'https://') return '';
    if (/^https?:\/\//i.test(target)) return target;
    if (/^[a-z][a-z0-9+.-]*:/i.test(target)) return target;
    if (!/\s/.test(target) && /[.:]/.test(target)) return `https://${target}`;
    return `https://www.google.com/search?q=${encodeURIComponent(target)}`;
  }

  async function submitAddress() {
    const target = normalizeBrowserTarget(browserUrlDraft);
    if (!target) return;
    browserUrlDraft = target;
    lastSyncedBrowserUrl = target;
    if (cortex.browserSession?.id) {
      cortex.browserNavigate(target);
      return;
    }
    await cortex.ensureBrowserSession(target);
  }

  function handleAddressKeydown(event: KeyboardEvent) {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    void submitAddress();
  }

  function openInBrowser(url: string | undefined) {
    const target = normalizeBrowserTarget(url || browserUrlDraft);
    if (!target || typeof window === 'undefined') return;
    window.open(target, '_blank', 'noopener,noreferrer');
  }

  function finiteNumber(value: unknown): value is number {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function getViewportMetrics(width = viewportClientWidth, height = viewportClientHeight) {
    const frame = cortex.browserFrame;
    if (!frame || width <= 0 || height <= 0 || frame.width <= 0 || frame.height <= 0) return null;
    const frameAspect = frame.width / frame.height;
    const containerAspect = width / height;
    if (containerAspect > frameAspect) {
      const renderedHeight = height;
      const renderedWidth = renderedHeight * frameAspect;
      return {
        offsetX: (width - renderedWidth) / 2,
        offsetY: 0,
        width: renderedWidth,
        height: renderedHeight,
      };
    }
    const renderedWidth = width;
    const renderedHeight = renderedWidth / frameAspect;
    return {
      offsetX: 0,
      offsetY: (height - renderedHeight) / 2,
      width: renderedWidth,
      height: renderedHeight,
    };
  }

  function framePixel(value: number | null | undefined, total: number, renderedTotal: number): number | null {
    if (!finiteNumber(value) || total <= 0 || renderedTotal <= 0) return null;
    return (value / total) * renderedTotal;
  }

  function buildBrowserFocusStyle() {
    const frame = cortex.browserFrame;
    const focus = frame?.focus;
    if (!frame || !focus) return '';
    const metrics = getViewportMetrics();
    if (!metrics) return '';
    const focusX = framePixel(focus.x, frame.width, metrics.width);
    const focusY = framePixel(focus.y, frame.height, metrics.height);
    const width = framePixel(focus.width, frame.width, metrics.width);
    const height = framePixel(focus.height, frame.height, metrics.height);
    const left = focusX == null ? null : metrics.offsetX + focusX;
    const top = focusY == null ? null : metrics.offsetY + focusY;
    if (left == null || top == null || width == null || height == null || width <= 0 || height <= 0) return '';
    return `left: ${left}px; top: ${top}px; width: ${width}px; height: ${height}px;`;
  }

  function buildBrowserCaretStyle() {
    const frame = cortex.browserFrame;
    const focus = frame?.focus;
    if (!frame || !focus?.editable) return '';
    const metrics = getViewportMetrics();
    if (!metrics) return '';
    const caretX = framePixel(focus.caret_x, frame.width, metrics.width);
    const caretY = framePixel(focus.caret_y, frame.height, metrics.height);
    const height = framePixel(focus.caret_height || 18, frame.height, metrics.height);
    const left = caretX == null ? null : metrics.offsetX + caretX;
    const top = caretY == null ? null : metrics.offsetY + caretY;
    if (left == null || top == null || height == null || height <= 0) return '';
    return `left: ${left}px; top: ${top}px; height: ${height}px;`;
  }

  const browserFocusStyle = $derived.by(() => buildBrowserFocusStyle());
  const browserCaretStyle = $derived.by(() => buildBrowserCaretStyle());

  function handleViewportClick(event: MouseEvent) {
    const session = cortex.browserSession;
    const frame = cortex.browserFrame;
    if (!session || !frame) return;
    const target = event.currentTarget as HTMLElement;
    target.focus();
    const rect = target.getBoundingClientRect();
    const metrics = getViewportMetrics(rect.width, rect.height);
    if (!metrics) return;
    const localX = event.clientX - rect.left - metrics.offsetX;
    const localY = event.clientY - rect.top - metrics.offsetY;
    if (localX < 0 || localY < 0 || localX > metrics.width || localY > metrics.height) return;
    const x = (localX / metrics.width) * session.viewport_width;
    const y = (localY / metrics.height) * session.viewport_height;
    cortex.browserClick(Math.round(x), Math.round(y));
  }

  function handleViewportWheel(event: WheelEvent) {
    if (!cortex.browserSession || !cortex.browserFrame) return;
    event.preventDefault();
    const now = Date.now();
    if (now - lastViewportWheelAt < 45) return;
    lastViewportWheelAt = now;
    cortex.browserScroll(event.deltaX, event.deltaY);
  }

  function handleViewportKeydown(event: KeyboardEvent) {
    if (!cortex.browserSession || !cortex.browserFrame) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    const mappedKey = event.shiftKey && event.key === 'Tab'
      ? 'Shift+Tab'
      : viewportKeyMap[event.key];

    if (mappedKey) {
      event.preventDefault();
      cortex.browserKey(mappedKey);
      return;
    }

    if (event.key.length === 1) {
      event.preventDefault();
      cortex.browserType(event.key, false);
    }
  }

  function handleViewportPaste(event: ClipboardEvent) {
    if (!cortex.browserSession || !cortex.browserFrame) return;
    const text = event.clipboardData?.getData('text/plain') || event.clipboardData?.getData('text') || '';
    if (!text) return;
    event.preventDefault();
    cortex.browserType(text, false);
  }
</script>

<div class="browser-thought-panel">
  <section class="browser-shell">
    <div class="browser-window-shell">
      <div class="browser-nav-bar">
        <div class="nav-controls">
          <ConstellationIconButton
            label="Back"
            title="Back"
            onclick={() => cortex.browserBack()}
            disabled={browserControlsDisabled}
          >
            <ConstellationIcon name="back" size={14} stroke={1.8} />
          </ConstellationIconButton>
          <ConstellationIconButton
            label="Forward"
            title="Forward"
            onclick={() => cortex.browserForward()}
            disabled={browserControlsDisabled}
          >
            <ConstellationIcon name="forward" size={14} stroke={1.8} />
          </ConstellationIconButton>
          <ConstellationIconButton
            label="Refresh"
            title="Refresh"
            onclick={() => cortex.browserRefresh()}
            disabled={browserControlsDisabled}
          >
            <ConstellationIcon name="refresh" size={14} stroke={1.8} />
          </ConstellationIconButton>
        </div>

        <ConstellationAddressField
          bind:value={browserUrlDraft}
          status={browserFieldStatus}
          className="browser-omnibox-field"
          placeholder="Search or enter URL"
          aria-label="Browser address"
          onkeydown={handleAddressKeydown}
        />

        <div class="nav-meta">
          <ConstellationIconButton
            label="Go"
            onclick={() => void submitAddress()}
            title="Go"
            disabled={!normalizeBrowserTarget(browserUrlDraft)}
          >
            <ConstellationIcon name="send" size={14} stroke={1.8} />
          </ConstellationIconButton>
          <ConstellationIconButton
            label="Open in browser"
            onclick={() => openInBrowser(browserUrl || browserUrlDraft)}
            title="Open in browser"
            disabled={!normalizeBrowserTarget(browserUrl || browserUrlDraft)}
          >
            <ConstellationIcon name="external-link" size={14} stroke={1.8} />
          </ConstellationIconButton>
        </div>
      </div>

      <div class="browser-content">
        <div class="browser-stage" class:is-empty={!browserUrl || !hasBrowserFrame}>
          <div class="browser-viewport-wrap">
            {#if browserUrl && cortex.browserFrame?.image_url}
              <button
                type="button"
                class="viewport-button"
                bind:clientWidth={viewportClientWidth}
                bind:clientHeight={viewportClientHeight}
                onclick={handleViewportClick}
                onkeydown={handleViewportKeydown}
                onpaste={handleViewportPaste}
                onwheel={handleViewportWheel}
                title="Click inside the streamed browser"
                aria-label="Live browser viewport"
              >
                <img
                  class="browser-viewport"
                  src={cortex.browserFrame.image_url}
                  alt={`Streamed browser view for ${browserPageLabel}`}
                />
                {#if browserFocusStyle}
                  <span class="viewport-focus-box" style={browserFocusStyle} aria-hidden="true"></span>
                {/if}
                {#if browserCaretStyle}
                  <span class="viewport-caret" style={browserCaretStyle} aria-hidden="true"></span>
                {/if}
                {#if browserStreamFailed}
                  <span class="viewport-stream-error" aria-hidden="true">
                    <span>Stream paused</span>
                  </span>
                {/if}
              </button>
            {:else}
              <div class="browser-placeholder" class:is-error={browserStreamFailed}>
                {#if browserStreamFailed}
                  <span>Browser unavailable</span>
                  {#if cortex.browserSession?.last_error}
                    <small>{cortex.browserSession.last_error}</small>
                  {/if}
                {:else if browserUrl}
                  <span>Waiting for browser frame</span>
                  <small>{browserUrlLabel || browserUrl}</small>
                {:else}
                  <span>Ready for a URL</span>
                {/if}
              </div>
            {/if}
          </div>
        </div>
      </div>
    </div>
  </section>
</div>

<style>
  .browser-thought-panel {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: flex;
    background: transparent;
    border: 0;
    box-shadow: none;
    overflow: visible;
    color: #eef4fb;
  }

  .browser-shell {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 0;
    padding: 0;
    min-height: 0;
    height: 100%;
  }

  .browser-window-shell {
    width: 100%;
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-height: 0;
    border-radius: 8px;
    background:
      linear-gradient(180deg, rgba(12, 14, 18, 0.98), rgba(9, 11, 15, 0.94)),
      rgba(5, 9, 16, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.08);
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.32);
    overflow: hidden;
  }

  .browser-nav-bar,
  .browser-stage {
    width: 100%;
    min-width: 0;
  }

  .browser-nav-bar {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 8px;
    align-items: center;
    min-height: 42px;
    padding: 6px 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    background: rgba(255, 255, 255, 0.02);
  }

  .browser-content {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    overflow: hidden;
    padding: 8px;
  }

  .browser-content::-webkit-scrollbar {
    width: 6px;
  }

  .browser-content::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.12);
    border-radius: 999px;
  }

  .nav-controls {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  :global(.browser-omnibox-field) {
    min-width: 0;
  }

  :global(.browser-omnibox-field.constellation-text-input) {
    min-height: 30px;
    padding: 0 10px;
  }

  :global(.browser-omnibox-field.constellation-text-input.is-mono .constellation-text-input-control),
  :global(.browser-omnibox-field .constellation-text-input-control) {
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 12px;
    letter-spacing: 0;
    text-transform: none;
  }

  .nav-meta {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    gap: 4px;
    flex-wrap: nowrap;
  }

  .browser-stage {
    flex: 1 1 auto;
    min-height: 220px;
    margin: 0;
    border-radius: 7px;
    overflow: hidden;
    background: rgba(4, 8, 13, 0.94);
    border: 1px solid rgba(255, 255, 255, 0.06);
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .browser-stage.is-empty {
    background: rgba(4, 8, 13, 0.72);
  }

  .browser-viewport-wrap {
    width: 100%;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    background: transparent;
    border: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .browser-viewport {
    width: 100%;
    height: 100%;
    object-fit: contain;
    display: block;
  }

  .viewport-button {
    position: relative;
    display: block;
    width: 100%;
    height: 100%;
    padding: 0;
    border: 0;
    background: transparent;
    cursor: default;
    overflow: hidden;
    outline: none;
    line-height: 0;
  }

  .viewport-button:focus-visible {
    box-shadow: inset 0 0 0 2px rgba(141, 183, 255, 0.62);
  }

  .viewport-focus-box,
  .viewport-caret,
  .viewport-stream-error {
    position: absolute;
    pointer-events: none;
    z-index: 2;
  }

  .viewport-focus-box {
    border: 2px solid rgba(48, 137, 255, 0.92);
    border-radius: 3px;
    box-shadow:
      0 0 0 1px rgba(255, 255, 255, 0.72),
      0 0 12px rgba(48, 137, 255, 0.28);
  }

  .viewport-caret {
    width: 2px;
    min-height: 14px;
    background: #111827;
    box-shadow:
      0 0 0 1px rgba(255, 255, 255, 0.74),
      0 0 8px rgba(48, 137, 255, 0.38);
    animation: browser-caret-blink 1s steps(2, start) infinite;
  }

  @keyframes browser-caret-blink {
    0%,
    45% {
      opacity: 1;
    }
    46%,
    100% {
      opacity: 0;
    }
  }

  .viewport-stream-error {
    inset: 12px;
    display: flex;
    align-items: flex-start;
    justify-content: flex-end;
  }

  .viewport-stream-error span {
    min-height: 24px;
    padding: 0 10px;
    border-radius: 999px;
    display: inline-flex;
    align-items: center;
    background: rgba(12, 16, 22, 0.86);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: rgba(247, 251, 255, 0.88);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .browser-placeholder {
    width: 100%;
    height: 100%;
    min-height: 0;
    display: grid;
    place-content: center;
    gap: 8px;
    padding: 16px;
    text-align: center;
    color: rgba(235, 242, 250, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    line-height: 1.45;
  }

  .browser-placeholder span {
    color: rgba(247, 251, 255, 0.72);
    font-size: 12px;
    line-height: 1.35;
  }

  .browser-placeholder small {
    display: block;
    max-width: min(280px, 100%);
    overflow-wrap: anywhere;
    color: rgba(235, 242, 250, 0.45);
    font-size: 10px;
    line-height: 1.45;
  }

  .browser-placeholder.is-error span {
    color: rgba(255, 214, 214, 0.82);
  }

  @media (max-width: 980px) {
    .browser-nav-bar {
      grid-template-columns: 1fr;
    }

    .nav-meta {
      justify-content: flex-start;
    }
  }
</style>
