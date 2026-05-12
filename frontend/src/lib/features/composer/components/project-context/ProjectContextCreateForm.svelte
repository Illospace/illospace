<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { ProjectContextResource } from '$lib/utils/projectContext';
  import ProjectContextConnectorMenu from './ProjectContextConnectorMenu.svelte';
  import ProjectContextGitHubConnector from './ProjectContextGitHubConnector.svelte';
  import ProjectContextLocalConnector from './ProjectContextLocalConnector.svelte';
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

  function openConnector(nextMode: ConnectorMode) {
    connectorMode = nextMode;
  }

  const connectorTitle = $derived(
    connectorMode === 'github'
      ? 'GitHub repo'
      : connectorMode === 'local'
        ? 'Files or folders'
        : '',
  );
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
