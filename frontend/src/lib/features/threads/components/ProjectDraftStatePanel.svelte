<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    getIdeaProjectDraftState,
    getIdeaProjectProfileDraftState,
    listIdeaProjectContext,
    listProjectContextProfiles,
    type ThreadProjectContextAttachment,
    type ThreadProjectContextProfile,
  } from '$lib/features/threads/api/threadApi';
  import {
    buildProjectDraftPanelView,
    projectDirectoryAncestorKeys,
    projectFileStatusTone,
    filterProjectSelectorItems,
    projectSelectorOptions,
    resolveProjectFileDisplayPath,
    visibleProjectExplorerRows,
    type ProjectExplorerFile,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import ProjectDraftFileBrowser from './ProjectDraftFileBrowser.svelte';
  import ProjectDraftFilePreview from './ProjectDraftFilePreview.svelte';
  import ProjectDraftPanelSummary from './ProjectDraftPanelSummary.svelte';
  import type {
    ProjectDraftStateRead,
    ProjectDraftStateResponse,
  } from '$lib/api/client';

  type DraftIdea = {
    id?: string | null;
  } | null;

  let {
    idea,
    runId = null,
    focusedFilePath = '',
    previewOnly = false,
  }: {
    idea: DraftIdea;
    runId?: string | number | null;
    focusedFilePath?: string | null;
    previewOnly?: boolean;
  } = $props();

  let draftState = $state<ProjectDraftStateResponse | ProjectDraftStateRead | null>(null);
  let loading = $state(false);
  let loadError = $state('');
  let loadedKey = $state('');
  let requestSeq = 0;
  let projectProfiles = $state<ThreadProjectContextProfile[]>([]);
  let projectAttachments = $state<ThreadProjectContextAttachment[]>([]);
  let projectsLoading = $state(false);
  let projectsLoadedKey = $state('');
  let selectedProjectProfileId = $state('');
  let projectSelectorOpen = $state(false);
  let projectSelectorQuery = $state('');

  let selectedFileKey = $state('');
  let collapsedDirectoryKeys = $state<string[]>([]);

  const draftView = $derived.by(() =>
    buildProjectDraftPanelView({ draftState, loading, loadError, runId }),
  );
  const statePayload = $derived(draftView.statePayload);
  const resources = $derived(draftView.resources);
  const aggregateCounts = $derived(draftView.aggregateCounts);
  const outOfDatePaths = $derived(draftView.outOfDatePaths);
  const fileBrowser = $derived(draftView.fileBrowser);
  const publishPlan = $derived(draftView.publishPlan);
  const runLabel = $derived(draftView.runLabel);
  const readiness = $derived(draftView.readiness);
  const signalTone = $derived(readiness.tone);
  const signalLabel = $derived(readiness.label);
  const projectSelectorItems = $derived(projectSelectorOptions(projectProfiles, projectAttachments));
  const filteredProjectSelectorItems = $derived(
    filterProjectSelectorItems(projectSelectorItems, projectSelectorQuery),
  );
  const selectedProjectItem = $derived(
    projectSelectorItems.find((item) => item.id === selectedProjectProfileId) ?? projectSelectorItems[0] ?? null,
  );
  const selectedProjectLabel = $derived(selectedProjectItem?.name ?? 'Project');
  const selectedProjectSubtitle = $derived(
    selectedProjectItem?.subtitle
      ?? (selectedProjectProfileId ? 'Accessible project' : 'Current thread Project'),
  );
  const selectedProjectContentLabels = $derived(selectedProjectItem?.contentLabels ?? []);
  const focusedPath = $derived(String(focusedFilePath ?? '').trim());
  const focusedFileResolution = $derived(resolveProjectFileDisplayPath(fileBrowser.files, focusedPath));
  const selectedFile = $derived.by(() => {
    if (previewOnly && focusedPath) return focusedFileResolution.file;
    return fileBrowser.files.find((file) => file.key === selectedFileKey) ?? null;
  });
  const visibleRows = $derived(visibleProjectExplorerRows(fileBrowser.rows, collapsedDirectoryKeys));

  function selectProjectProfile(projectId: string) {
    if (projectId === selectedProjectProfileId) {
      projectSelectorOpen = false;
      projectSelectorQuery = '';
      return;
    }
    selectedProjectProfileId = projectId;
    projectSelectorOpen = false;
    projectSelectorQuery = '';
    selectedFileKey = '';
    collapsedDirectoryKeys = [];
  }

  function selectFile(file: ProjectExplorerFile) {
    selectedFileKey = file.key;
  }

  function toggleDirectory(row: { key: string }) {
    collapsedDirectoryKeys = collapsedDirectoryKeys.includes(row.key)
      ? collapsedDirectoryKeys.filter((key) => key !== row.key)
      : [...collapsedDirectoryKeys, row.key];
  }

  async function loadProjectSelector(ideaId: string) {
    projectsLoading = true;
    try {
      const [attachments, profiles] = await Promise.all([
        listIdeaProjectContext(ideaId),
        listProjectContextProfiles(),
      ]);
      if (idea?.id !== ideaId) return;
      projectAttachments = attachments;
      projectProfiles = profiles;
      const options = projectSelectorOptions(profiles, attachments);
      if (!selectedProjectProfileId || !options.some((item) => item.id === selectedProjectProfileId)) {
        selectedProjectProfileId = options[0]?.id ?? '';
      }
    } catch {
      if (idea?.id === ideaId) {
        projectAttachments = [];
        projectProfiles = [];
      }
    } finally {
      if (idea?.id === ideaId) projectsLoading = false;
    }
  }

  async function loadDraftState(
    ideaId: string,
    currentRunId: string | number | null,
    projectProfileId: string,
  ) {
    const requestId = ++requestSeq;
    loading = true;
    loadError = '';
    try {
      const result = projectProfileId
        ? await getIdeaProjectProfileDraftState(ideaId, {
            runId: currentRunId,
            projectProfileId,
          })
        : await getIdeaProjectDraftState(ideaId, { runId: currentRunId });
      if (requestId !== requestSeq) return;
      draftState = result;
    } catch (error: any) {
      if (requestId !== requestSeq) return;
      draftState = null;
      loadError = error?.detail || error?.message || 'Project draft state is unavailable.';
    } finally {
      if (requestId === requestSeq) loading = false;
    }
  }

  async function reloadDraftStateAfterFileSave() {
    const ideaId = idea?.id ?? null;
    if (!ideaId) return;
    await loadDraftState(ideaId, runId ?? statePayload?.run_id ?? null, selectedProjectProfileId);
  }

  $effect(() => {
    const ideaId = idea?.id ?? null;
    if (!ideaId) {
      projectAttachments = [];
      projectProfiles = [];
      selectedProjectProfileId = '';
      projectsLoadedKey = '';
      projectsLoading = false;
      return;
    }
    if (projectsLoadedKey === ideaId) return;
    projectsLoadedKey = ideaId;
    void loadProjectSelector(ideaId);
  });

  $effect(() => {
    const ideaId = idea?.id ?? null;
    const currentRunId = runId ?? null;
    const key = `${ideaId ?? ''}:${currentRunId ?? ''}:${selectedProjectProfileId}`;
    if (!ideaId) {
      requestSeq += 1;
      draftState = null;
      loadError = '';
      loading = false;
      loadedKey = '';
      return;
    }
    if (loadedKey === key) return;
    loadedKey = key;
    void loadDraftState(ideaId, currentRunId, selectedProjectProfileId);
  });

  $effect(() => {
    if (previewOnly) return;
    if (fileBrowser.files.length === 0) {
      selectedFileKey = '';
      collapsedDirectoryKeys = [];
      return;
    }
    if (selectedFileKey && fileBrowser.files.some((file) => file.key === selectedFileKey)) return;
    const changed = fileBrowser.files.find((file) => projectFileStatusTone(file.status) !== 'clean');
    selectedFileKey = (changed ?? fileBrowser.files[0]).key;
  });

  $effect(() => {
    const file = selectedFile;
    if (!file || collapsedDirectoryKeys.length === 0) return;
    const selectedAncestors = new Set(projectDirectoryAncestorKeys(file.resourceId, file.path));
    const next = collapsedDirectoryKeys.filter((key) => !selectedAncestors.has(key));
    if (next.length !== collapsedDirectoryKeys.length) {
      collapsedDirectoryKeys = next;
    }
  });
</script>

<section
  class="project-draft-panel"
  class:project-draft-panel-preview-only={previewOnly}
  aria-label={previewOnly ? 'Project file preview' : 'Project workspace'}
>
  {#if !previewOnly}
    <ProjectDraftPanelSummary
      bind:projectSelectorOpen
      bind:projectSelectorQuery
      {projectsLoading}
      {projectSelectorItems}
      {filteredProjectSelectorItems}
      {selectedProjectProfileId}
      {selectedProjectLabel}
      {selectedProjectSubtitle}
      {selectedProjectContentLabels}
      {signalTone}
      {signalLabel}
      {runLabel}
      resourcesCount={resources.length}
      fileCount={fileBrowser.fileCount}
      readinessDetail={readiness.detail}
      {aggregateCounts}
      publishOperationCount={publishPlan.operationCount}
      publishBlockedCount={publishPlan.blockedCount}
      onSelectProjectProfile={selectProjectProfile}
    />
  {/if}

  {#if previewOnly && loading && !statePayload}
    <div class="project-draft-empty">Loading Project file preview...</div>
  {:else if previewOnly && loadError}
    <div class="project-draft-empty project-draft-empty-warning">{loadError}</div>
  {:else if previewOnly && statePayload?.ok === false}
    <div class="project-draft-empty project-draft-empty-warning">
      {statePayload.error || 'No Project draft state is bound to this run.'}
    </div>
  {:else if previewOnly && focusedFileResolution.ambiguous}
    <div class="project-draft-empty project-draft-empty-warning">
      Multiple Project files match {focusedPath}.
      <div class="project-draft-empty-paths">
        {#each focusedFileResolution.candidates.slice(0, 4) as file (file.key)}
          <code>{file.displayPath}</code>
        {/each}
      </div>
    </div>
  {:else if previewOnly}
    <ProjectDraftFilePreview
      {idea}
      runId={runId ?? statePayload?.run_id ?? null}
      projectProfileId={selectedProjectProfileId}
      {selectedFile}
      missingFilePath={focusedPath}
      fill
      onDraftStateChanged={reloadDraftStateAfterFileSave}
    />
  {:else if loading && !statePayload}
    <div class="project-draft-empty">Loading Project draft state...</div>
  {:else if loadError}
    <div class="project-draft-empty project-draft-empty-warning">{loadError}</div>
  {:else if statePayload?.ok === false}
    <div class="project-draft-empty project-draft-empty-warning">
      {statePayload.error || 'No Project draft state is bound to this run.'}
    </div>
  {:else}
    {#if outOfDatePaths.length > 0}
      <div class="project-draft-alert" data-tone="warning" aria-label="Out of date Project files">
        <ConstellationIcon name="refresh" size={13} />
        <span>{outOfDatePaths.length} out-of-date path{outOfDatePaths.length === 1 ? '' : 's'} need attention.</span>
      </div>
    {/if}

    <ProjectDraftFileBrowser
      {idea}
      runId={runId ?? statePayload?.run_id ?? null}
      projectProfileId={selectedProjectProfileId}
      {fileBrowser}
      {visibleRows}
      {selectedFileKey}
      {selectedFile}
      {collapsedDirectoryKeys}
      onSelectFile={selectFile}
      onToggleDirectory={toggleDirectory}
      onDraftStateChanged={reloadDraftStateAfterFileSave}
    />
  {/if}
</section>

<style>
  .project-draft-panel {
    display: flex;
    flex-direction: column;
    gap: 10px;
    flex: 1 1 auto;
    width: 100%;
    min-height: 100%;
    min-width: 0;
    color: rgba(239, 244, 251, 0.86);
    font-size: 12px;
  }

  .project-draft-panel-preview-only {
    min-height: 100%;
    gap: 0;
  }

  :global(:root[data-color-scheme='light']) .project-draft-panel {
    color: rgba(29, 39, 49, 0.86);
  }

  .project-draft-alert {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
    border-radius: 7px;
    padding: 4px 7px;
    background: rgba(255, 255, 255, 0.04);
    color: rgba(231, 238, 247, 0.58);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.35;
  }

  :global(:root[data-color-scheme='light']) .project-draft-alert {
    background: rgba(85, 104, 120, 0.055);
    color: rgba(82, 98, 111, 0.72);
  }

  .project-draft-alert[data-tone='warning'] {
    border: 1px solid rgba(236, 180, 95, 0.18);
    color: #e7bc77;
  }

  .project-draft-empty {
    color: rgba(231, 238, 247, 0.48);
    font-size: 12px;
    line-height: 1.55;
    padding: 10px 0;
  }

  :global(:root[data-color-scheme='light']) .project-draft-empty {
    color: rgba(82, 98, 111, 0.68);
  }

  .project-draft-empty-warning {
    color: #e7bc77;
  }

  .project-draft-empty-paths {
    display: grid;
    gap: 4px;
    margin-top: 8px;
  }

  .project-draft-empty-paths code {
    overflow-wrap: anywhere;
    color: currentColor;
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
  }

</style>
