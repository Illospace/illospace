<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import { listTeamMembers } from '$lib/features/cortex/api/cortexApi';
  import { auth } from '$lib/stores/auth.svelte';
  import type { ProjectContextResource } from '$lib/utils/projectContext';
  import ProjectContextConnectorMenu from './ProjectContextConnectorMenu.svelte';
  import ProjectContextGitHubConnector from './ProjectContextGitHubConnector.svelte';
  import ProjectContextLocalConnector from './ProjectContextLocalConnector.svelte';
  import ProjectContextResourcePills from './ProjectContextResourcePills.svelte';
  import type { ConnectorMode, ProjectVisibility } from './projectContextProfiles';

  type ShareableUser = {
    id?: string;
    user_id?: string;
    name?: string;
  };

  let {
    name = $bindable(''),
    description = $bindable(''),
    visibility = $bindable<ProjectVisibility>('private'),
    sharedUsernames = $bindable(''),
    resources = [],
    validation = { valid: true, errors: [] },
    saveError = '',
    canSave = false,
    saving = false,
    onAddResources,
    onRemoveResource,
    onCancel,
    onSave,
  }: {
    name?: string;
    description?: string;
    visibility?: ProjectVisibility;
    sharedUsernames?: string;
    resources?: ProjectContextResource[];
    validation?: { valid: boolean; errors: string[] };
    saveError?: string;
    canSave?: boolean;
    saving?: boolean;
    onAddResources?: (resources: ProjectContextResource[]) => void;
    onRemoveResource?: (resource: ProjectContextResource) => void;
    onCancel?: () => void;
    onSave?: () => void;
  } = $props();

  let connectorMode = $state<ConnectorMode>('menu');
  let teamUsers = $state<ShareableUser[]>([]);
  let teamUsersLoaded = false;
  let teamUsersLoading = $state(false);
  let teamUsersError = $state('');
  let shareSearch = $state('');
  let sharePickerOpen = $state(false);

  function openConnector(nextMode: ConnectorMode) {
    connectorMode = nextMode;
  }

  function userId(user: ShareableUser): string {
    return String(user.id ?? user.user_id ?? '').trim();
  }

  function userName(user: ShareableUser): string {
    return String(user.name ?? '').trim();
  }

  function selectedShareNames(): string[] {
    return sharedUsernames
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function selectedShareNameSet(): Set<string> {
    return new Set(selectedShareNames().map((item) => item.toLowerCase()));
  }

  const selectedShareUsers = $derived(selectedShareNames());
  const filteredShareUsers = $derived.by(() => {
    const needle = shareSearch.trim().toLowerCase();
    const selected = selectedShareNameSet();
    const currentUserId = String(auth.user?.id ?? '');
    return teamUsers
      .filter((user) => userName(user) && userId(user) !== currentUserId)
      .filter((user) => !selected.has(userName(user).toLowerCase()))
      .filter((user) => !needle || userName(user).toLowerCase().includes(needle))
      .sort((a, b) => userName(a).localeCompare(userName(b)));
  });

  const connectorTitle = $derived(
    connectorMode === 'github'
      ? 'GitHub repo'
      : connectorMode === 'local'
        ? 'Files or folders'
        : '',
  );

  async function loadShareUsers() {
    if (teamUsersLoading || teamUsersLoaded) return;
    teamUsersLoading = true;
    teamUsersError = '';
    try {
      teamUsers = await listTeamMembers();
      teamUsersLoaded = true;
    } catch {
      teamUsers = [];
      teamUsersLoaded = true;
      teamUsersError = 'Could not load users.';
    } finally {
      teamUsersLoading = false;
    }
  }

  function openSharePicker() {
    sharePickerOpen = true;
    void loadShareUsers();
  }

  function addShareUser(user: ShareableUser) {
    const name = userName(user);
    if (!name) return;
    const selected = selectedShareNames();
    if (!selected.some((item) => item.toLowerCase() === name.toLowerCase())) {
      sharedUsernames = [...selected, name].join(', ');
    }
    shareSearch = '';
    sharePickerOpen = true;
  }

  function removeShareUser(name: string) {
    sharedUsernames = selectedShareNames()
      .filter((item) => item.toLowerCase() !== name.toLowerCase())
      .join(', ');
    sharePickerOpen = true;
  }

  function handleShareKeydown(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      sharePickerOpen = false;
      return;
    }
    if (event.key === 'Enter') {
      const first = filteredShareUsers[0];
      if (!first) return;
      event.preventDefault();
      addShareUser(first);
      return;
    }
    if (event.key === 'Backspace' && !shareSearch.trim()) {
      const selected = selectedShareNames();
      if (!selected.length) return;
      event.preventDefault();
      sharedUsernames = selected.slice(0, -1).join(', ');
    }
  }

  $effect(() => {
    if (visibility === 'private') return;
    sharedUsernames = '';
    shareSearch = '';
    sharePickerOpen = false;
  });
</script>

<div class="project-create-intro">
  <strong>Projects are saved context containers.</strong>
  <span>Add a GitHub repo or backend-readable files before saving.</span>
</div>

<div class="project-create-fields">
  <input
    aria-label="Project name"
    placeholder="Project name"
    bind:value={name}
  />
  <input
    aria-label="Project description"
    placeholder="Description (optional)"
    bind:value={description}
  />
</div>

<div class="project-visibility-row" role="group" aria-label="Project visibility">
  <button
    type="button"
    class:selected={visibility === 'private'}
    aria-pressed={visibility === 'private'}
    onclick={() => { visibility = 'private'; }}
  >
    <ConstellationIcon name="lock" size={14} stroke={1.8} />
    <span>Private</span>
  </button>
  <button
    type="button"
    class:selected={visibility === 'public'}
    aria-pressed={visibility === 'public'}
    onclick={() => { visibility = 'public'; }}
  >
    <ConstellationIcon name="team" size={14} stroke={1.8} />
    <span>Public</span>
  </button>
</div>

{#if visibility === 'private'}
  <div class="project-access-picker">
    {#if selectedShareUsers.length}
      <div class="project-access-selected" aria-label="Shared users">
        {#each selectedShareUsers as selectedName}
          <button type="button" onclick={() => removeShareUser(selectedName)}>
            <span>{selectedName}</span>
            <ConstellationIcon name="x" size={12} stroke={2} />
          </button>
        {/each}
      </div>
    {/if}
    <input
      class="project-access-input"
      aria-label="Shared users"
      placeholder="Share with users"
      bind:value={shareSearch}
      onfocus={openSharePicker}
      oninput={openSharePicker}
      onkeydown={handleShareKeydown}
    />
    {#if sharePickerOpen}
      <div class="project-access-menu" role="listbox" aria-label="Users">
        {#if teamUsersLoading}
          <div class="project-access-empty">Loading users...</div>
        {:else if teamUsersError}
          <div class="project-access-empty">{teamUsersError}</div>
        {:else if filteredShareUsers.length}
          {#each filteredShareUsers as user (userId(user) || userName(user))}
            <button
              type="button"
              role="option"
              aria-selected="false"
              onpointerdown={(event) => event.preventDefault()}
              onclick={() => addShareUser(user)}
            >
              <ConstellationIcon name="team" size={14} stroke={1.8} />
              <span>{userName(user)}</span>
            </button>
          {/each}
        {:else}
          <div class="project-access-empty">No users found.</div>
        {/if}
      </div>
    {/if}
  </div>
{/if}

{#if connectorMode === 'menu'}
  <ProjectContextConnectorMenu onOpen={openConnector} />
{:else}
  <div class="connector-view-header">
    <button
      type="button"
      class="connector-back-button"
      aria-label="Back to resource options"
      onclick={() => { connectorMode = 'menu'; }}
    >
      <ConstellationIcon name="chevron-left" size={14} stroke={2} />
      <span>Resources</span>
    </button>
    <span class="connector-view-title">{connectorTitle}</span>
  </div>

  {#if connectorMode === 'github'}
    <ProjectContextGitHubConnector onAddResources={onAddResources} />
  {:else}
    <ProjectContextLocalConnector
      mode="local"
      onAddResources={onAddResources}
    />
  {/if}
{/if}

{#if resources.length}
  <ProjectContextResourcePills
    {resources}
    selected
    removable
    ariaLabel="Selected resources"
    onRemove={onRemoveResource}
  />
{:else}
  <p class="project-context-muted project-empty-note">No resources yet. Add a GitHub repo, file, or folder before saving.</p>
{/if}

{#if !validation.valid}
  <p class="project-context-error">{validation.errors[0]}</p>
{/if}
{#if saveError}
  <p class="project-context-error">{saveError}</p>
{/if}

<div class="project-context-actions">
  <button class="project-context-secondary" type="button" onclick={onCancel}>Cancel</button>
  <button class="project-context-primary" type="button" onclick={onSave} disabled={!canSave}>
    {saving ? 'Saving...' : 'Save project'}
  </button>
</div>
