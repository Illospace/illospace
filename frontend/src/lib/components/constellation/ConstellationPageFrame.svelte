<script lang="ts">
  import { getContext, type Snippet } from 'svelte';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalContext,
  } from './constellationPageFrameContext';

  type Props = {
    eyebrow?: string;
    title?: string;
    subtitle?: string;
    className?: string;
    headerClassName?: string;
    contentClassName?: string;
    actions?: Snippet;
    tabs?: Snippet;
    children?: Snippet;
  };

  let {
    eyebrow = '',
    title = '',
    subtitle = '',
    className = '',
    headerClassName = '',
    contentClassName = '',
    actions,
    tabs,
    children,
  }: Props = $props();

  const rootClass = $derived(['constellation-page-frame', className].filter(Boolean).join(' '));
  const headerClass = $derived(
    ['constellation-page-frame-header', headerClassName].filter(Boolean).join(' '),
  );
  const stackClass = $derived(
    ['constellation-page-frame-content-stack', contentClassName].filter(Boolean).join(' '),
  );
  const workspacePageModalContext = getContext<ConstellationPageFrameModalContext | undefined>(
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
  );
  const embeddedInWorkspacePageModal = workspacePageModalContext?.embedded === true;
  const showHeaderCopy = $derived(!embeddedInWorkspacePageModal && Boolean(eyebrow || title || subtitle));
  const showHeader = $derived(!embeddedInWorkspacePageModal && Boolean(showHeaderCopy || actions));

  $effect(() => {
    if (!embeddedInWorkspacePageModal) return;
    return workspacePageModalContext?.registerActions(actions);
  });
</script>

<section class={rootClass}>
  <div class="constellation-page-frame-scene-glow"></div>
  <div class="constellation-page-frame-scene-warmth"></div>

  <div class="constellation-page-frame-stage">
    <div class="constellation-page-frame-shell">
      {#if showHeader}
        <header class={headerClass}>
          <div class="constellation-page-frame-header-head">
            {#if showHeaderCopy}
              <div class="constellation-page-frame-header-copy">
                {#if eyebrow}
                  <p class="constellation-page-frame-header-eyebrow">{eyebrow}</p>
                {/if}
                {#if title}
                  <h1 class="constellation-page-frame-header-title">{title}</h1>
                {/if}
                {#if subtitle}
                  <p class="constellation-page-frame-header-subtitle">{subtitle}</p>
                {/if}
              </div>
            {/if}

            {#if actions}
              <div class="constellation-page-frame-header-actions">
                {@render actions()}
              </div>
            {/if}
          </div>
        </header>
      {/if}

      {#if tabs}
        <div class="constellation-page-frame-body">
          <div class="constellation-page-frame-tabs">
            {@render tabs()}
          </div>
          <div class={stackClass}>
            {#if children}
              {@render children()}
            {/if}
          </div>
        </div>
      {:else}
        <div class={stackClass}>
          {#if children}
            {@render children()}
          {/if}
        </div>
      {/if}
    </div>
  </div>
</section>

<style>
  .constellation-page-frame {
    position: relative;
    min-height: calc(100vh - (var(--page-padding, 24px) * 2));
    overflow: visible;
    isolation: isolate;
  }

  .constellation-page-frame-scene-glow,
  .constellation-page-frame-scene-warmth {
    position: absolute;
    inset: calc(var(--page-padding, 24px) * -1);
    z-index: 0;
    pointer-events: none;
  }

  .constellation-page-frame-scene-glow {
    background: var(
      --constellation-page-frame-theme-scene-glow,
      radial-gradient(circle at 34% 28%, rgba(141, 183, 255, 0.12), transparent 36%),
      radial-gradient(circle at 66% 68%, rgba(141, 183, 255, 0.08), transparent 42%)
    );
    filter: blur(36px);
    opacity: 0.78;
  }

  .constellation-page-frame-scene-warmth {
    background: var(
      --constellation-page-frame-theme-scene-warmth,
      radial-gradient(circle at 74% 78%, color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 12%, transparent), transparent 38%),
      radial-gradient(circle at 54% 52%, color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 4%, transparent), transparent 46%)
    );
    filter: blur(48px);
    opacity: 0.68;
  }

  .constellation-page-frame-stage {
    position: relative;
    z-index: 1;
    padding: 0;
    box-sizing: border-box;
  }

  .constellation-page-frame-shell {
    display: grid;
    gap: 26px;
    width: min(100%, 1240px);
    margin: 0 auto;
  }

  .constellation-page-frame-header {
    display: grid;
    gap: 10px;
    padding: 4px 0 10px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .constellation-page-frame-header-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
  }

  .constellation-page-frame-header-copy {
    display: grid;
    gap: 6px;
    max-width: 680px;
  }

  .constellation-page-frame-header-eyebrow {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-page-frame-header-title {
    margin: 0;
    max-width: 820px;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans, 'Inter', system-ui, sans-serif);
    font-size: 18px;
    font-weight: 560;
    line-height: 1.28;
    letter-spacing: 0;
  }

  .constellation-page-frame-header-subtitle {
    margin: 0;
    max-width: 620px;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.5;
  }

  .constellation-page-frame-header-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 10px;
  }

  .constellation-page-frame-content-stack {
    display: grid;
    gap: 22px;
    min-width: 0;
  }

  .constellation-page-frame-body {
    display: grid;
    gap: 18px;
    min-width: 0;
  }

  .constellation-page-frame-tabs {
    min-width: 0;
  }

  @media (max-width: 980px) {
    .constellation-page-frame-header-head {
      flex-direction: column;
    }

    .constellation-page-frame-header-actions {
      justify-content: flex-start;
    }
  }

  @media (max-width: 640px) {
    .constellation-page-frame-header-title {
      font-size: 16px;
    }
  }
</style>
