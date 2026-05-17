<script lang="ts">
  import { page } from '$app/stores';
  import ConstellationGlyphIcon from '../constellation/ConstellationGlyphIcon.svelte';
  import IllospaceLogo from './IllospaceLogo.svelte';
  import {
    WORKSPACE_PAGE_MODAL_PARAM,
    buildCortexHrefWithoutWorkspacePage,
    buildCortexWorkspacePageHref,
    isWorkspacePageModalId,
    workspacePageModalIdForPath,
  } from '$lib/features/cortex/domain/workspacePageModal';

  type NavRailGlyph =
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
  const activeWorkspacePageModalId = $derived(
    isWorkspacePageModalId($page.url.searchParams.get(WORKSPACE_PAGE_MODAL_PARAM))
      ? $page.url.searchParams.get(WORKSPACE_PAGE_MODAL_PARAM)
      : null,
  );

  function sourceParamsForNav(): URLSearchParams | undefined {
    return $page.url.pathname.startsWith('/cortex')
      ? $page.url.searchParams
      : undefined;
  }

  function hrefForItem(item: NavRailItem): string {
    const workspacePageId = workspacePageModalIdForPath(item.href);
    if (workspacePageId) {
      return buildCortexWorkspacePageHref(workspacePageId, sourceParamsForNav());
    }
    if (item.href === '/cortex') {
      return buildCortexHrefWithoutWorkspacePage(sourceParamsForNav());
    }
    return item.href;
  }

  function isActive(href: string, pathname: string): boolean {
    const workspacePageId = workspacePageModalIdForPath(href);
    if (workspacePageId) {
      return activeWorkspacePageModalId === workspacePageId || pathname.startsWith(href);
    }
    if (href === '/cortex' && activeWorkspacePageModalId) return false;
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
      href={buildCortexHrefWithoutWorkspacePage(sourceParamsForNav())}
      class="constellation-nav-rail-brand"
      aria-current={$page.url.pathname.startsWith('/cortex') && !activeWorkspacePageModalId ? 'page' : undefined}
      aria-label={`Go to ${brandLabel}`}
      title={brandLabel}
    >
      {#if brandMark}
        <span class="constellation-nav-rail-brand-mark" aria-hidden="true">
          <span class="constellation-nav-rail-brand-mark-text">{brandMark}</span>
        </span>
      {:else}
        <span class="constellation-nav-rail-brand-logo" aria-hidden="true">
          <span class="constellation-nav-rail-brand-logo-collapsed">
            <svg
              class="constellation-nav-rail-brand-icon"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="201 37 176 176"
              fill="none"
              focusable="false"
            >
              <path
                fill="currentColor"
                fill-rule="evenodd"
                d="M 298.6 62.4 A 72 72 0 1 0 333.5 180.2 L 317.2 165.5 A 50 50 0 1 1 292.9 83.7 Z"
              />
              <circle cx="295.8" cy="73.1" r="11" fill="currentColor" />
              <circle cx="325.3" cy="172.8" r="11" fill="currentColor" />
              <path
                fill="currentColor"
                fill-rule="evenodd"
                d="M 341.1 93.8 A 72 72 0 0 1 332.7 181.1 L 316.6 166.1 A 50 50 0 0 0 322.4 105.5 Z"
              />
              <circle cx="331.7" cy="99.7" r="11" fill="currentColor" />
              <circle cx="324.6" cy="173.6" r="11" fill="currentColor" />
              <circle cx="336" cy="64" r="17" fill="currentColor" />
            </svg>
          </span>
          <span class="constellation-nav-rail-brand-logo-expanded">
            <IllospaceLogo className="constellation-nav-rail-animated-logo" variant="animated" />
          </span>
        </span>
      {/if}
    </a>
  </div>

  <nav class="constellation-nav-rail-nav" aria-label="Workspace sections">
    {#each items as item}
      <a
        href={hrefForItem(item)}
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
    --nav-expanded-width: 166px;
    --nav-font-mono: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    --nav-brand-mark-background: var(--constellation-system-chrome-active-background, rgba(240, 240, 250, 0.1));
    --nav-brand-mark-color: var(--constellation-system-chrome-active-text, #f0f0fa);
    --nav-rail-background: var(
      --constellation-nav-rail-background,
      var(--constellation-system-chrome-background, linear-gradient(180deg, #000000, #04070d))
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
    backdrop-filter: var(--constellation-nav-rail-backdrop-filter, none);
    -webkit-backdrop-filter: var(--constellation-nav-rail-backdrop-filter, none);
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
    display: inline-grid;
    width: 24px;
    height: 24px;
    align-items: center;
    justify-content: center;
    overflow: visible;
    color: var(--nav-item-active-color);
    transition: width 180ms ease;
  }

  .constellation-nav-rail-brand-logo-collapsed,
  .constellation-nav-rail-brand-logo-expanded {
    grid-area: 1 / 1;
    display: inline-flex;
    height: 24px;
    align-items: center;
    justify-content: center;
  }

  .constellation-nav-rail-brand-logo-collapsed {
    width: 24px;
  }

  .constellation-nav-rail-brand-icon {
    display: block;
    width: 19px;
    height: 19px;
    color: currentColor;
  }

  .constellation-nav-rail-brand-logo-expanded {
    --illospace-logo-color: var(--nav-item-active-color);
    --illospace-logo-width: 64px;
    --illospace-logo-shift: 10.5px;
    --illospace-logo-letter-opacity: 1;
    --illospace-logo-letter-translate: 0px;
    --illospace-logo-letter-scale-y: 1;
    --illospace-logo-near-delay: 80ms;
    --illospace-logo-mid-delay: 140ms;
    --illospace-logo-i-delay: 200ms;
    display: none;
    width: 64px;
    pointer-events: none;
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
    padding-inline: 4px;
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
    width: 64px;
  }

  .constellation-nav-rail:hover .constellation-nav-rail-brand-logo-collapsed,
  .constellation-nav-rail:focus-within .constellation-nav-rail-brand-logo-collapsed,
  .constellation-nav-rail[data-expanded='true'] .constellation-nav-rail-brand-logo-collapsed {
    display: none;
  }

  .constellation-nav-rail:hover .constellation-nav-rail-brand-logo-expanded,
  .constellation-nav-rail:focus-within .constellation-nav-rail-brand-logo-expanded,
  .constellation-nav-rail[data-expanded='true'] .constellation-nav-rail-brand-logo-expanded {
    display: inline-flex;
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
