<script lang="ts">
  import { page } from '$app/stores';
  import ConstellationGlyphIcon from '../constellation/ConstellationGlyphIcon.svelte';
  import IllospaceLogo from './IllospaceLogo.svelte';

  type NavRailGlyph =
    | 'cortex'
    | 'cycles'
    | 'skills'
    | 'team'
    | 'vault'
    | 'runtime';
  type NavRailItem = {
    href: string;
    label: string;
    glyph: NavRailGlyph;
  };

  const defaultItems: readonly NavRailItem[] = [
    { href: '/cortex', label: 'Cortex', glyph: 'cortex' },
    { href: '/cycles', label: 'Cycles', glyph: 'cycles' },
    { href: '/skills', label: 'Skills', glyph: 'skills' },
    { href: '/team', label: 'Team', glyph: 'team' },
    { href: '/vault', label: 'Vault', glyph: 'vault' },
    { href: '/system', label: 'AI Runtime', glyph: 'runtime' },
  ];

  let {
    items = defaultItems,
    brandLabel = 'Illospace',
    brandMark = '',
    forceExpanded = false,
    className = '',
  }: {
    items?: readonly NavRailItem[];
    brandLabel?: string;
    brandMark?: string;
    forceExpanded?: boolean;
    className?: string;
  } = $props();

  const shellClass = $derived(['constellation-nav-rail', className].filter(Boolean).join(' '));

  function isActive(href: string, pathname: string): boolean {
    if (href === '/') return pathname === '/';
    return pathname === href || pathname.startsWith(`${href}/`);
  }
</script>

<aside
  class={shellClass}
  data-expanded={forceExpanded ? 'true' : undefined}
  aria-label="Primary workspace navigation"
>
  <div class="constellation-nav-rail-header">
    <a
      href="/cortex"
      class="constellation-nav-rail-brand"
      aria-current={$page.url.pathname.startsWith('/cortex') ? 'page' : undefined}
      aria-label={`Go to ${brandLabel}`}
      title={brandLabel}
    >
      {#if brandMark}
        <span class="constellation-nav-rail-brand-mark" aria-hidden="true">
          <span class="constellation-nav-rail-brand-mark-text">{brandMark}</span>
        </span>
      {:else}
        <span class="constellation-nav-rail-brand-logo" aria-hidden="true">
          <IllospaceLogo className="constellation-nav-rail-animated-logo" variant="animated" />
        </span>
      {/if}
    </a>
  </div>

  <nav class="constellation-nav-rail-nav" aria-label="Workspace sections">
    {#each items as item}
      <a
        href={item.href}
        class="constellation-nav-rail-item"
        class:is-active={isActive(item.href, $page.url.pathname)}
        aria-current={isActive(item.href, $page.url.pathname) ? 'page' : undefined}
        aria-label={item.label}
        title={item.label}
      >
        <span class="constellation-nav-rail-glyph" aria-hidden="true">
          <ConstellationGlyphIcon label={item.glyph} />
        </span>
        <span class="constellation-nav-rail-item-label">{item.label}</span>
      </a>
    {/each}
  </nav>
</aside>

<style>
  .constellation-nav-rail {
    --nav-shell-gap: 16px;
    --nav-collapsed-width: 54px;
    --nav-expanded-width: 184px;
    --nav-font-mono: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    --nav-brand-mark-background: var(--constellation-system-chrome-active-background, rgba(240, 240, 250, 0.1));
    --nav-brand-mark-color: var(--constellation-system-chrome-active-text, #f0f0fa);
    --nav-rail-background: var(
      --constellation-system-chrome-background,
      linear-gradient(180deg, rgba(0, 0, 0, 0.82), rgba(4, 7, 13, 0.72))
    );
    --nav-rail-border: var(--constellation-system-chrome-border, rgba(240, 240, 250, 0.08));
    --nav-item-color: var(--constellation-system-chrome-text, rgba(240, 240, 250, 0.58));
    --nav-item-active-background: var(--constellation-system-chrome-active-background, rgba(240, 240, 250, 0.06));
    --nav-item-active-color: var(--constellation-system-chrome-active-text, #ffffff);
    --nav-glyph-color: rgba(240, 240, 250, 0.72);
    position: fixed;
    top: var(--nav-shell-gap);
    left: var(--nav-shell-gap);
    bottom: auto;
    z-index: var(--z-nav, 100);
    display: flex;
    width: var(--nav-collapsed-width);
    min-height: auto;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--nav-rail-border);
    border-radius: 18px;
    background: var(--nav-rail-background);
    box-shadow: var(
      --constellation-system-chrome-shadow,
      0 24px 80px rgba(0, 0, 0, 0.22),
      inset 0 1px 0 rgba(240, 240, 250, 0.08)
    );
    backdrop-filter: var(--constellation-nav-rail-backdrop-filter, blur(20px));
    -webkit-backdrop-filter: var(--constellation-nav-rail-backdrop-filter, blur(20px));
    transition:
      width 220ms ease,
      transform 220ms ease,
      box-shadow 220ms ease;
  }

  .constellation-nav-rail:hover,
  .constellation-nav-rail:focus-within,
  .constellation-nav-rail[data-expanded='true'] {
    width: min(var(--nav-expanded-width), calc(100vw - (var(--nav-shell-gap) * 2)));
  }

  .constellation-nav-rail-header {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 8px 4px 7px;
    border-bottom: 1px solid rgba(240, 240, 250, 0.06);
  }

  .constellation-nav-rail-brand {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    height: 32px;
    width: 100%;
    min-width: 0;
    gap: 0;
    overflow: visible;
    border-radius: 12px;
    color: inherit;
    text-decoration: none;
    transition:
      gap 180ms ease,
      padding-inline 180ms ease;
  }

  .constellation-nav-rail-brand-logo,
  .constellation-nav-rail-glyph {
    flex-shrink: 0;
  }

  .constellation-nav-rail-brand-mark {
    display: inline-flex;
    width: 24px;
    height: 24px;
    align-items: center;
    justify-content: center;
  }

  .constellation-nav-rail-brand-logo {
    --illospace-logo-color: var(--nav-item-active-color);
    --illospace-logo-width: 24px;
    --illospace-logo-shift: -18.25px;
    --illospace-logo-letter-opacity: 0;
    --illospace-logo-letter-translate: 70px;
    --illospace-logo-letter-scale-y: 0.86;
    --illospace-logo-near-delay: 90ms;
    --illospace-logo-mid-delay: 45ms;
    --illospace-logo-i-delay: 0ms;
    display: inline-flex;
    width: 64px;
    height: 24px;
    align-items: center;
    justify-content: center;
  }

  .constellation-nav-rail-brand-mark-text {
    display: inline-flex;
    width: 24px;
    height: 24px;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background: var(--nav-brand-mark-background);
    color: var(--nav-brand-mark-color);
    font-family: var(--nav-font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .constellation-nav-rail-nav {
    display: grid;
    flex: 0 0 auto;
    align-content: start;
    gap: 6px;
    padding: 10px 8px 8px;
  }

  .constellation-nav-rail-item {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    width: 100%;
    height: 38px;
    min-width: 0;
    padding: 0;
    border-radius: 12px;
    color: var(--nav-item-color);
    text-decoration: none;
    font-family: var(--nav-font-mono);
    font-size: 11px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    transition:
      color 160ms ease,
      background-color 160ms ease,
      box-shadow 160ms ease;
  }

  .constellation-nav-rail-item-label {
    max-width: 0;
    min-width: 0;
    overflow: hidden;
    opacity: 0;
    transform: translateX(-6px);
    transition:
      max-width 180ms ease,
      opacity 180ms ease,
      transform 180ms ease;
    pointer-events: none;
    white-space: nowrap;
  }

  .constellation-nav-rail-item:hover,
  .constellation-nav-rail-item:focus-visible {
    color: var(--constellation-system-chrome-text-hover, rgba(240, 240, 250, 0.78));
    outline: none;
  }

  .constellation-nav-rail-item.is-active {
    background: var(--nav-item-active-background);
    color: var(--nav-item-active-color);
    box-shadow: var(
      --constellation-nav-rail-item-active-shadow,
      var(
        --constellation-system-chrome-active-shadow,
        inset 0 0 0 1px rgba(240, 240, 250, 0.14),
        0 0 24px rgba(141, 183, 255, 0.08)
      )
    );
  }

  .constellation-nav-rail-glyph {
    display: inline-flex;
    width: 22px;
    min-width: 22px;
    height: 22px;
    align-items: center;
    justify-content: center;
    color: var(--nav-glyph-color);
  }

  .constellation-nav-rail-item.is-active .constellation-nav-rail-glyph {
    color: var(--nav-item-active-color);
  }

  .constellation-nav-rail-glyph :global(svg) {
    width: 14px;
    height: 14px;
  }

  .constellation-nav-rail:hover .constellation-nav-rail-header,
  .constellation-nav-rail:focus-within .constellation-nav-rail-header,
  .constellation-nav-rail[data-expanded='true'] .constellation-nav-rail-header {
    justify-content: center;
  }

  .constellation-nav-rail:hover .constellation-nav-rail-brand,
  .constellation-nav-rail:focus-within .constellation-nav-rail-brand,
  .constellation-nav-rail[data-expanded='true'] .constellation-nav-rail-brand {
    justify-content: center;
    gap: 0;
    padding-inline: 0;
  }

  .constellation-nav-rail:hover .constellation-nav-rail-brand-logo,
  .constellation-nav-rail:focus-within .constellation-nav-rail-brand-logo,
  .constellation-nav-rail[data-expanded='true'] .constellation-nav-rail-brand-logo {
    --illospace-logo-width: 64px;
    --illospace-logo-shift: 10.5px;
    --illospace-logo-letter-opacity: 1;
    --illospace-logo-letter-translate: 0px;
    --illospace-logo-letter-scale-y: 1;
    --illospace-logo-near-delay: 80ms;
    --illospace-logo-mid-delay: 140ms;
    --illospace-logo-i-delay: 200ms;
  }

  .constellation-nav-rail:hover .constellation-nav-rail-item,
  .constellation-nav-rail:focus-within .constellation-nav-rail-item,
  .constellation-nav-rail[data-expanded='true'] .constellation-nav-rail-item {
    justify-content: flex-start;
    gap: 10px;
    padding-inline: 11px;
  }

  .constellation-nav-rail:hover .constellation-nav-rail-item-label,
  .constellation-nav-rail:focus-within .constellation-nav-rail-item-label,
  .constellation-nav-rail[data-expanded='true'] .constellation-nav-rail-item-label {
    max-width: 112px;
    opacity: 1;
    transform: translateX(0);
    pointer-events: auto;
  }

  @media (max-width: 900px) {
    .constellation-nav-rail {
      top: 12px;
      left: 12px;
      bottom: auto;
    }
  }
</style>
