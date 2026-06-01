<script lang="ts">
  import type { Snippet } from 'svelte';

  import { ConstellationIcon, ConstellationIconButton } from '$lib/components/constellation';
  import type { GeneratedAppSurface } from '$lib/features/workspace-apps/domain/generatedAppSurface';

  let {
    title,
    description = '',
    eyebrow = 'Workspace app',
    accent = 'var(--positive)',
    surface = 'workspace',
    className = '',
    closeLabel = 'Close workspace app',
    onclose,
    actions,
    children,
  }: {
    title: string;
    description?: string | null;
    eyebrow?: string;
    accent?: string;
    surface?: GeneratedAppSurface;
    className?: string;
    closeLabel?: string;
    onclose?: () => void;
    actions?: Snippet;
    children?: Snippet;
  } = $props();

  function handleCloseClick(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    onclose?.();
  }
</script>

<section
  class={`generated-app-chrome ${className}`}
  class:is-dock={surface === 'dock'}
  class:is-stage={surface === 'stage'}
  aria-label={title}
>
  <header class="generated-app-chrome__header generated-app-shell__header">
    <div class="generated-app-chrome__identity">
      <span class="generated-app-chrome__dot" style={`--app-accent:${accent}`} aria-hidden="true"></span>
      <div class="generated-app-chrome__title-block">
        <span class="generated-app-chrome__eyebrow">{eyebrow}</span>
        <h2 class="generated-app-chrome__title">{title}</h2>
        {#if description}
          <p class="generated-app-chrome__description">{description}</p>
        {/if}
      </div>
    </div>

    <div class="generated-app-chrome__actions">
      {@render actions?.()}
      {#if onclose}
        <ConstellationIconButton label={closeLabel} variant="quiet" size="md" onclick={handleCloseClick}>
          <ConstellationIcon name="close" size={15} stroke={2} />
        </ConstellationIconButton>
      {/if}
    </div>
  </header>

  {@render children?.()}
</section>

<style>
  .generated-app-chrome {
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    min-width: 0;
    overflow: hidden;
  }

  .generated-app-chrome.generated-app-shell.is-stage {
    width: 100%;
    max-width: none;
    height: 100%;
    min-width: 0;
    min-height: 0;
    max-height: none;
    border-color: var(--constellation-thread-reading-core-border);
    border-radius: 24px;
    background: var(--constellation-thread-reading-core-background);
    box-shadow: var(--constellation-thread-reading-core-shadow);
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .generated-app-chrome.generated-app-shell.is-dock {
    width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
    max-height: none;
    border-radius: 0;
  }

  .generated-app-chrome__header {
    display: flex;
    min-width: 0;
    min-height: 52px;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    box-sizing: border-box;
    padding: 8px 12px;
  }

  .generated-app-chrome__identity,
  .generated-app-chrome__actions {
    display: flex;
    min-width: 0;
    align-items: center;
    gap: 10px;
  }

  .generated-app-chrome__identity {
    flex: 1 1 auto;
  }

  .generated-app-chrome__actions {
    flex: 0 0 auto;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .generated-app-chrome__dot {
    width: 10px;
    height: 10px;
    flex: 0 0 auto;
    border-radius: 50%;
    background: var(--app-accent, var(--positive));
    box-shadow: var(--constellation-orbit-core-shadow);
  }

  .generated-app-chrome__title-block {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .generated-app-chrome__eyebrow {
    display: block;
    color: var(--constellation-section-description);
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 0;
    line-height: 1.1;
    text-transform: uppercase;
  }

  .generated-app-chrome__title,
  .generated-app-chrome__description {
    margin: 0;
    overflow: hidden;
    letter-spacing: 0;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .generated-app-chrome__title {
    color: var(--constellation-section-title);
    font-size: 15px;
    line-height: 1.18;
  }

  .generated-app-chrome__description {
    color: var(--constellation-section-description);
    font-size: 12px;
    line-height: 1.35;
  }

  @media (max-width: 900px) {
    .generated-app-chrome.generated-app-shell.is-stage {
      border-radius: 22px;
    }
  }

  @media (max-width: 680px) {
    .generated-app-chrome__header {
      align-items: flex-start;
      flex-direction: column;
    }

    .generated-app-chrome__actions {
      width: 100%;
      justify-content: flex-start;
    }
  }
</style>
