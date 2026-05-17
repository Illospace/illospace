<script lang="ts">
  import { browser } from '$app/environment';
  import { getContext, onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationEmptyState,
    ConstellationPageFrame,
    ConstellationPill,
    ConstellationPresenceSeed,
    ConstellationSection,
    ConstellationSkeletonBlock,
  } from '$lib/components/constellation';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalContext,
  } from '$lib/components/constellation/constellationPageFrameContext';
  import { DEFAULT_PROFILE_COLOR } from '$lib/features/cortex/components/menus/userProfilePalette';
  import { auth } from '$lib/stores/auth.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { buildPresenceSeedStyle, normalizeHexColor } from '$lib/utils/constellationPresence';
  import { parseServerDate } from '$lib/utils/datetime';

  interface TeamMember {
    id: number | string;
    name: string;
    email: string;
    role: string;
    color: string;
    created_at: string;
    approved: boolean;
    attribution_visible?: boolean;
  }

  interface TeamTokenUsage {
    user_id: string | null;
    runs: number;
    api_calls: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    cache_read: number;
    cache_write: number;
    estimated_cost: number;
    last_used_at: string | null;
  }

  interface TeamTokenAnalytics {
    window_days: number;
    generated_at: string;
    members: TeamTokenUsage[];
    unattributed: TeamTokenUsage;
    totals: TeamTokenUsage;
  }

  let members = $state<TeamMember[]>([]);
  let loading = $state(true);
  let tokenAnalytics = $state<TeamTokenAnalytics | null>(null);
  let tokenAnalyticsLoading = $state(true);

  let editingProfile = $state(false);
  let profileColor = $state(DEFAULT_PROFILE_COLOR);
  let profileAttribution = $state(true);
  let savingProfile = $state(false);

  let actionPending = $state<Record<string, boolean>>({});

  const workspacePageModalContext = getContext<ConstellationPageFrameModalContext | undefined>(
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
  );

  $effect(() => {
    return workspacePageModalContext?.registerRefreshAction({
      label: 'Refresh team',
      onclick: refreshTeam,
    });
  });

  const currentUserId = $derived(auth.user?.id ?? '');
  const approvedMembers = $derived.by(() => members.filter((member) => member.approved));
  const pendingMembers = $derived.by(() => members.filter((member) => !member.approved));
  const currentMember = $derived.by(
    () => approvedMembers.find((member) => String(member.id) === currentUserId) ?? null,
  );

  const tokenUsageByMember = $derived.by(() => {
    const usageMap = new Map<string, TeamTokenUsage>();
    for (const usage of tokenAnalytics?.members ?? []) {
      if (usage.user_id) usageMap.set(String(usage.user_id), usage);
    }
    return usageMap;
  });

  const maxMemberTokens = $derived.by(() =>
    Math.max(
      0,
      ...approvedMembers.map((member) => tokenUsageForMember(member).total_tokens || 0),
    ),
  );

  onMount(async () => {
    await Promise.all([loadMembers(), loadTokenAnalytics()]);
  });

  async function loadMembers() {
    loading = true;
    try {
      members = await api.listTeamMembers();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load team', 'error');
    } finally {
      loading = false;
    }
  }

  async function refreshTeam() {
    await Promise.all([loadMembers(), loadTokenAnalytics()]);
  }

  async function loadTokenAnalytics() {
    tokenAnalyticsLoading = true;
    try {
      tokenAnalytics = await api.teamTokenAnalytics(30);
    } catch (err: any) {
      tokenAnalytics = null;
      ui.toast(err.detail || 'Failed to load token analytics', 'error');
    } finally {
      tokenAnalyticsLoading = false;
    }
  }

  function inviteLink() {
    const params = new URLSearchParams({ view: 'register', mode: 'join' });
    if (auth.user?.org_slug) params.set('workspace', auth.user.org_slug);
    const path = `/login?${params.toString()}`;
    return browser ? `${window.location.origin}${path}` : path;
  }

  async function copyInviteLink() {
    const link = inviteLink();

    try {
      if (browser && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(link);
        ui.toast('Invite link copied', 'success');
        return;
      }
    } catch {
      // Fall through to the manual-copy prompt below.
    }

    if (browser) {
      window.prompt('Copy this invite link and send it to your teammate:', link);
      ui.toast('Invite link ready to share', 'info');
    }
  }

  function timeAgo(iso: string | undefined): string {
    if (!iso) return 'unknown';
    const createdAt = parseServerDate(iso);
    if (!createdAt) return 'unknown';
    const ms = Math.max(0, Date.now() - createdAt.getTime());
    const days = Math.floor(ms / 86400000);

    if (days < 1) {
      const hrs = Math.floor(ms / 3600000);
      if (hrs < 1) {
        const mins = Math.floor(ms / 60000);
        return mins < 1 ? 'just now' : `${mins}m ago`;
      }
      return `${hrs}h ago`;
    }

    if (days === 1) return 'yesterday';
    if (days < 30) return `${days}d ago`;

    const months = Math.floor(days / 30);
    return `${months}mo ago`;
  }

  function presenceSeedStyle(color?: string): string {
    return buildPresenceSeedStyle(color);
  }

  function emptyTokenUsage(userId: string | null = null): TeamTokenUsage {
    return {
      user_id: userId,
      runs: 0,
      api_calls: 0,
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      cache_read: 0,
      cache_write: 0,
      estimated_cost: 0,
      last_used_at: null,
    };
  }

  function tokenUsageForMember(member: TeamMember): TeamTokenUsage {
    const userId = String(member.id);
    return tokenUsageByMember.get(userId) ?? emptyTokenUsage(userId);
  }

  function tokenUsagePercent(usage: TeamTokenUsage): number {
    if (!maxMemberTokens || !usage.total_tokens) return 0;
    return Math.max(4, Math.min(100, (usage.total_tokens / maxMemberTokens) * 100));
  }

  function formatTokens(value: number | undefined): string {
    const n = Math.max(0, Number(value || 0));
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return Math.round(n).toLocaleString();
  }

  function formatCost(value: number | undefined): string {
    const n = Math.max(0, Number(value || 0));
    if (n === 0) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    if (n < 1) return `$${n.toFixed(3)}`;
    return `$${n.toFixed(2)}`;
  }

  function openProfileEdit(member: TeamMember | null = currentMember) {
    if (!member) {
      ui.toast('Your profile is not available yet.', 'error');
      return;
    }

    profileColor = normalizeHexColor(member.color) ?? DEFAULT_PROFILE_COLOR;
    profileAttribution = member.attribution_visible ?? true;
    editingProfile = true;
  }

  async function saveProfile() {
    savingProfile = true;
    try {
      await api.updateProfile({
        color: profileColor,
        attribution_visible: profileAttribution,
      });
      ui.toast('Profile updated', 'success');
      editingProfile = false;
      await loadMembers();
    } catch (err: any) {
      ui.toast(err.detail || 'Update failed', 'error');
    } finally {
      savingProfile = false;
    }
  }

  async function approveUser(userId: string) {
    actionPending[userId] = true;
    actionPending = { ...actionPending };

    try {
      await api.approveUser(userId);
      ui.toast('User approved', 'success');
      await loadMembers();
    } catch (err: any) {
      ui.toast(err.detail || 'Approve failed', 'error');
    } finally {
      delete actionPending[userId];
      actionPending = { ...actionPending };
    }
  }

  async function rejectUser(userId: string) {
    if (!confirm('Reject this user? They will not be able to access the team.')) return;

    actionPending[userId] = true;
    actionPending = { ...actionPending };

    try {
      await api.rejectUser(userId);
      ui.toast('User rejected', 'success');
      await loadMembers();
    } catch (err: any) {
      ui.toast(err.detail || 'Reject failed', 'error');
    } finally {
      delete actionPending[userId];
      actionPending = { ...actionPending };
    }
  }

  function isCurrentMember(member: TeamMember) {
    return String(member.id) === currentUserId;
  }

  function canApproveAccess() {
    return Boolean(currentMember);
  }

  function canRejectAccess() {
    return auth.user?.role === 'owner' || auth.user?.role === 'admin';
  }
</script>

<ConstellationPageFrame
  title="Team"
  subtitle="Manage members and usage."
>
  {#snippet actions()}
    {#if canApproveAccess()}
      <ConstellationButton variant="secondary" size="sm" onclick={copyInviteLink}>
        Invite member
      </ConstellationButton>
    {/if}
  {/snippet}

  {#if loading}
    <section class="team-loading-stack" aria-label="Team loading">
      <ConstellationSkeletonBlock variant="panel" height="68px" />
      <ConstellationSkeletonBlock variant="panel" height="280px" />
    </section>
  {:else if members.length === 0}
    <div class="team-empty">
      <ConstellationEmptyState
        title="No team members found."
        description="Invite a teammate to start building the roster."
      />
    </div>
  {:else}
    {#if pendingMembers.length}
      <ConstellationSection eyebrow="Access" title="Pending approval">
        <div class="team-pending-list">
          {#each pendingMembers as member (member.id)}
            <article class="team-pending-row">
              <div class="team-member-identity">
                <ConstellationPresenceSeed
                  label={member.name}
                  size="md"
                  treatment="plain"
                  style={presenceSeedStyle(member.color)}
                />
                <div class="team-member-copy">
                  <div class="team-member-name-row">
                    <h3>{member.name}</h3>
                    <ConstellationPill variant="warning">Pending</ConstellationPill>
                  </div>
                  <p>{member.email} · joined {timeAgo(member.created_at)}</p>
                </div>
              </div>

              <div class="team-pending-actions">
                {#if canRejectAccess()}
                  <ConstellationButton
                    variant="quiet"
                    size="sm"
                    disabled={actionPending[String(member.id)]}
                    onclick={() => rejectUser(String(member.id))}
                  >
                    Reject
                  </ConstellationButton>
                {/if}
                {#if canApproveAccess()}
                  <ConstellationButton
                    variant="secondary"
                    size="sm"
                    disabled={actionPending[String(member.id)]}
                    onclick={() => approveUser(String(member.id))}
                  >
                    {actionPending[String(member.id)] ? 'Working...' : 'Approve'}
                  </ConstellationButton>
                {/if}
              </div>
            </article>
          {/each}
        </div>
      </ConstellationSection>
    {/if}

    <ConstellationSection eyebrow="Roster" title="Members">
      {#snippet actions()}
        {#if tokenAnalyticsLoading}
          <ConstellationPill variant="muted">Loading tokens</ConstellationPill>
        {:else if tokenAnalytics}
          <ConstellationPill variant="muted">
            {tokenAnalytics.window_days}d · {formatTokens(tokenAnalytics.totals.total_tokens)} tokens · {formatCost(tokenAnalytics.totals.estimated_cost)}
          </ConstellationPill>
        {:else}
          <ConstellationPill variant="warning">Tokens unavailable</ConstellationPill>
        {/if}
      {/snippet}

      <div class="team-member-list" role="table" aria-label="Approved team members">
        <div class="team-member-header" role="row">
          <span role="columnheader">Member</span>
          <span role="columnheader">Role</span>
          <span role="columnheader">Activity</span>
          <span role="columnheader">Tokens</span>
          <span role="columnheader">Cost</span>
          <span role="columnheader">Last used</span>
        </div>

        {#each approvedMembers as member (member.id)}
          {@const tokenUsage = tokenUsageForMember(member)}
          <div class="team-member-row" role="row">
            <div class="team-member-cell team-member-identity" role="cell">
              {#if isCurrentMember(member)}
                <button
                  type="button"
                  class="team-avatar-button"
                  aria-label="Edit your profile"
                  onclick={() => openProfileEdit(member)}
                >
                  <ConstellationPresenceSeed
                    label={member.name}
                    size="md"
                    treatment="plain"
                    style={presenceSeedStyle(member.color)}
                  />
                </button>
              {:else}
                <ConstellationPresenceSeed
                  label={member.name}
                  size="md"
                  treatment="plain"
                  style={presenceSeedStyle(member.color)}
                />
              {/if}
              <div class="team-member-copy">
                <div class="team-member-name-row">
                  <h3>{member.name}</h3>
                  {#if isCurrentMember(member)}
                    <ConstellationPill variant="muted">You</ConstellationPill>
                  {/if}
                </div>
                <p>{member.email}</p>
              </div>
            </div>

            <div class="team-member-cell" role="cell">
              <span class="team-member-primary">{member.role}</span>
              <span class="team-member-secondary">joined {timeAgo(member.created_at)}</span>
            </div>

            <div class="team-member-cell team-member-number" role="cell">
              {#if tokenAnalyticsLoading}
                <span class="team-member-secondary">Loading</span>
              {:else if !tokenAnalytics}
                <span class="team-member-secondary">Unavailable</span>
              {:else}
                <span class="team-member-primary">{tokenUsage.runs.toLocaleString()} runs</span>
                <span class="team-member-secondary">{tokenUsage.api_calls.toLocaleString()} calls</span>
              {/if}
            </div>

            <div class="team-member-cell team-token-cell" role="cell">
              {#if tokenAnalyticsLoading}
                <span class="team-member-secondary">Loading</span>
              {:else if !tokenAnalytics}
                <span class="team-member-secondary">Unavailable</span>
              {:else}
                <div class="team-token-meter" aria-hidden="true">
                  <span
                    class="team-token-meter-fill"
                    style={`width: ${tokenUsagePercent(tokenUsage)}%`}
                  ></span>
                </div>
                <span class="team-member-primary">{formatTokens(tokenUsage.total_tokens)} tokens</span>
                <span class="team-member-secondary">
                  In {formatTokens(tokenUsage.input_tokens)} / Out {formatTokens(tokenUsage.output_tokens)}
                </span>
              {/if}
            </div>

            <div class="team-member-cell team-member-number" role="cell">
              {#if tokenAnalyticsLoading}
                <span class="team-member-secondary">Loading</span>
              {:else if !tokenAnalytics}
                <span class="team-member-secondary">Unavailable</span>
              {:else}
                <span class="team-member-primary">{formatCost(tokenUsage.estimated_cost)}</span>
              {/if}
            </div>

            <div class="team-member-cell" role="cell">
              {#if tokenAnalyticsLoading}
                <span class="team-member-secondary">Loading</span>
              {:else if !tokenAnalytics}
                <span class="team-member-secondary">Unavailable</span>
              {:else if tokenUsage.last_used_at}
                <span class="team-member-primary">{timeAgo(tokenUsage.last_used_at)}</span>
              {:else}
                <span class="team-member-secondary">No tracked usage</span>
              {/if}
            </div>
          </div>
        {/each}

        {#if !tokenAnalyticsLoading && tokenAnalytics?.unattributed?.total_tokens}
          <div class="team-member-row team-member-row-muted" role="row">
            <div class="team-member-cell team-member-identity" role="cell">
              <span class="team-system-mark" aria-hidden="true"></span>
              <div class="team-member-copy">
                <div class="team-member-name-row">
                  <h3>System / unattributed</h3>
                </div>
                <p>Usage without a member attribution.</p>
              </div>
            </div>
            <div class="team-member-cell" role="cell">
              <span class="team-member-secondary">System</span>
            </div>
            <div class="team-member-cell team-member-number" role="cell">
              <span class="team-member-primary">{tokenAnalytics.unattributed.runs.toLocaleString()} runs</span>
              <span class="team-member-secondary">{tokenAnalytics.unattributed.api_calls.toLocaleString()} calls</span>
            </div>
            <div class="team-member-cell team-token-cell" role="cell">
              <span class="team-member-primary">{formatTokens(tokenAnalytics.unattributed.total_tokens)} tokens</span>
            </div>
            <div class="team-member-cell team-member-number" role="cell">
              <span class="team-member-primary">{formatCost(tokenAnalytics.unattributed.estimated_cost)}</span>
            </div>
            <div class="team-member-cell" role="cell">
              <span class="team-member-secondary">Unassigned</span>
            </div>
          </div>
        {/if}
      </div>
    </ConstellationSection>

    {#if editingProfile}
      <section class="team-profile-editor" aria-label="Edit profile">
        <div class="team-profile-copy">
          <p class="team-profile-eyebrow">Profile</p>
          <h2 class="team-profile-title">Edit your presence</h2>
          <p class="team-profile-description">
            Choose your shared color and attribution preference.
          </p>
        </div>

        <div class="team-profile-fields">
          <label class="team-profile-field" for="profile-color">
            <span class="team-profile-label">Color</span>
            <div class="team-profile-color-row">
              <input
                id="profile-color"
                type="color"
                bind:value={profileColor}
                class="team-profile-color-input"
              />
              <span class="team-profile-color-value">{profileColor}</span>
              <ConstellationPresenceSeed
                label={currentMember?.name || auth.user?.name || 'You'}
                size="md"
                treatment="plain"
                style={presenceSeedStyle(profileColor)}
              />
            </div>
          </label>

          <label class="team-profile-field">
            <span class="team-profile-label">Attribution</span>
            <span class="team-profile-toggle">
              <input type="checkbox" bind:checked={profileAttribution} />
              <span>Show my name on contributions</span>
            </span>
          </label>
        </div>

        <div class="team-profile-actions">
          <ConstellationButton variant="quiet" size="sm" onclick={() => (editingProfile = false)}>
            Cancel
          </ConstellationButton>
          <ConstellationButton
            variant="secondary"
            size="sm"
            disabled={savingProfile}
            onclick={saveProfile}
          >
            {savingProfile ? 'Saving...' : 'Save'}
          </ConstellationButton>
        </div>
      </section>
    {/if}
  {/if}
</ConstellationPageFrame>

<style>
  .team-loading-stack {
    display: grid;
    gap: 14px;
  }

  .team-empty {
    padding-top: 18px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .team-pending-list,
  .team-member-list {
    display: grid;
    min-width: 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .team-pending-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
    padding: 14px 0;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .team-pending-actions {
    display: flex;
    flex: 0 0 auto;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
  }

  .team-member-header,
  .team-member-row {
    display: grid;
    grid-template-columns:
      minmax(250px, 1.45fr)
      minmax(112px, 0.62fr)
      minmax(108px, 0.58fr)
      minmax(210px, 1.08fr)
      minmax(86px, 0.44fr)
      minmax(116px, 0.58fr);
    gap: 16px;
    align-items: center;
    min-width: 900px;
  }

  .team-member-header {
    padding: 10px 0 8px;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    letter-spacing: 0.14em;
    line-height: 1.3;
    text-transform: uppercase;
  }

  .team-member-row {
    padding: 14px 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .team-member-row-muted {
    color: var(--constellation-color-text-tertiary);
  }

  .team-member-cell {
    display: grid;
    gap: 4px;
    min-width: 0;
    align-content: center;
  }

  .team-member-identity {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }

  .team-avatar-button {
    display: inline-grid;
    flex: 0 0 auto;
    width: 30px;
    height: 30px;
    place-items: center;
    padding: 0;
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }

  .team-avatar-button:hover {
    background: var(--constellation-surface-nested-background);
  }

  .team-avatar-button:focus-visible {
    outline: 2px solid var(--constellation-control-field-focus-border);
    outline-offset: 3px;
  }

  .team-system-mark {
    flex-shrink: 0;
    width: 28px;
    height: 28px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 999px;
    background: var(--constellation-surface-nested-background);
  }

  .team-member-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .team-member-name-row {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .team-member-copy h3 {
    margin: 0;
    overflow: hidden;
    color: var(--constellation-color-text-primary);
    font-size: 14px;
    font-weight: 560;
    line-height: 1.32;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .team-member-copy p {
    margin: 0;
    overflow: hidden;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .team-member-primary,
  .team-member-secondary {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .team-member-primary {
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    font-weight: 500;
    line-height: 1.35;
  }

  .team-member-secondary {
    color: var(--constellation-color-text-tertiary);
    font-size: 11px;
    line-height: 1.35;
  }

  .team-member-number .team-member-primary,
  .team-member-number .team-member-secondary {
    font-family: var(--constellation-font-mono);
  }

  .team-token-cell {
    gap: 5px;
  }

  .team-token-meter {
    position: relative;
    width: min(150px, 100%);
    height: 4px;
    overflow: hidden;
    border-radius: var(--constellation-radius-pill);
    background: color-mix(in srgb, var(--constellation-color-text-primary) 7%, transparent);
  }

  .team-token-meter-fill {
    position: absolute;
    inset: 0 auto 0 0;
    border-radius: inherit;
    background: color-mix(in srgb, var(--constellation-color-text-primary) 32%, transparent);
    box-shadow: none;
  }

  .team-profile-editor {
    display: grid;
    gap: 20px;
    padding-top: 18px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .team-profile-copy {
    display: grid;
    gap: 8px;
    max-width: 620px;
  }

  .team-profile-eyebrow {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .team-profile-title {
    margin: 0;
    color: var(--constellation-section-title);
    font-family: var(--constellation-font-sans);
    font-size: 16px;
    font-weight: 560;
    line-height: 1.3;
  }

  .team-profile-description {
    margin: 0;
    color: var(--constellation-section-description);
    font-size: 13px;
    line-height: 1.55;
  }

  .team-profile-fields {
    display: grid;
    gap: 16px;
  }

  .team-profile-field {
    display: grid;
    gap: 10px;
  }

  .team-profile-label {
    color: var(--constellation-label-meta);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .team-profile-color-row,
  .team-profile-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
  }

  .team-profile-color-input {
    width: 42px;
    height: 30px;
    padding: 0;
    border: 1px solid var(--constellation-control-field-border);
    border-radius: 10px;
    background: transparent;
    cursor: pointer;
  }

  .team-profile-color-value {
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .team-profile-toggle {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.4;
  }

  .team-profile-toggle input[type='checkbox'] {
    width: 16px;
    height: 16px;
    accent-color: var(--constellation-color-amber);
  }

  @media (max-width: 1020px) {
    .team-member-list {
      overflow-x: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--constellation-data-table-scrollbar) transparent;
    }
  }

  @media (max-width: 760px) {
    .team-pending-row {
      align-items: flex-start;
      flex-direction: column;
    }

    .team-pending-actions {
      justify-content: flex-start;
      padding-left: 40px;
    }
  }
</style>
