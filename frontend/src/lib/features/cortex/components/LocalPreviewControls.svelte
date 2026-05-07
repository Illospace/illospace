<script lang="ts">
  import { browser, dev } from '$app/environment';
  import { onDestroy, onMount } from 'svelte';
  import { auth } from '$lib/stores/auth.svelte';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import type { CortexWorkspacePoint } from '$lib/features/workspace-scene/domain/workspacePoint';
  import {
    LOCAL_PREVIEW_APP_KIND,
    LOCAL_PREVIEW_APP_SEEDS,
    LOCAL_PREVIEW_MEMBER_SEEDS,
    LOCAL_PREVIEW_STORAGE_KEY,
    LOCAL_PREVIEW_TEXT_LAB_ID,
    buildLocalPreviewAnchorMember,
    buildLocalPreviewIdeas,
    buildLocalPreviewMembers,
    clampLocalPreviewValue,
    isLocalPreviewDummyApp,
    isLocalPreviewIdeaId,
    isLocalPreviewMemberId,
    previewIdeaSignature,
    previewMemberSignature,
    stripLocalPreviewIdeas,
    stripLocalPreviewMembers,
  } from '$lib/utils/cortexLocalPreview';

  let {
    workspaceContext = null,
    activeWorkspaceAppId = null,
    onActiveWorkspaceAppIdChange,
  }: {
    workspaceContext?: CortexWorkspacePoint | null;
    activeWorkspaceAppId?: string | null;
    onActiveWorkspaceAppIdChange?: (appId: string | null) => void;
  } = $props();

  let localPreviewEnabled = $state(false);
  let localPreviewUserCount = $state(1);
  let localPreviewBlobCount = $state(2);
  let localPreviewPanelOpen = $state(false);
  let localPreviewSettingsLoaded = $state(false);
  let localPreviewAppSaving = $state(false);

  const localPreviewAvailable = $derived.by(() => {
    if (!browser || !dev) return false;
    const hostname = window.location.hostname;
    return hostname === 'localhost' || hostname === '127.0.0.1';
  });
  const localPreviewApps = $derived(workspaceApps.visibleApps.filter(isLocalPreviewDummyApp));
  const localPreviewTextLabIdea = $derived(cortex.ideas.find((idea) => idea.id === LOCAL_PREVIEW_TEXT_LAB_ID));

  function stepLocalPreviewUsers(delta: number) {
    localPreviewUserCount = clampLocalPreviewValue(localPreviewUserCount + delta, 0, LOCAL_PREVIEW_MEMBER_SEEDS.length);
  }

  function stepLocalPreviewBlobs(delta: number) {
    localPreviewBlobCount = clampLocalPreviewValue(localPreviewBlobCount + delta, 0, 5);
  }

  function localPreviewAppSeed(index: number) {
    return LOCAL_PREVIEW_APP_SEEDS[index % LOCAL_PREVIEW_APP_SEEDS.length];
  }

  async function handleAddLocalPreviewApp() {
    if (!auth.user?.id) {
      ui.toast('Sign in before adding preview apps', 'info');
      return;
    }

    localPreviewAppSaving = true;
    try {
      const index = localPreviewApps.length;
      const seed = localPreviewAppSeed(index);
      const suffix = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
      const baseX = workspaceContext?.worldX ?? 0;
      const baseY = workspaceContext?.worldY ?? 0;
      const angle = -Math.PI / 2 + (index % 6) * 0.72;
      const radius = 250 + Math.floor(index / 6) * 120;
      const positionX = baseX + Math.cos(angle) * radius;
      const positionY = baseY + Math.sin(angle) * radius;
      await workspaceApps.create({
        key: `local-preview-orbit-${seed.key}-${suffix}`,
        name: seed.name,
        description: 'Local preview app for Cortex orbit drag/drop testing.',
        renderer_key: 'generated-ui-app',
        source_kind: 'json',
        source_code: JSON.stringify({
          schema_version: 1,
          title: seed.name,
          description: 'Local preview workspace app for free-position drag testing.',
          views: [
            {
              id: 'preview-metrics',
              type: 'metrics',
              title: 'Preview state',
              metrics: [
                { label: seed.label, value: seed.metric },
                { label: 'Placement', value: 'Free' },
              ],
            },
            {
              id: 'preview-list',
              type: 'list',
              title: 'Behavior',
              rows: [
                { title: 'Drag the thumbnail anywhere in the workspace.' },
                { title: 'Drop it on the bin to archive and restore it later.' },
              ],
              columns: [{ key: 'title', label: 'Instruction' }],
            },
          ],
        }),
        manifest: {
          contract_version: 1,
          state_key: 'default',
          data_plan: { mode: 'app_local', scope: 'ui_state' },
          design_contract: {
            kit: 'constellation-app-kit',
            theme_modes: ['dark', 'light'],
          },
        },
        visual_spec: {
          accent: seed.accent,
          placement: 'free',
          position_x: positionX,
          position_y: positionY,
          thumbnail: {
            label: seed.name,
            value: seed.metric,
            unit: seed.label,
            secondary: 'Preview',
          },
          preview: {
            title: seed.name,
            primary_value: seed.metric,
            primary_unit: seed.label,
          },
        },
        metadata: {
          local_preview_kind: LOCAL_PREVIEW_APP_KIND,
        },
        anchor_user_id: auth.user.id,
        initial_state: {
          count: Number(seed.metric),
          label: seed.label,
        },
      });
      ui.toast('Dummy app added', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to add dummy app', 'error');
    } finally {
      localPreviewAppSaving = false;
    }
  }

  async function handleClearLocalPreviewApps() {
    if (!localPreviewApps.length) return;

    localPreviewAppSaving = true;
    try {
      await Promise.all(localPreviewApps.map((app) => workspaceApps.archive(app.id)));
      if (activeWorkspaceAppId && !workspaceApps.appById(activeWorkspaceAppId)) {
        onActiveWorkspaceAppIdChange?.(null);
      }
      ui.toast('Dummy apps cleared', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to clear dummy apps', 'error');
    } finally {
      localPreviewAppSaving = false;
    }
  }

  async function handleOpenTextLab() {
    if (!localPreviewEnabled || !localPreviewTextLabIdea) return;
    await cortex.selectIdea(LOCAL_PREVIEW_TEXT_LAB_ID);
  }

  $effect(() => {
    const members = cortex.teamMembers;
    const ideas = cortex.ideas;
    const realMembers = stripLocalPreviewMembers(members);
    const realIdeas = stripLocalPreviewIdeas(ideas);

    if (!localPreviewAvailable || !localPreviewSettingsLoaded || !localPreviewEnabled) {
      if (members.length !== realMembers.length) {
        cortex.teamMembers = realMembers;
        return;
      }

      if (ideas.length !== realIdeas.length) {
        cortex.ideas = realIdeas;
      }
      return;
    }

    const previewBaseMembers = realMembers.length
      ? realMembers
      : (() => {
          const anchorMember = buildLocalPreviewAnchorMember(auth.user);
          return anchorMember ? [anchorMember] : [];
        })();
    if (!previewBaseMembers.length) return;

    const previewMembers = buildLocalPreviewMembers(auth.user?.color ?? previewBaseMembers[0]?.color, localPreviewUserCount);
    const currentPreviewMembers = members.filter((member) => isLocalPreviewMemberId(member?.id));
    const nextMembers = [...previewBaseMembers, ...previewMembers];

    if (
      members.length !== nextMembers.length ||
      previewMemberSignature(currentPreviewMembers) !== previewMemberSignature(previewMembers)
    ) {
      cortex.teamMembers = nextMembers;
      return;
    }

    const previewIdeas = buildLocalPreviewIdeas(previewMembers, localPreviewBlobCount);
    const currentPreviewIdeas = ideas.filter((idea) => isLocalPreviewIdeaId(idea?.id));

    if (
      ideas.length !== realIdeas.length + previewIdeas.length ||
      previewIdeaSignature(currentPreviewIdeas) !== previewIdeaSignature(previewIdeas)
    ) {
      cortex.ideas = [...realIdeas, ...previewIdeas];
    }
  });

  $effect(() => {
    if (!browser || !localPreviewSettingsLoaded || !localPreviewAvailable) return;

    localStorage.setItem(
      LOCAL_PREVIEW_STORAGE_KEY,
      JSON.stringify({
        enabled: localPreviewEnabled,
        userCount: localPreviewUserCount,
        blobCount: localPreviewBlobCount,
      }),
    );
  });

  onMount(() => {
    if (localPreviewAvailable) {
      try {
        const raw = localStorage.getItem(LOCAL_PREVIEW_STORAGE_KEY);
        if (raw) {
          const parsed = JSON.parse(raw) as {
            enabled?: boolean;
            userCount?: number;
            blobCount?: number;
          };
          localPreviewEnabled = parsed.enabled ?? localPreviewEnabled;
          localPreviewUserCount = clampLocalPreviewValue(
            parsed.userCount ?? localPreviewUserCount,
            0,
            LOCAL_PREVIEW_MEMBER_SEEDS.length,
          );
          localPreviewBlobCount = clampLocalPreviewValue(parsed.blobCount ?? localPreviewBlobCount, 0, 5);
        }
      } catch {
        // Best-effort dev-only settings restore.
      }
    }
    localPreviewSettingsLoaded = true;
  });

  onDestroy(() => {
    cortex.teamMembers = stripLocalPreviewMembers(cortex.teamMembers);
    cortex.ideas = stripLocalPreviewIdeas(cortex.ideas);
  });
</script>

{#if localPreviewAvailable}
  <div class="cortex-local-preview">
    <button
      type="button"
      class="cortex-local-preview__toggle"
      onclick={() => (localPreviewPanelOpen = !localPreviewPanelOpen)}
    >
      Preview {localPreviewEnabled ? `${localPreviewUserCount}u / ${localPreviewBlobCount}b` : 'off'}
    </button>

    {#if localPreviewPanelOpen}
      <div class="cortex-local-preview__panel">
        <label class="cortex-local-preview__switch">
          <input type="checkbox" bind:checked={localPreviewEnabled} />
          <span>Enable local users</span>
        </label>

        <div class="cortex-local-preview__row">
          <span class="cortex-local-preview__label">Users</span>
          <div class="cortex-local-preview__stepper">
            <button type="button" onclick={() => stepLocalPreviewUsers(-1)} aria-label="Decrease preview users">-</button>
            <strong>{localPreviewUserCount}</strong>
            <button type="button" onclick={() => stepLocalPreviewUsers(1)} aria-label="Increase preview users">+</button>
          </div>
        </div>

        <div class="cortex-local-preview__row">
          <span class="cortex-local-preview__label">Blobs each</span>
          <div class="cortex-local-preview__stepper">
            <button type="button" onclick={() => stepLocalPreviewBlobs(-1)} aria-label="Decrease preview blobs">-</button>
            <strong>{localPreviewBlobCount}</strong>
            <button type="button" onclick={() => stepLocalPreviewBlobs(1)} aria-label="Increase preview blobs">+</button>
          </div>
        </div>

        <div class="cortex-local-preview__section">
          <div class="cortex-local-preview__row">
            <span class="cortex-local-preview__label">Text lab</span>
            <button
              type="button"
              class="cortex-local-preview__inline-action"
              onclick={handleOpenTextLab}
              disabled={!localPreviewEnabled || !localPreviewTextLabIdea}
            >
              Open
            </button>
          </div>
        </div>

        <div class="cortex-local-preview__section">
          <div class="cortex-local-preview__row">
            <span class="cortex-local-preview__label">Dummy apps</span>
            <strong class="cortex-local-preview__count">{localPreviewApps.length}</strong>
          </div>
          <div class="cortex-local-preview__actions">
            <button type="button" onclick={handleAddLocalPreviewApp} disabled={localPreviewAppSaving}>
              Add app
            </button>
            <button
              type="button"
              onclick={handleClearLocalPreviewApps}
              disabled={localPreviewAppSaving || localPreviewApps.length === 0}
            >
              Clear
            </button>
          </div>
        </div>
      </div>
    {/if}
  </div>
{/if}

<style>
  .cortex-local-preview {
    position: absolute;
    right: 20px;
    bottom: 78px;
    z-index: 32;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 10px;
    pointer-events: auto;
  }

  .cortex-local-preview__toggle,
  .cortex-local-preview__stepper button,
  .cortex-local-preview__inline-action,
  .cortex-local-preview__actions button {
    border: 1px solid var(--constellation-control-surface-border);
    background: var(--constellation-control-surface-background);
    color: var(--constellation-color-text-secondary);
    box-shadow: var(--constellation-control-surface-shadow);
    backdrop-filter: blur(12px) saturate(1.06);
    -webkit-backdrop-filter: blur(12px) saturate(1.06);
  }

  .cortex-local-preview__toggle {
    min-height: 30px;
    padding: 0 12px;
    border-radius: 999px;
    font-size: 11px;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  .cortex-local-preview__panel {
    width: 214px;
    padding: 12px;
    border-radius: 16px;
    border: 1px solid var(--constellation-surface-floating-border);
    background: var(--constellation-surface-floating-background);
    color: var(--constellation-color-text-secondary);
    box-shadow: var(--constellation-surface-floating-shadow);
    backdrop-filter: blur(16px) saturate(1.08);
    -webkit-backdrop-filter: blur(16px) saturate(1.08);
  }

  .cortex-local-preview__switch {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
    font-size: 12px;
  }

  .cortex-local-preview__switch input {
    accent-color: var(--constellation-color-spectral);
  }

  .cortex-local-preview__row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .cortex-local-preview__row + .cortex-local-preview__row {
    margin-top: 10px;
  }

  .cortex-local-preview__section {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
  }

  .cortex-local-preview__label {
    font-size: 11px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--constellation-color-text-muted);
  }

  .cortex-local-preview__stepper {
    display: inline-flex;
    align-items: center;
    gap: 10px;
  }

  .cortex-local-preview__stepper strong {
    min-width: 18px;
    text-align: center;
    font-size: 13px;
    font-weight: 600;
  }

  .cortex-local-preview__count {
    min-width: 18px;
    text-align: right;
    font-size: 13px;
    font-weight: 650;
  }

  .cortex-local-preview__stepper button {
    width: 24px;
    height: 24px;
    border-radius: 999px;
    font-size: 14px;
    line-height: 1;
  }

  .cortex-local-preview__actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    margin-top: 10px;
  }

  .cortex-local-preview__actions button {
    min-height: 28px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 650;
  }

  .cortex-local-preview__inline-action {
    min-height: 26px;
    padding: 0 10px;
    border-radius: 999px;
    font-size: 11px;
    font-weight: 650;
  }

  .cortex-local-preview__inline-action:disabled,
  .cortex-local-preview__actions button:disabled {
    cursor: default;
    opacity: 0.48;
  }

  @media (max-width: 900px) {
    .cortex-local-preview {
      right: 12px;
      bottom: 18px;
    }

    .cortex-local-preview__panel {
      width: 196px;
    }
  }
</style>
