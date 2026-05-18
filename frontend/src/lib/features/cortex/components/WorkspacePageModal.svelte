<script lang="ts">
  import { setContext, type Component, type Snippet } from 'svelte';
  import {
    ConstellationGlyphIcon,
    ConstellationIcon,
    ConstellationIconButton,
    ConstellationSkeletonBlock,
  } from '$lib/components/constellation';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalRefreshAction,
    type ConstellationPageFrameModalContext,
  } from '$lib/components/constellation/constellationPageFrameContext';
  import type { WorkspacePageModalSection } from '$lib/features/cortex/domain/workspacePageModal';

  let {
    section,
    PageComponent = null,
    loading = false,
    onclose,
  }: {
    section: WorkspacePageModalSection;
    PageComponent?: Component | null;
    loading?: boolean;
    onclose?: () => void;
  } = $props();

  let pageActions = $state<Snippet | undefined>();
  let activeActionsToken: symbol | null = null;
  let refreshAction = $state<ConstellationPageFrameModalRefreshAction | undefined>();
  let activeRefreshToken: symbol | null = null;

  function registerPageFrameActions(actions: Snippet | undefined) {
    const token = Symbol('workspace-page-actions');
    activeActionsToken = token;
    pageActions = actions;

    return () => {
      if (activeActionsToken !== token) return;
      activeActionsToken = null;
      pageActions = undefined;
    };
  }

  function registerRefreshAction(action: ConstellationPageFrameModalRefreshAction | undefined) {
    const token = Symbol('workspace-page-refresh-action');
    activeRefreshToken = token;
    refreshAction = action;

    return () => {
      if (activeRefreshToken !== token) return;
      activeRefreshToken = null;
      refreshAction = undefined;
    };
  }

  setContext<ConstellationPageFrameModalContext>(CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT, {
    embedded: true,
    registerActions: registerPageFrameActions,
    registerRefreshAction,
  });
</script>

<div class="workspace-page-modal">
  <button
    type="button"
    class="workspace-page-modal__scrim"
    aria-label="Close page"
    onclick={() => onclose?.()}
  ></button>

  <div
    class="workspace-page-modal__surface"
    role="dialog"
    aria-modal="true"
    aria-labelledby="workspace-page-modal-title"
  >
    <header class="workspace-page-modal__header">
      <div class="workspace-page-modal__identity">
        <span class="workspace-page-modal__glyph" aria-hidden="true">
          <ConstellationGlyphIcon label={section.glyph} />
        </span>
        <div class="workspace-page-modal__copy">
          <h1 id="workspace-page-modal-title">{section.title}</h1>
          <span>{section.subtitle}</span>
        </div>
      </div>

      <div class="workspace-page-modal__actions">
        {#if pageActions}
          <div class="workspace-page-modal__page-actions">
            {@render pageActions()}
          </div>
        {/if}
        {#if refreshAction}
          <ConstellationIconButton
            label={refreshAction.label}
            title=""
            size="md"
            disabled={refreshAction.disabled}
            onclick={() => refreshAction?.onclick()}
          >
            <ConstellationIcon name="refresh" size={15} />
          </ConstellationIconButton>
        {/if}
        <ConstellationIconButton label="Close page" title="Close page" size="md" onclick={() => onclose?.()}>
          <ConstellationIcon name="close" size={16} />
        </ConstellationIconButton>
      </div>
    </header>

    <div class="workspace-page-modal__body">
      {#if PageComponent}
        <PageComponent />
      {:else if loading}
        <div class="workspace-page-modal__loading" aria-live="polite">
          <ConstellationSkeletonBlock height="34px" />
          <ConstellationSkeletonBlock height="180px" />
          <ConstellationSkeletonBlock height="280px" />
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .workspace-page-modal {
    --workspace-page-modal-edge-gap: clamp(14px, 2.5vw, 28px);
    --workspace-page-modal-top-gap: 68px;
    position: absolute;
    inset: 0;
    z-index: 92;
    display: grid;
    align-items: start;
    justify-items: center;
    box-sizing: border-box;
    padding: var(--workspace-page-modal-top-gap) var(--workspace-page-modal-edge-gap)
      var(--workspace-page-modal-edge-gap) max(92px, var(--workspace-page-modal-edge-gap));
  }

  .workspace-page-modal__scrim {
    position: absolute;
    inset: 0;
    border: 0;
    padding: 0;
    background: var(--workspace-page-modal-scrim, rgba(2, 5, 10, 0.54));
    cursor: default;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  :global(:root[data-color-scheme='light']) .workspace-page-modal__scrim {
    --workspace-page-modal-scrim: rgba(229, 236, 242, 0.54);
  }

  .workspace-page-modal__surface {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    width: min(1320px, 100%);
    height: calc(100svh - var(--workspace-page-modal-top-gap) - var(--workspace-page-modal-edge-gap));
    min-height: min(520px, calc(100svh - var(--workspace-page-modal-top-gap) - var(--workspace-page-modal-edge-gap)));
    overflow: hidden;
    border: 1px solid var(--constellation-surface-floating-border);
    border-radius: 22px;
    background: var(--constellation-surface-floating-background);
    box-shadow: var(--constellation-surface-floating-shadow);
  }

  .workspace-page-modal__header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
    padding: 16px 18px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-surface-floating-background) 92%, transparent);
  }

  .workspace-page-modal__identity {
    display: flex;
    flex: 1 1 280px;
    min-width: 0;
    align-items: center;
    gap: 12px;
  }

  .workspace-page-modal__glyph {
    display: inline-flex;
    width: 38px;
    height: 38px;
    flex: 0 0 auto;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--constellation-control-surface-border);
    border-radius: 12px;
    color: var(--constellation-color-text-primary);
    background: var(--constellation-control-button-secondary-background);
  }

  .workspace-page-modal__glyph :global(svg) {
    width: 17px;
    height: 17px;
  }

  .workspace-page-modal__copy {
    display: grid;
    min-width: 0;
    gap: 4px;
  }

  .workspace-page-modal__copy h1,
  .workspace-page-modal__copy span {
    margin: 0;
  }

  .workspace-page-modal__copy h1 {
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans, var(--font-sans));
    font-size: 17px;
    font-weight: 600;
    line-height: 1.25;
    letter-spacing: 0;
  }

  .workspace-page-modal__copy span {
    max-width: 660px;
    overflow: hidden;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .workspace-page-modal__actions {
    display: flex;
    min-width: 0;
    flex: 0 0 auto;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
  }

  .workspace-page-modal__page-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
  }

  .workspace-page-modal__page-actions :global(.constellation-button) {
    min-height: 34px;
    padding: 0 12px;
    border-radius: 8px;
    --button-background: var(--constellation-icon-button-quiet-background);
    --button-background-hover: var(--constellation-icon-button-quiet-background-hover);
    --button-border: var(--constellation-icon-button-quiet-border, var(--constellation-icon-button-border));
    --button-border-hover: var(--constellation-icon-button-quiet-border-hover);
    --button-text: var(--constellation-icon-button-text);
    --button-text-hover: var(--constellation-color-text-primary);
    --button-shadow: var(--constellation-icon-button-quiet-shadow);
    --button-shadow-hover: var(--button-shadow);
    box-shadow: var(--button-shadow);
  }

  .workspace-page-modal__page-actions :global(.constellation-button-primary),
  .workspace-page-modal__page-actions :global(.constellation-button-secondary),
  .workspace-page-modal__page-actions :global(.constellation-button-quiet) {
    --button-background: var(--constellation-icon-button-quiet-background);
    --button-background-hover: var(--constellation-icon-button-quiet-background-hover);
    --button-border: var(--constellation-icon-button-quiet-border, var(--constellation-icon-button-border));
    --button-border-hover: var(--constellation-icon-button-quiet-border-hover);
    --button-text: var(--constellation-icon-button-text);
    --button-text-hover: var(--constellation-color-text-primary);
    --button-shadow: var(--constellation-icon-button-quiet-shadow);
    --button-shadow-hover: var(--button-shadow);
  }

  .workspace-page-modal__body {
    min-height: 0;
    overflow: auto;
    padding: 22px;
    overscroll-behavior: contain;
  }

  .workspace-page-modal__loading {
    display: grid;
    gap: 18px;
  }

  :global(.workspace-page-modal__body .constellation-page-frame) {
    min-height: auto;
  }

  :global(.workspace-page-modal__body .constellation-page-frame-scene-glow),
  :global(.workspace-page-modal__body .constellation-page-frame-scene-warmth) {
    display: none;
  }

  :global(.workspace-page-modal__body .constellation-page-frame-shell) {
    width: 100%;
    max-width: none;
  }

  @media (max-width: 860px) {
    .workspace-page-modal {
      padding: 74px 10px 10px;
    }

    .workspace-page-modal__surface {
      height: calc(100svh - 84px);
      min-height: 0;
      border-radius: 18px;
    }

    .workspace-page-modal__header {
      align-items: flex-start;
      padding: 14px;
    }

    .workspace-page-modal__actions {
      width: 100%;
      align-items: flex-start;
      justify-content: flex-start;
    }

    .workspace-page-modal__page-actions {
      max-width: min(100%, 420px);
      justify-content: flex-start;
    }

    .workspace-page-modal__copy span {
      white-space: normal;
    }

    .workspace-page-modal__body {
      padding: 14px;
    }
  }
</style>
