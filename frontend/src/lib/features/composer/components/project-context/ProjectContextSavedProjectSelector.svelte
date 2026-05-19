<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import { auth } from '$lib/stores/auth.svelte';
  import {
    projectAccessInitial,
    projectAccessMemberKey,
    projectAccessMemberName,
    summarizeProjectAccess,
    type ProjectAccessMember,
  } from '$lib/utils/projectProfileAccess';
  import type { ProjectContextProfile } from './projectContextProfiles';

  let {
    profiles = [],
    selectedProfileId = $bindable('none'),
    selectedProfile,
    onCreate,
    onSelect,
    loading = false,
  }: {
    profiles?: ProjectContextProfile[];
    selectedProfileId?: string;
    selectedProfile?: ProjectContextProfile;
    onCreate?: () => void;
    onSelect?: () => void;
    loading?: boolean;
  } = $props();

  let query = $state('');

  const filteredProfiles = $derived.by(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return profiles;

    return profiles.filter((profile) => {
      const searchable = [
        profile.name,
        profile.description,
        profile.id === 'none' ? '' : profile.visibility === 'public' ? 'public' : 'private',
        ...(profile.id === 'none' ? [] : (profile.access ?? []).map(projectAccessMemberName)),
        ...profile.resources.flatMap((resource) => [
          resource.label,
          resource.name,
          resource.path,
          resource.repo,
          resource.uri,
        ]),
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();

      return searchable.includes(needle);
    });
  });

  function selectProject(profile: ProjectContextProfile) {
    selectedProfileId = profile.id;
    onSelect?.();
  }

  function profileMeta(profile: ProjectContextProfile): string {
    if (profile.id === 'none') return profile.description;
    return profile.description || `${profile.resources.length} resource${profile.resources.length === 1 ? '' : 's'}`;
  }

  function profileOwner(profile: ProjectContextProfile): ProjectAccessMember | null {
    const currentUser = auth.user;
    if (!currentUser || !profile.userId || String(profile.userId) !== String(currentUser.id)) return null;
    return {
      user_id: String(currentUser.id),
      name: currentUser.name,
      email: currentUser.email,
    };
  }
</script>

<div class="project-context-search-control">
  <ConstellationIcon name="search" size={16} stroke={1.8} />
  <input
    aria-label="Search projects"
    placeholder="Search saved projects"
    bind:value={query}
  />
</div>

<div class="project-context-profile-list" role="listbox" aria-label="Saved projects">
  {#if loading}
    <div class="project-context-muted compact">Loading projects...</div>
  {:else if filteredProfiles.length}
    {#each filteredProfiles as profile}
      {@const isSelected = profile.id === selectedProfile?.id || profile.id === selectedProfileId}
      {@const accessSummary = summarizeProjectAccess(profile, undefined, profileOwner(profile))}
      <button
        type="button"
        class="project-context-profile-option"
        class:selected={isSelected}
        role="option"
        aria-selected={isSelected}
        onclick={() => selectProject(profile)}
      >
        <ConstellationIcon
          name={profile.id === 'none' ? 'folder' : profile.visibility === 'public' ? 'team' : 'lock'}
          size={17}
          stroke={1.8}
        />
        <span class="project-context-profile-option-copy">
          <span class="project-context-profile-option-title">
            <strong>{profile.name}</strong>
            {#if profile.id !== 'none'}
              {#if accessSummary.isPublic}
                <span
                  class="project-context-profile-visibility-pill public"
                  title={accessSummary.tooltip}
                  aria-label={accessSummary.ariaLabel}
                >Public</span>
              {:else if accessSummary.members.length}
                <span
                  class="project-context-profile-access-stack"
                  title={accessSummary.tooltip}
                  aria-label={accessSummary.ariaLabel}
                >
                  {#each accessSummary.visibleMembers as member, index (projectAccessMemberKey(member, index))}
                    <span class="project-context-profile-access-avatar" aria-hidden="true">
                      {projectAccessInitial(member)}
                    </span>
                  {/each}
                  {#if accessSummary.overflowCount > 0}
                    <span class="project-context-profile-access-avatar overflow" aria-hidden="true">
                      +{accessSummary.overflowCount}
                    </span>
                  {/if}
                </span>
              {:else}
                <span
                  class="project-context-profile-visibility-pill private"
                  title={accessSummary.tooltip}
                  aria-label={accessSummary.ariaLabel}
                >Private</span>
              {/if}
            {/if}
          </span>
          <small>{profileMeta(profile)}</small>
        </span>
        {#if isSelected}
          <span class="project-context-profile-check" aria-hidden="true">
            <ConstellationIcon name="check" size={13} stroke={2.2} />
          </span>
        {/if}
      </button>
    {/each}
  {:else}
    <div class="project-context-muted compact">No matching projects.</div>
  {/if}
</div>

<div class="project-context-profile-footer">
  <button class="project-context-add-project" type="button" onclick={onCreate}>
    <ConstellationIcon name="folder-plus" size={17} stroke={1.8} />
    <span>Create project</span>
  </button>
</div>
