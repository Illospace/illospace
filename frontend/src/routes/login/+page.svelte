<script lang="ts">
  import { browser } from '$app/environment';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import {
    ConstellationButton,
    ConstellationGlyphIcon,
    ConstellationNotice,
    ConstellationPanel,
    ConstellationTextInput,
  } from '$lib/components/constellation';
  import IllospaceLogo from '$lib/components/layout/IllospaceLogo.svelte';
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth.svelte';

  type View = 'login' | 'register' | 'pending';
  type PublicOrg = { id: string; name: string; slug: string };
  type AuthRouteOptions = { forceSetup?: boolean };

  const initialParams = browser ? new URLSearchParams(window.location.search) : new URLSearchParams();

  let view = $state<View>(
    initialParams.get('view') === 'register' || initialParams.has('workspace')
      ? 'register'
      : 'login',
  );
  let error = $state('');
  let loading = $state(false);
  let setupMode = $state(false);
  let routingAfterAuth = $state(false);
  let autoRoutedUserId = $state('');

  let name = $state('');
  let email = $state('');
  let password = $state('');
  let orgName = $state('');
  let setupLoaded = $state(false);
  let requestedOrg = $state<PublicOrg | null>(null);

  const workspaceSlug = $derived(($page.url.searchParams.get('workspace') || '').trim().toLowerCase());
  const wantsRegister = $derived(
    $page.url.searchParams.get('view') === 'register' || Boolean(workspaceSlug),
  );
  const joinOrg = $derived(requestedOrg);
  const requestedInviteMissing = $derived(Boolean(setupLoaded && workspaceSlug && !requestedOrg && !setupMode));
  const hasInvite = $derived(Boolean(workspaceSlug && requestedOrg));
  const inviteCheckPending = $derived(Boolean(!setupLoaded && workspaceSlug && !setupMode));
  const pendingWorkspaceName = $derived(auth.user?.org_name || joinOrg?.name || 'this workspace');

  const panelTone = $derived.by(() => {
    if (view === 'pending') return 'warning';
    if (setupMode || view === 'register') return 'info';
    return 'default';
  });

  const submitLabel = $derived.by(() => {
    if (setupMode) return 'Create workspace';
    if (view === 'register' && hasInvite) return 'Create account';
    if (view === 'register') return 'Create workspace';
    return 'Sign in';
  });

  const switchPrompt = $derived.by(() =>
    view === 'register' ? 'Already have an account?' : 'New here?',
  );

  const switchCta = $derived.by(() =>
    view === 'register' ? 'Sign in' : 'Create an account',
  );

  let setupRequestSeq = 0;

  $effect(() => {
    if (wantsRegister && view !== 'pending') {
      view = 'register';
    }
  });

  $effect(() => {
    const requestSlug = workspaceSlug;
    const requestSeq = ++setupRequestSeq;
    setupLoaded = false;
    requestedOrg = null;

    api
      .setupCheck(requestSlug || null)
      .then((res) => {
        if (requestSeq !== setupRequestSeq) return;
        requestedOrg = res.requested_org ?? null;
        setupLoaded = true;
        if (res.setup_required) {
          setupMode = true;
          view = 'register';
        } else {
          setupMode = false;
        }
      })
      .catch((e) => {
        if (requestSeq !== setupRequestSeq) return;
        setupLoaded = true;
        console.warn('[login] setup check failed', e);
      });
  });

  $effect(() => {
    if (auth.user) {
      if (loading || routingAfterAuth) {
        return;
      }
      if (auth.user.approved === false) {
        view = 'pending';
      } else if (autoRoutedUserId !== auth.user.id) {
        autoRoutedUserId = auth.user.id;
        void routeAfterApprovedAuth();
      }
    }
  });

  function resetForm() {
    name = '';
    email = '';
    password = '';
    orgName = '';
    error = '';
  }

  async function handleLogin() {
    error = '';
    if (!email || !password) {
      error = 'Email and password are required';
      return;
    }
    loading = true;
    try {
      await auth.login(email, password);
      await auth.init();
      if (auth.user && auth.user.approved === false) {
        view = 'pending';
      } else {
        await routeAfterApprovedAuth();
      }
    } catch (e: any) {
      error = e?.detail || 'Invalid credentials';
    } finally {
      loading = false;
    }
  }

  async function handleRegister() {
    error = '';
    if (inviteCheckPending) {
      error = 'Checking invite link...';
      return;
    }
    if (!name || !email || !password) {
      error = 'All fields are required';
      return;
    }
    if (password.length < 8) {
      error = 'Password must be at least 8 characters';
      return;
    }
    if (setupMode && !orgName) {
      error = 'Workspace name is required';
      return;
    }
    if (!setupMode && !hasInvite && !orgName) {
      error = 'Workspace name is required';
      return;
    }
    loading = true;
    try {
      const mode = !setupMode && hasInvite ? 'join' : 'create';
      await api.register({
        name,
        email,
        password,
        workspace_mode: mode,
        ...(mode === 'join' && joinOrg?.slug ? { workspace_slug: joinOrg.slug } : {}),
        ...(mode === 'create' && orgName ? { org_name: orgName } : {}),
      });
      await auth.init();
      if (auth.user && auth.user.approved === false) {
        view = 'pending';
      } else {
        await routeAfterApprovedAuth({ forceSetup: mode === 'create' });
      }
    } catch (e: any) {
      error = e?.detail || 'Registration failed';
    } finally {
      loading = false;
    }
  }

  async function handleLogout() {
    await auth.logout();
    view = 'login';
    autoRoutedUserId = '';
    resetForm();
    await goto('/login', { replaceState: true, noScroll: true });
  }

  function switchToRegister() {
    resetForm();
    view = 'register';
    void goto('/login?view=register&mode=create', { replaceState: true, noScroll: true });
  }

  function switchToLogin() {
    resetForm();
    setupMode = false;
    view = 'login';
    void goto('/login', { replaceState: true, noScroll: true });
  }

  function handleSubmit(event: SubmitEvent) {
    event.preventDefault();
    if (view === 'login') {
      void handleLogin();
      return;
    }
    void handleRegister();
  }

  async function routeAfterApprovedAuth({ forceSetup = false }: AuthRouteOptions = {}) {
    if (routingAfterAuth) return;
    routingAfterAuth = true;
    if (auth.user?.id) {
      autoRoutedUserId = auth.user.id;
    }
    try {
      if (forceSetup) {
        await goto('/onboarding');
        return;
      }
      const runtime = await api.runtimeSettings();
      if (runtime?.connection?.setup_required) {
        await goto('/onboarding');
        return;
      }
      const params = new URLSearchParams({ onboarding: 'runtime-ready' });
      await goto(`/cortex?${params.toString()}`);
    } catch {
      await goto('/onboarding');
    } finally {
      routingAfterAuth = false;
    }
  }
</script>

<div class="auth-shell">
  <div class="auth-scene auth-scene-spectral" aria-hidden="true"></div>
  <div class="auth-scene auth-scene-warm" aria-hidden="true"></div>
  <div class="auth-stars" aria-hidden="true"></div>

  <main class="auth-stage">
    <div class="auth-stack">
      <div class="auth-brand">
        <div class="auth-brand-mark" aria-hidden="true">
          <IllospaceLogo className="auth-brand-logo" variant="small" title="Illospace" />
        </div>
      </div>

      <ConstellationPanel tone={panelTone} padding="lg" className="auth-panel">
        {#if view === 'pending'}
          <div class="auth-pending">
            <div class="auth-pending-icon" aria-hidden="true">
              <ConstellationGlyphIcon label="team" />
            </div>
            <p class="auth-pending-title">Your request to join {pendingWorkspaceName} is pending approval.</p>
            <p class="auth-pending-copy">A workspace owner needs to approve your account before you can enter.</p>
            <ConstellationButton variant="secondary" onclick={handleLogout}>
              Sign out
            </ConstellationButton>
          </div>
        {:else}
          {#if error}
            <ConstellationNotice tone="danger" compact title={error} />
          {/if}

          <form class="auth-form" onsubmit={handleSubmit}>
            {#if view === 'register' && !setupMode}
              {#if requestedInviteMissing}
                <ConstellationNotice
                  tone="warning"
                  compact
                  title="Invite link not found."
                  description="Ask for a fresh invite, or create a new workspace below."
                />
              {:else if hasInvite && joinOrg}
                <ConstellationNotice
                  tone="info"
                  compact
                  title={`Joining ${joinOrg.name}`}
                  description="Create your account to request access. A workspace owner will approve it."
                />
              {:else}
                <p class="auth-context">
                  To join an existing workspace, ask a teammate for an invite link.
                </p>
              {/if}
            {/if}

            {#if setupMode || (view === 'register' && !hasInvite && !inviteCheckPending)}
              <label class="auth-field" for="auth-org-name">
                <span class="auth-field-label">Workspace name</span>
                <ConstellationTextInput
                  id="auth-org-name"
                  bind:value={orgName}
                  type="text"
                  placeholder="e.g. Acme"
                  maxlength={120}
                  required
                />
              </label>
            {/if}

            {#if view === 'register'}
              <label class="auth-field" for="auth-name">
                <span class="auth-field-label">Your name</span>
                <ConstellationTextInput
                  id="auth-name"
                  bind:value={name}
                  type="text"
                  placeholder="e.g. Alex"
                  maxlength={120}
                  autocomplete="name"
                  required
                />
              </label>
            {/if}

            <label class="auth-field" for="auth-email">
              <span class="auth-field-label">Email</span>
              <ConstellationTextInput
                id="auth-email"
                bind:value={email}
                type="email"
                placeholder="you@example.com"
                autocomplete="email"
                inputmode="email"
                required
              />
            </label>

            <label class="auth-field" for="auth-password">
              <span class="auth-field-label">Password</span>
              <ConstellationTextInput
                id="auth-password"
                bind:value={password}
                type="password"
                placeholder={view === 'register' ? 'Min. 8 characters' : ''}
                autocomplete={view === 'register' ? 'new-password' : 'current-password'}
                required
              />
            </label>

            <ConstellationButton
              type="submit"
              fullWidth
              loading={loading}
              loadingLabel="Working..."
            >
              {submitLabel}
            </ConstellationButton>
          </form>

          {#if !setupMode}
            <div class="auth-switch">
              <span>{switchPrompt}</span>
              <ConstellationButton
                variant="quiet"
                size="sm"
                className="auth-switch-button"
                onclick={view === 'register' ? switchToLogin : switchToRegister}
              >
                {switchCta}
              </ConstellationButton>
            </div>
          {/if}
        {/if}
      </ConstellationPanel>
    </div>
  </main>
</div>

<style>
  .auth-shell {
    position: relative;
    min-height: 100vh;
    overflow: hidden;
    isolation: isolate;
    background: var(--constellation-workspace-theme-background);
  }

  .auth-shell::before {
    content: '';
    position: absolute;
    inset: 0;
    z-index: 0;
    pointer-events: none;
    background:
      radial-gradient(circle at top, rgba(255, 255, 255, 0.1), transparent 28%),
      linear-gradient(180deg, rgba(255, 255, 255, 0.03), transparent 34%);
    opacity: 0.6;
  }

  .auth-stars,
  .auth-scene {
    position: absolute;
    inset: 0;
    z-index: 0;
    pointer-events: none;
  }

  .auth-stars {
    background-image:
      radial-gradient(var(--constellation-workspace-theme-star-color-a) 0.8px, transparent 0.8px),
      radial-gradient(var(--constellation-workspace-theme-star-color-b) 1px, transparent 1px),
      radial-gradient(var(--constellation-workspace-theme-star-color-c) 1px, transparent 1px);
    background-position:
      0 0,
      44px 92px,
      146px 34px;
    background-size:
      240px 240px,
      320px 320px,
      400px 400px;
    opacity: var(--constellation-workspace-theme-star-opacity);
  }

  .auth-scene {
    filter: blur(54px);
  }

  .auth-scene-spectral {
    background:
      radial-gradient(
        circle at 22% 18%,
        color-mix(in srgb, var(--constellation-color-spectral) 26%, transparent),
        transparent 24%
      );
  }

  .auth-scene-warm {
    background:
      radial-gradient(
        circle at 82% 82%,
        color-mix(in srgb, var(--constellation-color-amber) 22%, transparent),
        transparent 26%
      );
  }

  .auth-stage {
    position: relative;
    z-index: 1;
    display: grid;
    min-height: 100vh;
    place-items: center;
    padding: clamp(24px, 4vw, 48px);
  }

  .auth-stack {
    display: grid;
    gap: 18px;
    width: min(100%, 430px);
  }

  .auth-brand {
    display: grid;
    justify-items: center;
    gap: 12px;
    text-align: center;
  }

  .auth-brand-mark {
    display: grid;
    place-items: center;
    width: 112px;
    height: 64px;
  }

  .auth-brand-mark :global(.auth-brand-logo) {
    width: 100%;
    height: 100%;
  }

  :global(.auth-panel .constellation-panel-content) {
    display: grid;
    gap: 18px;
  }

  .auth-form {
    display: grid;
    gap: 14px;
  }

  .auth-context {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.5;
  }

  .auth-field {
    display: grid;
    gap: 7px;
  }

  .auth-field-label {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .auth-form :global(.constellation-text-input) {
    min-height: 40px;
    padding-inline: 12px;
  }

  .auth-switch {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.5;
  }

  :global(.auth-switch-button) {
    flex: 0 0 auto;
  }

  .auth-pending {
    display: grid;
    justify-items: center;
    gap: 14px;
    text-align: center;
  }

  .auth-pending-icon {
    display: grid;
    place-items: center;
    width: 44px;
    height: 44px;
    border-radius: 999px;
    border: 1px solid color-mix(in srgb, var(--constellation-color-amber) 28%, var(--constellation-surface-nested-border));
    background: color-mix(in srgb, var(--constellation-color-amber) 10%, var(--constellation-surface-nested-background));
    color: var(--constellation-color-text-primary);
    box-shadow: var(--constellation-surface-nested-shadow);
  }

  .auth-pending-icon :global(.constellation-glyph-icon),
  .auth-pending-icon :global(svg) {
    width: 18px;
    height: 18px;
  }

  .auth-pending-title,
  .auth-pending-copy {
    margin: 0;
  }

  .auth-pending-title {
    color: var(--constellation-color-text-primary);
    font-size: 15px;
    line-height: 1.55;
  }

  .auth-pending-copy {
    max-width: 30ch;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.6;
  }

  @media (max-width: 640px) {
    .auth-stage {
      padding: 20px 16px 28px;
    }

    .auth-stack {
      width: 100%;
    }

    .auth-switch {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
