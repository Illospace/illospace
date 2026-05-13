<script lang="ts">
  import { browser } from '$app/environment';
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationActionRow,
    ConstellationButton,
    ConstellationCallout,
    ConstellationEmptyState,
    ConstellationPageFrame,
    ConstellationPanel,
    ConstellationPill,
    ConstellationPresenceSeed,
    ConstellationPresenceStack,
    ConstellationSection,
    ConstellationSkeletonBlock,
  } from '$lib/components/constellation';
  import { auth } from '$lib/stores/auth.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { buildPresenceSeedStyle } from '$lib/utils/constellationPresence';
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
  let profileColor = $state('#6d46d9');
  let profileAttribution = $state(true);
  let savingProfile = $state(false);

  let actionPending = $state<Record<string, boolean>>({});

  const currentUserId = $derived(auth.user?.id ?? '');
  const approvedMembers = $derived.by(() => members.filter((member) => member.approved));
  const pendingMembers = $derived.by(() => members.filter((member) => !member.approved));
  const currentMember = $derived.by(
    () => approvedMembers.find((member) => String(member.id) === currentUserId) ?? null,
  );

  const activePresenceMembers = $derived.by(() =>
    approvedMembers.slice(0, 4).map((member) => ({
      name: member.name,
      tone: 'spectral' as const,
      style: presenceSeedStyle(member.color),
    })),
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

    profileColor = member.color || '#6366f1';
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

  function memberSubtitle(member: TeamMember) {
    const pieces = [member.email, `joined ${timeAgo(member.created_at)}`];
    return pieces.join(' · ');
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
  eyebrow="Constellation Team"
  title="Team"
  subtitle={loading ? 'Loading roster and approvals.' : `${approvedMembers.length} active member${approvedMembers.length === 1 ? '' : 's'}${pendingMembers.length ? ` · ${pendingMembers.length} pending approval` : ''}`}
>
  {#snippet actions()}
    {#if canApproveAccess()}
      <ConstellationButton variant="primary" size="sm" onclick={copyInviteLink}>
        Invite member
      </ConstellationButton>
    {/if}
    <ConstellationButton variant="quiet" size="sm" onclick={refreshTeam}>Refresh</ConstellationButton>
    {#if currentMember}
      <ConstellationButton variant="secondary" size="sm" onclick={() => openProfileEdit()}>
        Edit profile
      </ConstellationButton>
    {/if}
  {/snippet}

  {#if loading}
    <section class="team-loading-stack" aria-label="Team loading">
      <ConstellationSkeletonBlock variant="panel" height="120px" />
      <ConstellationSkeletonBlock variant="panel" height="280px" />
    </section>
  {:else if members.length === 0}
    <ConstellationPanel>
      <ConstellationEmptyState
        title="No team members found."
        description="Once people join the workspace, approvals and roster details will appear here."
      />
    </ConstellationPanel>
  {:else}
    {#if approvedMembers.length || pendingMembers.length}
      <ConstellationCallout
        title={pendingMembers.length ? `${pendingMembers.length} ${pendingMembers.length === 1 ? 'person is' : 'people are'} waiting for approval` : `${approvedMembers.length} active ${approvedMembers.length === 1 ? 'member' : 'members'} in the workspace`}
        text={`${approvedMembers.length} member${approvedMembers.length === 1 ? '' : 's'} are active across workspace and thread right now.`}
      >
        {#snippet actions()}
          {#if activePresenceMembers.length}
            <ConstellationPresenceStack
              members={activePresenceMembers}
              caption={`${approvedMembers.length} active now`}
            />
          {/if}
          {#if pendingMembers.length}
            <ConstellationPill variant="thinking">{pendingMembers.length} pending</ConstellationPill>
          {/if}
        {/snippet}
      </ConstellationCallout>
    {/if}

    {#if pendingMembers.length}
      <ConstellationSection
        eyebrow="Pending approval"
        title="Approval queue"
        description="Any active workspace member can approve new access requests."
      >
        <div class="team-row-stack">
          {#each pendingMembers as member (member.id)}
            <ConstellationActionRow
              title={member.name}
              description={memberSubtitle(member)}
              tone="warning"
              meta="Awaiting member review"
            >
              {#snippet leading()}
                <ConstellationPresenceSeed
                  label={member.name}
                  size="md"
                  style={presenceSeedStyle(member.color)}
                />
              {/snippet}

              {#snippet badge()}
                <ConstellationPill variant="warning">Pending</ConstellationPill>
              {/snippet}

              {#snippet actions()}
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
              {/snippet}
            </ConstellationActionRow>
          {/each}
        </div>
      </ConstellationSection>
    {/if}

    <ConstellationSection
      eyebrow="Roster"
      title="Members"
      description="Approved people with their current presence color, access role, and run-linked token usage."
    >
      {#snippet actions()}
        {#if tokenAnalyticsLoading}
          <ConstellationPill variant="muted">Loading tokens</ConstellationPill>
        {:else if tokenAnalytics}
          <ConstellationPill variant="info">
            {formatTokens(tokenAnalytics.totals.total_tokens)} tokens · {formatCost(tokenAnalytics.totals.estimated_cost)} · {tokenAnalytics.window_days}d
          </ConstellationPill>
        {:else}
          <ConstellationPill variant="warning">Tokens unavailable</ConstellationPill>
        {/if}
      {/snippet}

      <div class="team-row-stack">
        {#each approvedMembers as member (member.id)}
          {@const tokenUsage = tokenUsageForMember(member)}
          <ConstellationActionRow
            title={member.name}
            description={memberSubtitle(member)}
            tone="default"
            meta={member.role}
          >
            {#snippet leading()}
              <ConstellationPresenceSeed
                label={member.name}
                size="md"
                style={presenceSeedStyle(member.color)}
              />
            {/snippet}

            {#snippet badge()}
              <ConstellationPill variant={member.role === 'owner' ? 'info' : 'muted'}>
                {isCurrentMember(member) ? 'You' : member.role}
              </ConstellationPill>
            {/snippet}

            {#snippet actions()}
              {#if isCurrentMember(member)}
                <ConstellationButton
                  variant="quiet"
                  size="sm"
                  onclick={() => openProfileEdit(member)}
                >
                  Edit
                </ConstellationButton>
              {/if}
            {/snippet}

            {#snippet supporting()}
              <div class="team-token-analytics">
                {#if tokenAnalyticsLoading}
                  <span class="team-token-muted">Loading token analytics...</span>
                {:else if !tokenAnalytics}
                  <span class="team-token-muted">Token analytics unavailable</span>
                {:else}
                  <div class="team-token-meter" aria-hidden="true">
                    <span
                      class="team-token-meter-fill"
                      style={`width: ${tokenUsagePercent(tokenUsage)}%`}
                    ></span>
                  </div>
                  <div class="team-token-metrics" aria-label={`Token analytics for ${member.name}`}>
                    <span class="team-token-metric team-token-metric-primary">
                      {formatTokens(tokenUsage.total_tokens)} tokens
                    </span>
                    <span class="team-token-metric">In {formatTokens(tokenUsage.input_tokens)}</span>
                    <span class="team-token-metric">Out {formatTokens(tokenUsage.output_tokens)}</span>
                    <span class="team-token-metric">{tokenUsage.runs.toLocaleString()} runs</span>
                    <span class="team-token-metric">{tokenUsage.api_calls.toLocaleString()} calls</span>
                    {#if tokenUsage.cache_read || tokenUsage.cache_write}
                      <span class="team-token-metric">Cache {formatTokens(tokenUsage.cache_read)} read</span>
                    {/if}
                    <span class="team-token-metric">{formatCost(tokenUsage.estimated_cost)}</span>
                    {#if tokenUsage.last_used_at}
                      <span class="team-token-muted">last {timeAgo(tokenUsage.last_used_at)}</span>
                    {:else}
                      <span class="team-token-muted">no tracked usage</span>
                    {/if}
                  </div>
                {/if}
              </div>
            {/snippet}
          </ConstellationActionRow>
        {/each}
      </div>
    </ConstellationSection>

    {#if !tokenAnalyticsLoading && tokenAnalytics?.unattributed?.total_tokens}
      <ConstellationPanel>
        <div class="team-token-unattributed">
          <div>
            <p class="team-token-unattributed-eyebrow">System / unattributed</p>
            <p class="team-token-unattributed-title">
              {formatTokens(tokenAnalytics.unattributed.total_tokens)} tokens could not be assigned to a member.
            </p>
          </div>
          <div class="team-token-unattributed-meta">
            {tokenAnalytics.unattributed.runs.toLocaleString()} runs · {tokenAnalytics.unattributed.api_calls.toLocaleString()} calls · {formatCost(tokenAnalytics.unattributed.estimated_cost)}
          </div>
        </div>
      </ConstellationPanel>
    {/if}

    {#if editingProfile}
      <ConstellationPanel tone="info">
        <div class="team-profile-editor">
          <div class="team-profile-copy">
            <p class="team-profile-eyebrow">Profile</p>
            <h2 class="team-profile-title">Edit your presence</h2>
            <p class="team-profile-description">
              Choose the color used for your seed across shared surfaces and whether your name appears on contributions.
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
              {savingProfile ? 'Saving…' : 'Save'}
            </ConstellationButton>
          </div>
        </div>
      </ConstellationPanel>
    {/if}
  {/if}
</ConstellationPageFrame>

<style>
  .team-loading-stack,
  .team-row-stack {
    display: grid;
    gap: 18px;
  }

  .team-profile-editor {
    display: grid;
    gap: 22px;
  }

  .team-token-analytics {
    display: grid;
    gap: 8px;
    width: 100%;
    min-width: 0;
  }

  .team-token-meter {
    position: relative;
    width: min(420px, 100%);
    height: 5px;
    overflow: hidden;
    border-radius: var(--constellation-radius-pill);
    background: color-mix(in srgb, var(--constellation-color-text-primary) 8%, transparent);
  }

  .team-token-meter-fill {
    position: absolute;
    inset: 0 auto 0 0;
    border-radius: inherit;
    background: linear-gradient(
      90deg,
      color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 78%, transparent),
      color-mix(in srgb, var(--constellation-color-blue, #8DB7FF) 84%, transparent)
    );
    box-shadow: 0 0 14px color-mix(in srgb, var(--constellation-color-blue, #8DB7FF) 24%, transparent);
  }

  .team-token-metrics {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
    min-width: 0;
  }

  .team-token-metric,
  .team-token-muted {
    color: rgba(240, 240, 250, 0.58);
    font-size: 11px;
    line-height: 1.35;
    white-space: nowrap;
  }

  .team-token-metric {
    padding: 3px 7px;
    border-radius: var(--constellation-radius-pill);
    border: 1px solid rgba(240, 240, 250, 0.08);
    background: rgba(255, 255, 255, 0.035);
    font-family: var(--constellation-font-mono);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .team-token-metric-primary {
    color: rgba(255, 255, 255, 0.86);
    border-color: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 30%, transparent);
    background: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 10%, transparent);
  }

  .team-token-unattributed {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  .team-token-unattributed-eyebrow,
  .team-token-unattributed-title {
    margin: 0;
  }

  .team-token-unattributed-eyebrow {
    color: rgba(240, 240, 250, 0.52);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .team-token-unattributed-title {
    margin-top: 6px;
    color: rgba(255, 255, 255, 0.84);
    font-size: 13px;
    line-height: 1.45;
  }

  .team-token-unattributed-meta {
    flex-shrink: 0;
    color: rgba(240, 240, 250, 0.58);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .team-profile-copy {
    display: grid;
    gap: 8px;
    max-width: 620px;
  }

  .team-profile-eyebrow {
    margin: 0;
    color: rgba(240, 240, 250, 0.56);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .team-profile-title {
    margin: 0;
    color: rgba(255, 255, 255, 0.96);
    font-family: var(--constellation-font-sans);
    font-size: 16px;
    font-weight: 560;
    line-height: 1.3;
  }

  .team-profile-description {
    margin: 0;
    color: rgba(240, 240, 250, 0.54);
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
    color: rgba(240, 240, 250, 0.6);
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
    border: 1px solid rgba(240, 240, 250, 0.14);
    border-radius: 10px;
    background: transparent;
    cursor: pointer;
  }

  .team-profile-color-value {
    color: rgba(240, 240, 250, 0.68);
    font-family: var(--constellation-font-mono);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .team-profile-toggle {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    color: rgba(240, 240, 250, 0.72);
    font-size: 13px;
    line-height: 1.4;
  }

  .team-profile-toggle input[type='checkbox'] {
    width: 16px;
    height: 16px;
    accent-color: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 92%, transparent);
  }

  @media (max-width: 760px) {
    .team-token-unattributed {
      align-items: flex-start;
      flex-direction: column;
    }

    .team-token-unattributed-meta {
      white-space: normal;
    }
  }
</style>
