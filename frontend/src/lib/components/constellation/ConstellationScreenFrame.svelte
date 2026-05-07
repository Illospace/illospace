<script lang="ts">
  import type { Snippet } from 'svelte';

  import ConstellationNavRail from '$lib/components/layout/ConstellationNavRail.svelte';

  type Props = {
    eyebrow?: string;
    title?: string;
    subtitle?: string;
    brandLabel?: string;
    brandMark?: string;
    statusLabel?: string;
    statusHint?: string;
    className?: string;
    contentClassName?: string;
    actions?: Snippet;
    children?: Snippet;
  };

  let {
    eyebrow = '',
    title = '',
    subtitle = '',
    brandLabel = 'Illospace',
    brandMark = '',
    className = '',
    contentClassName = '',
    actions,
    children,
  }: Props = $props();

  const rootClass = $derived(['constellation-screen-frame', className].filter(Boolean).join(' '));
  const stackClass = $derived(
    ['constellation-screen-frame-main-stack', contentClassName].filter(Boolean).join(' '),
  );
  const showHero = $derived(Boolean(eyebrow || title || subtitle || actions));
</script>

<div class={rootClass}>
  <ConstellationNavRail
    {brandLabel}
    {brandMark}
  />

  <main class="constellation-screen-frame-main-pane">
    <section class="constellation-screen-frame-board">
      <div class="constellation-screen-frame-scene-glow"></div>
      <div class="constellation-screen-frame-scene-warmth"></div>

      <div class="constellation-screen-frame-stage">
        <div class="constellation-screen-frame-shell">
          {#if showHero}
            <header class="constellation-screen-frame-hero">
              <div class="constellation-screen-frame-hero-head">
                <div class="constellation-screen-frame-hero-copy">
                  {#if eyebrow}
                    <p class="constellation-screen-frame-hero-eyebrow">{eyebrow}</p>
                  {/if}
                  {#if title}
                    <h1 class="constellation-screen-frame-hero-title">{title}</h1>
                  {/if}
                  {#if subtitle}
                    <p class="constellation-screen-frame-hero-subtitle">{subtitle}</p>
                  {/if}
                </div>

                {#if actions}
                  <div class="constellation-screen-frame-hero-actions">
                    {@render actions()}
                  </div>
                {/if}
              </div>
            </header>
          {/if}

          <div class={stackClass}>
            {#if children}
              {@render children()}
            {/if}
          </div>
        </div>
      </div>
    </section>
  </main>
</div>

<style>
  .constellation-screen-frame {
    position: relative;
    min-height: 100vh;
    overflow: hidden;
  }

  .constellation-screen-frame-main-pane {
    position: relative;
    min-height: 100vh;
  }

  .constellation-screen-frame-board {
    position: relative;
    min-height: 100vh;
    overflow: hidden;
    isolation: isolate;
  }

  .constellation-screen-frame-scene-glow,
  .constellation-screen-frame-scene-warmth {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 0;
  }

  .constellation-screen-frame-scene-glow {
    background: var(--constellation-screen-frame-scene-glow);
    filter: blur(36px);
    opacity: 0.78;
  }

  .constellation-screen-frame-scene-warmth {
    background: var(--constellation-screen-frame-scene-warmth);
    filter: blur(48px);
    opacity: 0.68;
  }

  .constellation-screen-frame-stage {
    position: relative;
    z-index: 1;
    display: grid;
    min-height: 100vh;
    padding: 26px 28px 28px 112px;
    box-sizing: border-box;
    overflow-y: auto;
  }

  .constellation-screen-frame-shell {
    display: grid;
    gap: 26px;
    width: min(100%, 1240px);
    margin: 0 auto;
  }

  .constellation-screen-frame-hero {
    display: grid;
    gap: 10px;
    padding: 4px 0 10px;
    border-bottom: 1px solid var(--constellation-screen-frame-hero-border);
  }

  .constellation-screen-frame-hero-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
  }

  .constellation-screen-frame-hero-copy {
    display: grid;
    gap: 6px;
    max-width: 680px;
  }

  .constellation-screen-frame-hero-eyebrow {
    margin: 0;
    color: var(--constellation-screen-frame-hero-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-screen-frame-hero-title {
    margin: 0;
    max-width: 820px;
    color: var(--constellation-screen-frame-hero-title);
    font-family: var(--constellation-font-sans);
    font-size: clamp(15px, 1.35vw, 18px);
    font-weight: 560;
    line-height: 1.28;
    letter-spacing: 0;
  }

  .constellation-screen-frame-hero-subtitle {
    margin: 0;
    max-width: 620px;
    color: var(--constellation-screen-frame-hero-subtitle);
    font-size: 12px;
    line-height: 1.5;
  }

  .constellation-screen-frame-hero-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 10px;
  }

  .constellation-screen-frame-main-stack {
    display: grid;
    gap: 22px;
  }

  @media (max-width: 980px) {
    .constellation-screen-frame-stage {
      padding-left: 28px;
    }

    .constellation-screen-frame-hero-head {
      flex-direction: column;
    }

    .constellation-screen-frame-hero-actions {
      justify-content: flex-start;
    }
  }
</style>
