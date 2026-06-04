<script lang="ts">
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
    projectFileMatchesDisplayPath,
    projectSelectorOptions,
    type ProjectExplorerFile,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import ProjectDraftFilePreview from './ProjectDraftFilePreview.svelte';
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
    filePath = '',
  }: {
    idea: DraftIdea;
    runId?: string | number | null;
    filePath?: string | null;
  } = $props();

  let draftState = $state<ProjectDraftStateResponse | ProjectDraftStateRead | null>(null);
  let loading = $state(false);
  let loadError = $state('');
  let loadedKey = $state('');
  let requestSeq = 0;
  let projectProfiles = $state<ThreadProjectContextProfile[]>([]);
  let projectAttachments = $state<ThreadProjectContextAttachment[]>([]);
  let projectsLoadedKey = $state('');
  let selectedProjectProfileId = $state('');

  const focusedFilePath = $derived(String(filePath ?? '').trim());
  const draftView = $derived.by(() =>
    buildProjectDraftPanelView({ draftState, loading, loadError, runId }),
  );
  const statePayload = $derived(draftView.statePayload);
  const fileBrowser = $derived(draftView.fileBrowser);
  const projectSelectorItems = $derived(projectSelectorOptions(projectProfiles, projectAttachments));
  const selectedFile = $derived.by((): ProjectExplorerFile | null => {
    if (!focusedFilePath) return null;
    return fileBrowser.files.find((file) => projectFileMatchesDisplayPath(file, focusedFilePath)) ?? null;
  });

  async function loadProjectSelector(ideaId: string) {
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
</script>

<section class="thread-project-file-preview-pane" aria-label="Project file preview">
  {#if loading && !statePayload}
    <div class="project-draft-empty">Loading Project file preview...</div>
  {:else if loadError}
    <div class="project-draft-empty project-draft-empty-warning">{loadError}</div>
  {:else if statePayload?.ok === false}
    <div class="project-draft-empty project-draft-empty-warning">
      {statePayload.error || 'No Project draft state is bound to this run.'}
    </div>
  {:else}
    <ProjectDraftFilePreview
      {idea}
      runId={runId ?? statePayload?.run_id ?? null}
      projectProfileId={selectedProjectProfileId}
      selectedFile={selectedFile}
      missingFilePath={focusedFilePath}
      fill
      onDraftStateChanged={reloadDraftStateAfterFileSave}
    />
  {/if}
</section>

<style>
  .thread-project-file-preview-pane {
    display: flex;
    flex-direction: column;
    min-height: 100%;
    min-width: 0;
    color: rgba(239, 244, 251, 0.86);
  }

  :global(:root[data-color-scheme='light']) .thread-project-file-preview-pane {
    color: rgba(29, 39, 49, 0.86);
  }

  .project-draft-empty {
    color: rgba(231, 238, 247, 0.48);
    font-size: 12px;
    line-height: 1.55;
    padding: 14px 16px;
  }

  :global(:root[data-color-scheme='light']) .project-draft-empty {
    color: rgba(82, 98, 111, 0.68);
  }

  .project-draft-empty-warning {
    color: #e7bc77;
  }
</style>
