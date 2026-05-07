<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
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
      <button
        type="button"
        class="project-context-profile-option"
        class:selected={isSelected}
        role="option"
        aria-selected={isSelected}
        onclick={() => selectProject(profile)}
      >
        <ConstellationIcon name="folder" size={17} stroke={1.8} />
        <span class="project-context-profile-option-copy">
          <strong>{profile.name}</strong>
          {#if profile.description && profile.id !== 'none'}
            <small>{profile.description}</small>
          {:else if profile.resources.length}
            <small>{profile.resources.length} resource{profile.resources.length === 1 ? '' : 's'}</small>
          {:else if profile.id !== 'none'}
            <small>Empty project — add resources when you create or attach context</small>
          {/if}
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
