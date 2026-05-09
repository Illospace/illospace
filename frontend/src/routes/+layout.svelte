<script lang="ts">
  import '../app.css';
  import { ConstellationNavRail } from '$lib/components/constellation';
  import Toast from '$lib/components/layout/Toast.svelte';
  import GlobalSearch from '$lib/components/layout/GlobalSearch.svelte';
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth.svelte';
  import { theme } from '$lib/stores/theme.svelte';
  import { wsClient } from '$lib/stores/ws.svelte';
  import { requiresPersonalOpenAIOnboarding } from '$lib/utils/runtimeOnboarding';
  import { onMount, onDestroy } from 'svelte';
  import { dev } from '$app/environment';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';

  let { children } = $props();
  let currentPath = $derived($page.url.pathname);
  let isLoginPage = $derived(currentPath === '/login');
  let isOnboardingPage = $derived(currentPath.startsWith('/onboarding'));
  let isSystemPage = $derived(
    currentPath.startsWith('/system') || currentPath.startsWith('/auth/') || isOnboardingPage,
  );
  let isCortexPage = $derived(currentPath.startsWith('/cortex'));
  let isVaultPreviewPage = $derived(
    dev && currentPath === '/vault' && $page.url.searchParams.get('preview') === '1',
  );
  let isCyclesPreviewPage = $derived(
    dev && currentPath === '/cycles' && $page.url.searchParams.get('preview') === '1',
  );
  let isPreviewPage = $derived(isVaultPreviewPage || isCyclesPreviewPage);
  let showNavRail = $derived(!auth.loading && !isLoginPage && !isPreviewPage && !isOnboardingPage);
  let showSearch = $state(false);
  let navRailArrivalActive = $state(false);
  let navRailArrivalPlayed = $state(false);
  let navRailArrivalTimer: ReturnType<typeof setTimeout> | null = null;
  const wsTabId =
    typeof crypto !== 'undefined' && 'randomUUID' in crypto
      ? crypto.randomUUID()
      : `tab-${Date.now()}-${Math.random().toString(36).slice(2)}`;

  function handleKeydown(e: KeyboardEvent) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
      e.preventDefault();
      showSearch = !showSearch;
    }
  }

  $effect(() => {
    if (!showNavRail || navRailArrivalPlayed) return;
    navRailArrivalPlayed = true;

    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;

    navRailArrivalActive = true;
    const timeout = window.setTimeout(() => {
      navRailArrivalActive = false;
      if (navRailArrivalTimer === timeout) navRailArrivalTimer = null;
    }, 620);
    navRailArrivalTimer = timeout;

    return () => {
      if (navRailArrivalTimer === timeout) navRailArrivalTimer = null;
      window.clearTimeout(timeout);
    };
  });

  onMount(async () => {
    theme.init();
    document.addEventListener('keydown', handleKeydown);
    if (isPreviewPage) {
      auth.loading = false;
      return;
    }
    await auth.init();
    if (auth.user) {
      if (auth.user.approved === false && currentPath !== '/login') {
        goto('/login');
      } else if (auth.user.approved !== false) {
        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        wsClient.connect(
          `${protocol}//${location.host}/ws`,
          async () => (await api.issueWsToken(wsTabId)).token,
        );

        try {
          const runtime = await api.runtimeSettings();
          if (requiresPersonalOpenAIOnboarding(runtime) && !isSystemPage) {
            goto('/onboarding');
          }
        } catch {
          // Runtime setup is recoverable from the System tab.
        }
      }
    } else if (currentPath !== '/login' && !isPreviewPage) {
      goto('/login');
    }
  });

  onDestroy(() => {
    if (navRailArrivalTimer) {
      clearTimeout(navRailArrivalTimer);
      navRailArrivalTimer = null;
    }
    document.removeEventListener('keydown', handleKeydown);
  });

</script>

{#if auth.loading}
  <div class="loading-screen">Loading...</div>
{:else if isLoginPage}
  {@render children()}
{:else}
  <div class="app-layout" class:cortex-layout={isCortexPage}>
    {#if showNavRail}
      <ConstellationNavRail className={navRailArrivalActive ? 'constellation-nav-rail-arriving' : ''} />
    {/if}
    <main
      class="main-content"
      class:cortex-host={isCortexPage}
      class:onboarding-host={isOnboardingPage}
      class:preview-host={isPreviewPage || isOnboardingPage}
    >
      {@render children()}
    </main>
  </div>
  <Toast />
  <GlobalSearch visible={showSearch} onclose={() => (showSearch = false)} />
{/if}

<style>
  .app-layout {
    --app-nav-shell-gap: 16px;
    --app-nav-collapsed-width: 54px;
    --app-nav-reserved-width: calc(
      var(--app-nav-shell-gap) * 2 + var(--app-nav-collapsed-width) + 24px
    );
    display: flex;
    min-height: 100vh;
    position: relative;
    background: var(--constellation-workspace-theme-background, var(--bg-0));
  }
  .app-layout.cortex-layout {
    overflow: hidden;
  }
  :global(.constellation-nav-rail.constellation-nav-rail-arriving) {
    animation: cortex-nav-rail-arrive 620ms cubic-bezier(0.22, 1, 0.36, 1) both;
  }
  .main-content {
    flex: 1;
    box-sizing: border-box;
    min-height: 100vh;
    margin-left: 0;
    padding: var(--page-padding);
    padding-left: calc(var(--page-padding) + var(--app-nav-reserved-width));
    max-width: var(--page-max-width);
    overflow-y: auto;
  }
  .main-content.cortex-host {
    margin-left: 0 !important;
    padding: 0;
    max-width: none;
    overflow: hidden;
    height: 100vh;
    width: 100%;
  }
  .main-content.preview-host {
    padding-left: var(--page-padding);
  }
  .main-content.onboarding-host {
    margin-left: 0 !important;
    padding: 0;
    max-width: none;
    overflow: hidden;
    height: 100vh;
    width: 100%;
  }
  @media (max-width: 900px) {
    .main-content {
      padding-left: calc(var(--page-padding) + 78px);
    }
    .main-content.preview-host {
      padding-left: var(--page-padding);
    }
    .main-content.onboarding-host {
      padding: 0;
    }
  }
  .loading-screen {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100vh;
    color: var(--text-2);
    font-family: var(--font-sans);
  }
  @keyframes cortex-nav-rail-arrive {
    0% {
      opacity: 0;
      filter: blur(3px);
      transform: translate3d(-24px, 0, 0);
    }

    100% {
      opacity: 1;
      filter: none;
      transform: translate3d(0, 0, 0);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    :global(.constellation-nav-rail.constellation-nav-rail-arriving) {
      animation: none !important;
    }
  }
</style>
