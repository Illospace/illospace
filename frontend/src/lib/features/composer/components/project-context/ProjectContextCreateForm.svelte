<script lang="ts">
  import type { ProjectContextResource } from '$lib/utils/projectContext';
  import ProjectContextConnectorMenu from './ProjectContextConnectorMenu.svelte';
  import ProjectContextResourcePills from './ProjectContextResourcePills.svelte';
  import type { ConnectorMode } from './projectContextProfiles';

  let {
    name = $bindable(''),
    description = $bindable(''),
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
  let ProjectContextGitHubConnector = $state<any>(null);
  let ProjectContextLocalConnector = $state<any>(null);

  async function loadGitHubConnector() {
    if (ProjectContextGitHubConnector) return;
    const module = await import('./ProjectContextGitHubConnector.svelte');
    ProjectContextGitHubConnector = module.default;
  }

  async function loadLocalConnector() {
    if (ProjectContextLocalConnector) return;
    const module = await import('./ProjectContextLocalConnector.svelte');
    ProjectContextLocalConnector = module.default;
  }

  function openConnector(nextMode: ConnectorMode) {
    connectorMode = nextMode;
    if (nextMode === 'github') void loadGitHubConnector();
    if (nextMode === 'folder' || nextMode === 'file') void loadLocalConnector();
  }
</script>

<div class="project-create-intro">
  <strong>Projects are saved context containers.</strong>
  <span>Create one empty, or add resources now. A resource can be a GitHub repo, a folder tree, or individual files.</span>
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

<div class="connector-tabs" aria-label="Project resource options">
  <button type="button" class:active={connectorMode === 'menu'} onclick={() => { connectorMode = 'menu'; }}>Add resources</button>
  <button type="button" class:active={connectorMode === 'github'} onclick={() => openConnector('github')}>Repo</button>
  <button type="button" class:active={connectorMode === 'folder'} onclick={() => openConnector('folder')}>Folder</button>
  <button type="button" class:active={connectorMode === 'file'} onclick={() => openConnector('file')}>Files</button>
</div>

{#if connectorMode === 'menu'}
  <ProjectContextConnectorMenu onOpen={openConnector} />
{:else if connectorMode === 'github'}
  {#if ProjectContextGitHubConnector}
    <ProjectContextGitHubConnector onAddResources={onAddResources} />
  {:else}
    <div class="project-context-muted compact">Loading connector...</div>
  {/if}
{:else}
  {#if ProjectContextLocalConnector}
    <ProjectContextLocalConnector
      mode={connectorMode === 'file' ? 'file' : 'folder'}
      onAddResources={onAddResources}
    />
  {:else}
    <div class="project-context-muted compact">Loading connector...</div>
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
  <p class="project-context-muted project-empty-note">No resources yet. Saving now creates an empty project you can use as a named container.</p>
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
    {saving ? 'Saving...' : (resources.length ? 'Save project' : 'Create empty project')}
  </button>
</div>
