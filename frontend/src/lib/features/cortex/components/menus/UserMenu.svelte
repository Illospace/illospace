<script lang="ts">
  import { onMount, tick } from 'svelte';
  import {
    buildAstrePrimitiveStyle,
    ConstellationAstrePalette,
    ConstellationButton,
    ConstellationIcon,
    ConstellationPill,
    ConstellationTextInput,
  } from '$lib/components/constellation';
  import { goto } from '$app/navigation';
  import { updateProfile } from '$lib/features/cortex/api/cortexApi';
  import { auth } from '$lib/stores/auth.svelte';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { theme } from '$lib/stores/theme.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { wsClient } from '$lib/stores/ws.svelte';
  import { normalizeHexColor } from '$lib/utils/constellationPresence';
  import {
    DEFAULT_PROFILE_COLOR,
    USER_PROFILE_COLOR_OPTIONS,
  } from './userProfilePalette';

  export type CortexUserMenuAnchor = {
    x: number;
    y: number;
  };

  let {
    anchor,
    onclose,
  }: {
    anchor: CortexUserMenuAnchor;
    onclose?: () => void;
  } = $props();

  let menuEl: HTMLElement | undefined = $state();
  let profileName = $state('');
  let profileColor = $state(DEFAULT_PROFILE_COLOR);
  let saving = $state(false);
  let loggingOut = $state(false);
  let lastAnchorKey = $state('');
  let viewportWidth = $state(1280);
  let viewportHeight = $state(800);

  const currentUser = $derived(auth.user);
  const trimmedName = $derived(profileName.trim());
  const currentColor = $derived(normalizeHexColor(currentUser?.color) ?? DEFAULT_PROFILE_COLOR);
  const normalizedProfileColor = $derived(normalizeHexColor(profileColor) ?? DEFAULT_PROFILE_COLOR);
  const usedColorOwners = $derived.by(() => {
    const owners = new Map<string, string>();
    for (const member of cortex.teamMembers) {
      if (!member?.id || member.id === currentUser?.id) continue;
      const color = normalizeHexColor(member.color ?? (member as any).cortex_color);
      if (!color) continue;
      owners.set(color.toLowerCase(), member.name || member.email || 'A teammate');
    }
    return owners;
  });
  const usedNames = $derived.by(() => {
    const names = new Set<string>();
    for (const member of cortex.teamMembers) {
      if (!member?.id || member.id === currentUser?.id) continue;
      const name = String(member.name || '').trim();
      if (name) names.add(name.toLowerCase());
    }
    return names;
  });
  const nameTaken = $derived(Boolean(trimmedName && usedNames.has(trimmedName.toLowerCase())));
  const colorTakenBy = $derived(usedColorOwners.get(normalizedProfileColor.toLowerCase()) ?? '');
  const colorTaken = $derived(Boolean(colorTakenBy));
  const hasChanges = $derived(
    Boolean(
      currentUser
        && (trimmedName !== (currentUser.name || '').trim()
          || normalizedProfileColor.toLowerCase() !== currentColor.toLowerCase()),
    ),
  );
  const validationMessage = $derived.by(() => {
    if (!currentUser) return 'Profile is not available yet.';
    if (!trimmedName) return 'Choose a username.';
    if (nameTaken) return 'That username is already taken in this workspace.';
    if (colorTaken) return `${colorTakenBy} already uses that color.`;
    return '';
  });
  const canSave = $derived(Boolean(currentUser && hasChanges && !saving && !validationMessage));
  const paletteItems = $derived.by(() =>
    USER_PROFILE_COLOR_OPTIONS.map((option) => {
      const takenBy = usedColorOwners.get(option.id.toLowerCase());
      const disabled = Boolean(takenBy && option.id.toLowerCase() !== currentColor.toLowerCase());
      return {
        ...option,
        astreStyle: buildAstrePrimitiveStyle({
          id: option.id,
          accent: option.id,
          mode: theme.mode,
          activity: 'idle',
        }),
        disabled,
        title: disabled ? `${option.label} is used by ${takenBy}` : option.label,
      };
    }),
  );
  const menuStyle = $derived.by(() => {
    const width = 352;
    const height = 480;
    const left = clamp(anchor.x + 18, 92, Math.max(92, viewportWidth - width - 18));
    const top = clamp(anchor.y - 28, 18, Math.max(18, viewportHeight - height - 18));
    return `left: ${left}px; top: ${top}px;`;
  });

  $effect(() => {
    const anchorKey = `${Math.round(anchor.x)}:${Math.round(anchor.y)}:${currentUser?.id ?? ''}`;
    if (anchorKey === lastAnchorKey) return;
    lastAnchorKey = anchorKey;
    profileName = currentUser?.name || '';
    profileColor = currentColor;
    void tick().then(() => {
      menuEl?.querySelector<HTMLInputElement>('#cortex-user-menu-name')?.focus();
    });
  });

  onMount(() => {
    updateViewport();
    window.addEventListener('resize', updateViewport);
    window.addEventListener('keydown', handleWindowKeydown);
    return () => {
      window.removeEventListener('resize', updateViewport);
      window.removeEventListener('keydown', handleWindowKeydown);
    };
  });

  function clamp(value: number, min: number, max: number) {
    return Math.min(max, Math.max(min, value));
  }

  function updateViewport() {
    viewportWidth = window.innerWidth;
    viewportHeight = window.innerHeight;
  }

  function handleWindowKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') onclose?.();
  }

  function handleColorChange(nextColor: string) {
    const normalized = normalizeHexColor(nextColor);
    if (!normalized) return;
    if (usedColorOwners.has(normalized.toLowerCase()) && normalized.toLowerCase() !== currentColor.toLowerCase()) {
      ui.toast('That color is already taken in this workspace.', 'error');
      return;
    }
    profileColor = normalized;
  }

  async function saveProfile() {
    if (!currentUser || !canSave) {
      if (validationMessage) ui.toast(validationMessage, 'error');
      return;
    }

    saving = true;
    const previousName = currentUser.name;
    const previousColor = currentColor;
    const nextName = trimmedName;
    const nextColor = normalizedProfileColor;
    try {
      await updateProfile({
        name: nextName,
        color: nextColor,
      });
      auth.user = {
        ...currentUser,
        name: nextName,
        color: nextColor,
      };
      cortex.applyUserProfileUpdate({
        userId: currentUser.id,
        name: nextName,
        color: nextColor,
        previousName,
        previousColor,
      });
      ui.toast('Profile updated', 'success');
      onclose?.();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to update profile', 'error');
    } finally {
      saving = false;
    }
  }

  async function logOut() {
    if (loggingOut || saving) return;
    loggingOut = true;
    try {
      await auth.logout();
      wsClient.disconnect();
      onclose?.();
      await goto('/login');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to log out', 'error');
      loggingOut = false;
    }
  }
</script>

<div class="cortex-user-menu-layer">
  <button
    type="button"
    class="cortex-user-menu-backdrop"
    aria-label="Close user menu"
    onclick={() => onclose?.()}
  ></button>

  <section
    class="cortex-user-menu"
    style={menuStyle}
    bind:this={menuEl}
    aria-label="User menu"
  >
    <header class="cortex-user-menu-header">
      <div>
        <p class="cortex-user-menu-eyebrow">You</p>
        <h2>{currentUser?.name || 'Profile'}</h2>
      </div>
      {#if currentUser?.email}
        <ConstellationPill variant="muted">{currentUser.email}</ConstellationPill>
      {/if}
    </header>

    <div class="cortex-user-menu-body">
      <div class="cortex-user-menu-field">
        <label class="cortex-user-menu-label" for="cortex-user-menu-name">Username</label>
        <ConstellationTextInput
          id="cortex-user-menu-name"
          bind:value={profileName}
          maxlength={100}
          placeholder="Your name"
          disabled={saving}
        />
      </div>

      <div class="cortex-user-menu-field">
        <div class="cortex-user-menu-row-label">
          <span class="cortex-user-menu-label">Color</span>
          <span class="cortex-user-menu-hint">Taken colors are locked</span>
        </div>
        <ConstellationAstrePalette
          items={paletteItems}
          value={normalizedProfileColor}
          ariaLabel="Workspace color"
          columns={4}
          previewLetter=""
          previewOwner=""
          astreScale="compact"
          astreArchivedCount={0}
          swatchSize={72}
          onValueChange={handleColorChange}
        />
      </div>

      {#if validationMessage}
        <p class="cortex-user-menu-validation" aria-live="polite">{validationMessage}</p>
      {/if}
    </div>

    <footer class="cortex-user-menu-actions">
      <ConstellationButton
        variant="destructive"
        size="sm"
        loading={loggingOut}
        loadingLabel="Log out"
        disabled={saving}
        onclick={logOut}
      >
        {#snippet leadingVisual()}
          <ConstellationIcon name="logout" size={14} stroke={1.9} />
        {/snippet}
        Log out
      </ConstellationButton>
      <div class="cortex-user-menu-action-group">
        <ConstellationButton
          variant="quiet"
          size="sm"
          disabled={saving || loggingOut}
          onclick={() => onclose?.()}
        >
          Cancel
        </ConstellationButton>
        <ConstellationButton
          variant="primary"
          size="sm"
          loading={saving}
          loadingLabel="Saving"
          disabled={!canSave || loggingOut}
          onclick={saveProfile}
        >
          Save
        </ConstellationButton>
      </div>
    </footer>
  </section>
</div>

<style>
  .cortex-user-menu-layer {
    position: fixed;
    inset: 0;
    z-index: 220;
    pointer-events: none;
  }

  .cortex-user-menu-backdrop {
    position: absolute;
    inset: 0;
    border: 0;
    background: transparent;
    cursor: default;
    pointer-events: auto;
  }

  .cortex-user-menu {
    --cortex-user-menu-border: rgba(240, 240, 250, 0.09);
    --cortex-user-menu-background: rgba(9, 12, 18, 0.98);
    --cortex-user-menu-shadow:
      0 18px 42px rgba(0, 0, 0, 0.32),
      0 0 0 1px rgba(255, 255, 255, 0.02) inset;
    --constellation-button-primary-background: rgba(240, 240, 250, 0.9);
    --constellation-button-primary-background-hover: rgba(255, 255, 255, 0.96);
    --constellation-button-primary-border: transparent;
    --constellation-button-primary-border-hover: transparent;
    --constellation-button-primary-text: rgba(9, 12, 18, 0.96);
    --constellation-button-primary-shadow: none;
    --constellation-button-quiet-background: transparent;
    --constellation-button-quiet-background-hover: rgba(240, 240, 250, 0.07);
    --constellation-button-quiet-border: transparent;
    --constellation-button-quiet-border-hover: transparent;
    --constellation-button-quiet-shadow: none;
    --constellation-button-destructive-shadow: none;

    position: absolute;
    display: grid;
    width: min(352px, calc(100vw - 28px));
    gap: 12px;
    padding: 12px;
    border: 1px solid var(--cortex-user-menu-border);
    border-radius: 16px;
    background: var(--cortex-user-menu-background);
    box-shadow: var(--cortex-user-menu-shadow);
    color: var(--constellation-color-text-primary);
    pointer-events: auto;
  }

  .cortex-user-menu-header,
  .cortex-user-menu-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .cortex-user-menu-header {
    min-width: 0;
  }

  .cortex-user-menu-header h2,
  .cortex-user-menu-eyebrow,
  .cortex-user-menu-validation {
    margin: 0;
  }

  .cortex-user-menu-header h2 {
    overflow: hidden;
    font-family: var(--constellation-font-sans);
    font-size: 16px;
    font-weight: 510;
    letter-spacing: 0;
    line-height: 1.2;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cortex-user-menu-eyebrow,
  .cortex-user-menu-label {
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .cortex-user-menu-body {
    display: grid;
    gap: 12px;
  }

  .cortex-user-menu-field {
    display: grid;
    gap: 7px;
  }

  .cortex-user-menu-row-label {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
  }

  .cortex-user-menu-hint,
  .cortex-user-menu-validation {
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.35;
  }

  .cortex-user-menu-validation {
    color: var(--constellation-control-pill-danger-text, #fecaca);
  }

  .cortex-user-menu-actions {
    justify-content: space-between;
    padding-top: 0;
  }

  .cortex-user-menu-action-group {
    display: inline-flex;
    align-items: center;
    gap: 8px;
  }

  :global(:root[data-color-scheme='light']) .cortex-user-menu {
    --cortex-user-menu-border: rgba(49, 63, 76, 0.12);
    --cortex-user-menu-background: rgba(255, 253, 247, 0.98);
    --cortex-user-menu-shadow: 0 18px 38px rgba(54, 70, 82, 0.13);
    --constellation-button-primary-background: rgba(33, 47, 61, 0.98);
    --constellation-button-primary-background-hover: rgba(42, 58, 73, 0.98);
    --constellation-button-primary-text: #fffdf7;
    --constellation-button-quiet-background-hover: rgba(49, 63, 76, 0.06);
  }

  .cortex-user-menu :global(.constellation-astre-palette) {
    gap: 8px;
  }

  .cortex-user-menu :global(.constellation-text-input),
  .cortex-user-menu :global(.constellation-button-secondary),
  .cortex-user-menu :global(.constellation-button-quiet) {
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .cortex-user-menu :global(.constellation-button::after) {
    display: none;
  }

  @media (max-width: 620px) {
    .cortex-user-menu {
      left: 14px !important;
      right: 14px;
      top: auto !important;
      bottom: 14px;
      width: auto;
    }
  }
</style>
