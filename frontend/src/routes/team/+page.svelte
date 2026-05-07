<script lang="ts">
  import { browser } from '$app/environment';
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationActionRow,
    ConstellationActivityFeed,
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
    ConstellationSplitLayout,
  } from '$lib/components/constellation';
  import { auth } from '$lib/stores/auth.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { buildPresenceSeedStyle } from '$lib/utils/constellationPresence';

  interface TeamMember {
    id: number;
    name: string;
    email: string;
    role: string;
    color: string;
    created_at: string;
    approved: boolean;
    attribution_visible?: boolean;
  }

  interface ActivityItem {
    user_id: string;
    user_name: string;
    skill_name: string | null;
    status: string;
    created_at: string;
    idea_title: string | null;
    type: string;
  }

  let members = $state<TeamMember[]>([]);
  let activity = $state<ActivityItem[]>([]);
  let loading = $state(true);
  let activityLoading = $state(true);

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

  const activityFeedItems = $derived.by(() =>
    activity.slice(0, 8).map((item) => {
      const member = memberForActivity(item);
      return {
        name: item.user_name,
        tone: 'spectral' as const,
        text: buildActivityText(item),
        at: timeAgo(item.created_at),
        seedStyle: presenceSeedStyle(member?.color),
        actorColor: member?.color || undefined,
      };
    }),
  );

  onMount(async () => {
    await Promise.all([loadMembers(), loadActivity()]);
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

  async function loadActivity() {
    activityLoading = true;
    try {
      activity = await api.teamActivity(48);
    } catch {
      activity = [];
    } finally {
      activityLoading = false;
    }
  }

  async function refreshTeam() {
    await Promise.all([loadMembers(), loadActivity()]);
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
    const ms = Date.now() - new Date(iso).getTime();
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

  function memberForActivity(item: ActivityItem) {
    return members.find(
      (member) => String(member.id) === item.user_id || member.name === item.user_name,
    );
  }

  function presenceSeedStyle(color?: string): string {
    return buildPresenceSeedStyle(color);
  }

  function buildActivityText(item: ActivityItem): string {
    const status = item.status ? ` (${item.status})` : '';

    if (item.skill_name && item.idea_title) {
      return `ran ${item.skill_name} on "${item.idea_title}"${status}.`;
    }

    if (item.skill_name) {
      return `ran ${item.skill_name}${status}.`;
    }

    if (item.idea_title) {
      return `worked on "${item.idea_title}"${status}.`;
    }

    return `${item.type.replace(/_/g, ' ')}${status}.`;
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

  function isOwner() {
    return true;
  }
</script>

<ConstellationPageFrame
  eyebrow="Constellation Team"
  title="Team"
  subtitle={loading ? 'Loading roster, approvals, and recent collaboration.' : `${approvedMembers.length} active member${approvedMembers.length === 1 ? '' : 's'}${pendingMembers.length ? ` · ${pendingMembers.length} pending approval` : ''}`}
>
  {#snippet actions()}
    {#if isOwner()}
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
      <ConstellationSkeletonBlock variant="panel" height="240px" />
    </section>
  {:else if members.length === 0}
    <ConstellationPanel>
      <ConstellationEmptyState
        title="No team members found."
        description="Once people join the workspace, approvals, roster details, and collaboration activity will appear here."
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
        description="Owners can approve new access requests without leaving the shared workspace."
      >
        <div class="team-row-stack">
          {#each pendingMembers as member (member.id)}
            <ConstellationActionRow
              title={member.name}
              description={memberSubtitle(member)}
              tone="warning"
              meta="Awaiting owner review"
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
                {#if isOwner()}
                  <ConstellationButton
                    variant="quiet"
                    size="sm"
                    disabled={actionPending[String(member.id)]}
                    onclick={() => rejectUser(String(member.id))}
                  >
                    Reject
                  </ConstellationButton>
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

    <ConstellationSplitLayout>
      <ConstellationSection
        eyebrow="Roster"
        title="Members"
        description="Approved people with their current workspace presence color and access role."
      >
        <div class="team-row-stack">
          {#each approvedMembers as member (member.id)}
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
            </ConstellationActionRow>
          {/each}
        </div>
      </ConstellationSection>

      <ConstellationSection
        eyebrow="Presence"
        title="Recent collaboration"
        description="Latest shared activity across the workspace in the last 48 hours."
      >
        {#if activityLoading}
          <ConstellationSkeletonBlock variant="panel" height="220px" />
        {:else if activityFeedItems.length === 0}
          <ConstellationEmptyState
            size="sm"
            title="No recent activity."
            description="New runs, reviews, and collaboration updates will show up here as the team works."
          />
        {:else}
          <ConstellationActivityFeed items={activityFeedItems} />
        {/if}
      </ConstellationSection>
    </ConstellationSplitLayout>

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
    accent-color: rgba(213, 161, 77, 0.92);
  }
</style>
