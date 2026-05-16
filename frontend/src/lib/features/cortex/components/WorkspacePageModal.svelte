<script lang="ts">
  import { setContext, type Component } from 'svelte';
  import {
    ConstellationGlyphIcon,
    ConstellationIcon,
    ConstellationIconButton,
    ConstellationSkeletonBlock,
  } from '$lib/components/constellation';
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

  setContext('constellation:workspace-page-modal', true);
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
          <p>{section.eyebrow}</p>
          <h1 id="workspace-page-modal-title">{section.title}</h1>
          <span>{section.subtitle}</span>
        </div>
      </div>

      <div class="workspace-page-modal__actions">
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
    position: absolute;
    inset: 0;
    z-index: 92;
    display: grid;
    align-items: center;
    justify-items: center;
    box-sizing: border-box;
    padding: clamp(14px, 2.5vw, 28px) clamp(14px, 2.5vw, 28px) clamp(14px, 2.5vw, 28px)
      max(92px, clamp(14px, 2.5vw, 28px));
  }

  .workspace-page-modal__scrim {
    position: absolute;
    inset: 0;
    border: 0;
    padding: 0;
    background: var(--workspace-page-modal-scrim, rgba(2, 5, 10, 0.54));
    cursor: default;
    backdrop-filter: blur(12px) saturate(1.02);
    -webkit-backdrop-filter: blur(12px) saturate(1.02);
  }

  :global(:root[data-color-scheme='light']) .workspace-page-modal__scrim {
    --workspace-page-modal-scrim: rgba(229, 236, 242, 0.54);
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .workspace-page-modal__surface {
    position: relative;
    z-index: 1;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    width: min(1320px, 100%);
    height: min(88svh, calc(100svh - 34px));
    min-height: 520px;
    overflow: hidden;
    border: 1px solid var(--constellation-surface-floating-border);
    border-radius: 22px;
    background: var(--constellation-surface-floating-background);
    box-shadow: var(--constellation-surface-floating-shadow);
  }

  .workspace-page-modal__header {
    display: flex;
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
    gap: 3px;
  }

  .workspace-page-modal__copy p,
  .workspace-page-modal__copy h1,
  .workspace-page-modal__copy span {
    margin: 0;
  }

  .workspace-page-modal__copy p {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
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
    flex: 0 0 auto;
    align-items: center;
    gap: 8px;
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

    .workspace-page-modal__copy span {
      white-space: normal;
    }

    .workspace-page-modal__body {
      padding: 14px;
    }
  }
</style>
