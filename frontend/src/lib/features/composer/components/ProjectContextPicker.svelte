<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { ConstellationIcon } from '$lib/components/constellation';
  import {
    createProjectContextProfile,
    listProjectContextProfiles,
  } from '$lib/features/cortex/api/cortexApi';
  import {
    normalizeProjectContextResource,
    normalizeProjectContextResources,
    validateProjectContextResources,
    type ProjectContextPickerState,
    type ProjectContextResource,
    type ProjectContextSnapshotLike,
  } from '$lib/utils/projectContext';
  import ProjectContextCreateForm from './project-context/ProjectContextCreateForm.svelte';
  import ProjectContextSavedProjectSelector from './project-context/ProjectContextSavedProjectSelector.svelte';
  import './project-context/projectContextPicker.css';
  import {
    BUILTIN_PROJECT_CONTEXT_PROFILES,
    mapServerProjectProfile,
    projectContextErrorDetail,
    resourceLocator,
    slugify,
    type ProjectContextProfile,
  } from './project-context/projectContextProfiles';

  let {
    mode = 'workspace',
    disabled = false,
    initialOpen = false,
    initialProfileId = 'none',
    currentSnapshot = null,
    contextKey = '',
    loadServerProfiles = true,
    persistProfiles = loadServerProfiles,
    onStateChange,
    onOpenChange,
  }: {
    mode?: 'workspace' | 'thread' | 'inline';
    disabled?: boolean;
    initialOpen?: boolean;
    initialProfileId?: string;
    currentSnapshot?: ProjectContextSnapshotLike | null;
    contextKey?: string;
    loadServerProfiles?: boolean;
    persistProfiles?: boolean;
    onStateChange?: (state: ProjectContextPickerState) => void;
    onOpenChange?: (open: boolean) => void;
  } = $props();

  let projectContextOpen = $state(false);
  let rootEl: HTMLDivElement | undefined = $state();
  let modalEl: HTMLDivElement | undefined = $state();
  let selectedProjectProfileId = $state('none');
  let serverProjectProfiles = $state<ProjectContextProfile[]>([]);
  let serverProfilesAvailable = $state(false);
  let creatingProject = $state(false);
  let newProjectName = $state('');
  let newProjectDescription = $state('');
  let selectedResources = $state<ProjectContextResource[]>([]);
  let projectSaving = $state(false);
  let projectSaveError = $state('');
  let projectProfilesLoaded = false;
  let projectProfilesLoading = $state(false);
  let userSelectedProjectProfile = false;
  let lastContextKey = '';

  const CURRENT_THREAD_PROJECT_PROFILE_ID = 'current-thread-project';

  const currentThreadProjectProfile = $derived(buildCurrentThreadProjectProfile());

  const projectContextProfiles = $derived([
    ...BUILTIN_PROJECT_CONTEXT_PROFILES,
    ...(currentThreadProjectProfile ? [currentThreadProjectProfile] : []),
    ...serverProjectProfiles,
  ]);
  const selectedProjectProfile = $derived(
    projectContextProfiles.find((profile) => profile.id === selectedProjectProfileId)
      ?? projectContextProfiles[0],
  );
  const activeProjectResources = $derived(
    creatingProject ? selectedResources : (selectedProjectProfile?.resources ?? []),
  );
  const activeProjectValidation = $derived(validateProjectContextResources(activeProjectResources));
  const activeProjectContextSnapshot = $derived(buildProjectContextAttachment());
  const activeProjectStateValidation = $derived(
    activeProjectContextSnapshot ? activeProjectValidation : { valid: true, errors: [] },
  );
  const chipLabel = $derived(
    creatingProject
      ? (newProjectName.trim() || 'New Project')
      : (selectedProjectProfile?.id !== 'none' ? (selectedProjectProfile?.name ?? 'Project Context') : 'Project Context'),
  );
  const canSaveProject = $derived(
    creatingProject
      && newProjectName.trim().length > 0
      && selectedResources.length > 0
      && activeProjectValidation.valid
      && !projectSaving,
  );

  function snapshotResources(snapshot: ProjectContextSnapshotLike | null | undefined): ProjectContextResource[] {
    const resources = snapshot?.resources ?? snapshot?.targets ?? [];
    return Array.isArray(resources) ? normalizeProjectContextResources(resources) : [];
  }

  function snapshotName(snapshot: ProjectContextSnapshotLike | null | undefined): string {
    return snapshot?.selected_profile_name?.trim()
      || snapshot?.profile_name?.trim()
      || snapshot?.name?.trim()
      || 'Saved project';
  }

  function currentSnapshotServerProfileId(): string {
    const serverProfileId = currentSnapshot?.project_profile_id;
    return typeof serverProfileId === 'string' ? serverProfileId.trim() : '';
  }

  function serverProfileForCurrentSnapshot(): ProjectContextProfile | undefined {
    const serverProfileId = currentSnapshotServerProfileId();
    if (!serverProfileId) return undefined;
    return serverProjectProfiles.find((profile) => profile.serverProfileId === serverProfileId);
  }

  function buildCurrentThreadProjectProfile(): ProjectContextProfile | null {
    if (!currentSnapshot) return null;
    if (serverProfileForCurrentSnapshot()) return null;

    return {
      id: CURRENT_THREAD_PROJECT_PROFILE_ID,
      serverProfileId: currentSnapshotServerProfileId() || undefined,
      name: snapshotName(currentSnapshot),
      description: currentSnapshot.description?.trim() || 'Attached to this thread.',
      resources: snapshotResources(currentSnapshot),
    };
  }

  function profileIdForCurrentSnapshot(): string {
    if (!currentSnapshot) return 'none';
    const serverProfile = serverProfileForCurrentSnapshot();
    if (serverProfile) return serverProfile.id;

    const rawSelectedProfileId = currentSnapshot.selected_profile_id;
    const selectedProfileId = typeof rawSelectedProfileId === 'string' ? rawSelectedProfileId.trim() : '';
    if (
      selectedProfileId
      && projectContextProfiles.some((profile) => profile.id === selectedProfileId)
    ) {
      return selectedProfileId;
    }

    return currentThreadProjectProfile ? CURRENT_THREAD_PROJECT_PROFILE_ID : 'none';
  }

  function buildProjectContextAttachment() {
    const resources = normalizeProjectContextResources(activeProjectResources);
    const selectedName = creatingProject ? newProjectName.trim() : (selectedProjectProfile?.name ?? '');
    const selectedDescription = creatingProject
      ? newProjectDescription.trim()
      : (selectedProjectProfile?.description ?? '');
    if (!resources.length && selectedProjectProfile?.id === 'none' && !creatingProject) return null;
    const validation = activeProjectValidation;
    return {
      version: 1,
      source: 'cortex-ui-project-picker',
      selected_profile_id: selectedProjectProfile?.id,
      project_profile_id: selectedProjectProfile?.serverProfileId,
      selected_profile_name: selectedName || undefined,
      name: selectedName || undefined,
      description: selectedDescription || undefined,
      validation_status: validation.valid ? 'client_validated' : 'client_invalid',
      validation_errors: validation.errors,
      resources,
    };
  }

  async function loadProjectContextProfiles(force = false) {
    if (!loadServerProfiles) return;
    if (projectProfilesLoading || (projectProfilesLoaded && !force)) return;
    projectProfilesLoading = true;
    try {
      const profiles = await listProjectContextProfiles();
      serverProjectProfiles = profiles.map(mapServerProjectProfile);
      serverProfilesAvailable = true;
    } catch {
      serverProjectProfiles = [];
      serverProfilesAvailable = false;
    } finally {
      projectProfilesLoaded = true;
      projectProfilesLoading = false;
    }
  }

  function suggestProjectName(resource: ProjectContextResource) {
    if (newProjectName.trim()) return;
    newProjectName = resource.label ?? resource.name ?? resource.path ?? 'New Project';
  }

  function addSelectedResource(resource: ProjectContextResource) {
    const normalizedResource = normalizeProjectContextResource(resource, selectedResources.length);
    const key = resourceLocator(normalizedResource);
    if (!key) return;
    selectedResources = [
      normalizedResource,
      ...selectedResources.filter((item) => resourceLocator(item) !== key),
    ];
    suggestProjectName(normalizedResource);
    projectSaveError = '';
  }

  function addSelectedResources(resources: ProjectContextResource[]) {
    for (const resource of resources) {
      addSelectedResource(resource);
    }
  }

  function removeSelectedResource(resource: ProjectContextResource) {
    const key = resourceLocator(resource);
    selectedResources = selectedResources.filter((item) => resourceLocator(item) !== key);
  }

  function beginCreateProject() {
    setProjectContextOpen(false);
    creatingProject = true;
    projectSaveError = '';
  }

  function cancelCreateProject() {
    creatingProject = false;
    selectedResources = [];
    newProjectName = '';
    newProjectDescription = '';
    projectSaveError = '';
  }

  function setProjectContextOpen(nextOpen: boolean) {
    if (disabled && nextOpen) return;
    if (projectContextOpen === nextOpen) return;
    projectContextOpen = nextOpen;
    onOpenChange?.(nextOpen);
    if (nextOpen) void loadProjectContextProfiles();
  }

  function errorCopy(err: any): string {
    const detail = err?.detail;
    const validationErrors = detail?.validation_errors;
    if (Array.isArray(validationErrors) && validationErrors.length) return String(validationErrors[0]);
    return projectContextErrorDetail(err, 'Could not save project.');
  }

  async function saveProjectProfile() {
    if (!canSaveProject) return;
    projectSaving = true;
    projectSaveError = '';
    const name = newProjectName.trim();
    const description = newProjectDescription.trim();
    const projectContext = {
      name,
      description: description || undefined,
      resources: normalizeProjectContextResources(selectedResources),
    };
    try {
      let profile: ProjectContextProfile;
      if (persistProfiles && serverProfilesAvailable) {
        const created = await createProjectContextProfile({
          slug: slugify(name),
          name,
          description: description || null,
          project_context: projectContext,
          metadata: { source: 'cortex-ui-project-picker' },
        });
        profile = mapServerProjectProfile(created);
      } else {
        profile = {
          id: `local:${slugify(name)}-${Date.now()}`,
          name,
          description,
          resources: normalizeProjectContextResources(selectedResources),
        };
      }
      serverProjectProfiles = [profile, ...serverProjectProfiles.filter((item) => item.id !== profile.id)];
      selectedProjectProfileId = profile.id;
      userSelectedProjectProfile = true;
      creatingProject = false;
      selectedResources = [];
      newProjectName = '';
      newProjectDescription = '';
    } catch (err: any) {
      projectSaveError = errorCopy(err);
    } finally {
      projectSaving = false;
    }
  }

  $effect(() => {
    onStateChange?.({
      snapshot: activeProjectContextSnapshot,
      valid: activeProjectStateValidation.valid,
      error: activeProjectStateValidation.errors[0] ?? null,
      resourceCount: activeProjectResources.length,
    });
  });

  $effect(() => {
    if (mode !== 'thread') return;

    const nextContextKey = contextKey || JSON.stringify(currentSnapshot ?? null);
    if (nextContextKey !== lastContextKey) {
      lastContextKey = nextContextKey;
      userSelectedProjectProfile = false;
    }

    if (userSelectedProjectProfile) return;
    selectedProjectProfileId = profileIdForCurrentSnapshot();
  });

  $effect(() => {
    if (!projectContextOpen) return;

    function handlePointerDown(event: MouseEvent) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (!rootEl?.contains(target)) {
        setProjectContextOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setProjectContextOpen(false);
      }
    }

    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  });

  $effect(() => {
    if (!creatingProject) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        cancelCreateProject();
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    void tick().then(() => {
      modalEl?.querySelector<HTMLInputElement>('input:not([type="hidden"]):not([disabled])')?.focus();
    });

    return () => {
      document.removeEventListener('keydown', handleKeyDown);
    };
  });

  function projectContextPortal(node: HTMLElement) {
    document.body.appendChild(node);

    return {
      destroy() {
        node.remove();
      },
    };
  }

  onMount(() => {
    projectContextOpen = initialOpen;
    selectedProjectProfileId = mode === 'thread' ? profileIdForCurrentSnapshot() : initialProfileId;
    serverProfilesAvailable = persistProfiles && loadServerProfiles;
    onOpenChange?.(projectContextOpen);
    if (projectContextOpen) void loadProjectContextProfiles();
  });
</script>

<div bind:this={rootEl} class="project-context-composer" data-mode={mode}>
  <button
    class="project-context-chip"
    class:active={projectContextOpen || creatingProject}
    class:invalid={!activeProjectStateValidation.valid}
    type="button"
    disabled={disabled}
    aria-expanded={projectContextOpen}
    aria-haspopup="dialog"
    onclick={() => setProjectContextOpen(!projectContextOpen)}
  >
    <ConstellationIcon name="folder" size={15} stroke={1.9} />
    <span class="project-context-chip-label">{chipLabel}</span>
    <ConstellationIcon name="chevron-down" size={12} stroke={1.9} className="project-context-chip-chevron" />
  </button>

  {#if projectContextOpen}
    <div class="project-context-popover" role="dialog" aria-label="Project Context picker">
	      <ProjectContextSavedProjectSelector
	        profiles={projectContextProfiles}
	        bind:selectedProfileId={selectedProjectProfileId}
	        selectedProfile={selectedProjectProfile}
	        loading={projectProfilesLoading}
	        onCreate={beginCreateProject}
	        onSelect={() => {
	          userSelectedProjectProfile = true;
	          setProjectContextOpen(false);
	        }}
	      />
    </div>
  {/if}

  {#if creatingProject}
    <div
      class="project-context-modal-backdrop"
      use:projectContextPortal
      role="dialog"
      aria-modal="true"
      aria-label="Create project"
      tabindex="-1"
    >
      <button
        class="project-context-modal-scrim"
        type="button"
        aria-label="Close create project modal"
        onclick={cancelCreateProject}
      ></button>
      <div class="project-context-modal" bind:this={modalEl}>
        <div class="project-context-modal-header">
          <div class="project-context-modal-title">
            <strong>Create project</strong>
          </div>
          <button
            class="project-context-close"
            type="button"
            onclick={cancelCreateProject}
            aria-label="Close create project modal"
          >
            <ConstellationIcon name="close" size={12} stroke={2} />
          </button>
        </div>

        <div class="project-context-modal-body">
          <ProjectContextCreateForm
            bind:name={newProjectName}
            bind:description={newProjectDescription}
            resources={selectedResources}
            validation={activeProjectValidation}
            saveError={projectSaveError}
            canSave={canSaveProject}
            saving={projectSaving}
            onAddResources={addSelectedResources}
            onRemoveResource={removeSelectedResource}
            onCancel={cancelCreateProject}
            onSave={() => void saveProjectProfile()}
          />
        </div>
      </div>
    </div>
  {/if}
</div>
