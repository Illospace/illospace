<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    getIdeaProjectDraftFile,
    getIdeaProjectDraftFileBlobUrl,
    getIdeaProjectProfileDraftFile,
    getIdeaProjectProfileDraftFileBlobUrl,
    updateIdeaProjectDraftFile,
    updateIdeaProjectProfileDraftFile,
  } from '$lib/features/threads/api/threadApi';
  import {
    buildProjectFilePreviewView,
    projectFileExtension,
    projectFileKind,
    projectFileKindLabel,
    projectFileLayerLabel,
    projectFileSizeLabel,
    projectFileStatusLabel,
    projectFileStatusTone,
    type ProjectExplorerFile,
    type ProjectPreviewLayerKey,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import type { ProjectDraftFileResponse } from '$lib/api/client';
  import ProjectDraftFilePreviewContent from './ProjectDraftFilePreviewContent.svelte';
  import ProjectDraftFilePreviewEditor from './ProjectDraftFilePreviewEditor.svelte';
  import {
    canEmbedProjectFileKind,
    projectFileIconName,
  } from './projectDraftFilePreviewPresentation';

  type DraftIdea = {
    id?: string | null;
  } | null;

  let {
    idea,
    runId = null,
    projectProfileId = '',
    selectedFile = null,
    missingFilePath = '',
    fill = false,
    onDraftStateChanged,
  }: {
    idea: DraftIdea;
    runId?: string | number | null;
    projectProfileId?: string;
    selectedFile?: ProjectExplorerFile | null;
    missingFilePath?: string | null;
    fill?: boolean;
    onDraftStateChanged?: () => void | Promise<void>;
  } = $props();

  let filePreview = $state<ProjectDraftFileResponse | null>(null);
  let filePreviewLoading = $state(false);
  let filePreviewError = $state('');
  let loadedFileKey = $state('');
  let fileRequestSeq = 0;
  let editingFileKey = $state('');
  let editorContent = $state('');
  let fileSaveLoading = $state(false);
  let fileSaveError = $state('');
  let fileSaveNotice = $state('');
  let previewMode = $state<'review' | 'final'>('review');
  let lastPreviewInteractionKey = $state('');

  const previewView = $derived(buildProjectFilePreviewView(filePreview, selectedFile));
  const isEditingSelectedFile = $derived(Boolean(selectedFile && editingFileKey === selectedFile.key));
  const selectedFileKind = $derived(projectFileKind(selectedFile));
  const selectedFileIsRich = $derived(canEmbedProjectFileKind(selectedFileKind));
  const showPreviewModeTabs = $derived(previewView.mode === 'diff' && !selectedFileIsRich);
  const activePreviewMode = $derived(showPreviewModeTabs && previewMode === 'review' ? 'review' : 'final');
  const selectedFileKindLabel = $derived(projectFileKindLabel(selectedFile));
  const selectedFileIcon = $derived(projectFileIconName(selectedFileKind));
  const selectedFileExtension = $derived(projectFileExtension(selectedFile).replace(/^\./, '').toUpperCase());

  function projectLayerBlobUrl(layer: ProjectPreviewLayerKey | null): string {
    const ideaId = idea?.id ?? null;
    const file = selectedFile;
    if (!ideaId || !file || !layer) return '';
    if (projectProfileId) {
      return getIdeaProjectProfileDraftFileBlobUrl(ideaId, {
        runId,
        projectProfileId,
        path: file.path,
        layer,
      });
    }
    return getIdeaProjectDraftFileBlobUrl(ideaId, {
      runId,
      resourceId: file.resourceId,
      path: file.path,
      layer,
    });
  }

  function setPreviewMode(mode: 'review' | 'final') {
    previewMode = mode;
  }

  function beginFileEdit() {
    if (!selectedFile || !previewView.canEdit) return;
    editingFileKey = selectedFile.key;
    editorContent = previewView.editableContent;
    fileSaveError = '';
    fileSaveNotice = '';
  }

  function cancelFileEdit() {
    editingFileKey = '';
    editorContent = '';
    fileSaveError = '';
  }

  async function saveFileEdit() {
    const ideaId = idea?.id ?? null;
    const file = selectedFile;
    if (!ideaId || !file) return;
    fileSaveLoading = true;
    fileSaveError = '';
    fileSaveNotice = '';
    try {
      const result = projectProfileId
        ? await updateIdeaProjectProfileDraftFile(ideaId, {
            runId,
            projectProfileId,
            resourceId: file.resourceId,
            path: file.path,
            content: editorContent,
          })
        : await updateIdeaProjectDraftFile(ideaId, {
            runId,
            resourceId: file.resourceId,
            path: file.path,
            content: editorContent,
          });
      filePreview = result;
      editingFileKey = '';
      editorContent = '';
      loadedFileKey = filePreviewLoadKey(ideaId, runId, projectProfileId, file);
      fileSaveNotice = 'Thread draft saved.';
      previewMode = 'final';
      await onDraftStateChanged?.();
    } catch (error: any) {
      fileSaveError = error?.detail || error?.message || 'Project draft file could not be updated.';
    } finally {
      fileSaveLoading = false;
    }
  }

  function filePreviewLoadKey(
    ideaId: string,
    currentRunId: string | number | null,
    currentProjectProfileId: string,
    file: ProjectExplorerFile,
  ): string {
    return `${ideaId}:${currentProjectProfileId}:${currentRunId ?? ''}:${file.resourceId}:${file.path}`;
  }

  async function loadFilePreview(
    ideaId: string,
    currentRunId: string | number | null,
    file: ProjectExplorerFile,
    currentProjectProfileId: string,
  ) {
    const requestId = ++fileRequestSeq;
    filePreviewLoading = true;
    filePreviewError = '';
    try {
      const result = currentProjectProfileId
        ? await getIdeaProjectProfileDraftFile(ideaId, {
            runId: currentRunId,
            projectProfileId: currentProjectProfileId,
            path: file.path,
          })
        : await getIdeaProjectDraftFile(ideaId, {
            runId: currentRunId,
            resourceId: file.resourceId,
            path: file.path,
          });
      if (requestId !== fileRequestSeq) return;
      filePreview = result;
    } catch (error: any) {
      if (requestId !== fileRequestSeq) return;
      filePreview = null;
      filePreviewError = error?.detail || error?.message || 'Project file preview is unavailable.';
    } finally {
      if (requestId === fileRequestSeq) filePreviewLoading = false;
    }
  }

  $effect(() => {
    const ideaId = idea?.id ?? null;
    const file = selectedFile;
    const fileKey = ideaId && file
      ? filePreviewLoadKey(ideaId, runId ?? null, projectProfileId, file)
      : '';
    if (lastPreviewInteractionKey === fileKey) return;
    lastPreviewInteractionKey = fileKey;
    editingFileKey = '';
    editorContent = '';
    fileSaveError = '';
    fileSaveNotice = '';
    previewMode = 'review';
  });

  $effect(() => {
    const ideaId = idea?.id ?? null;
    const file = selectedFile;
    const currentRunId = runId ?? null;
    if (!ideaId || !file) {
      fileRequestSeq += 1;
      filePreview = null;
      filePreviewError = '';
      filePreviewLoading = false;
      loadedFileKey = '';
      return;
    }
    const key = filePreviewLoadKey(ideaId, currentRunId, projectProfileId, file);
    if (loadedFileKey === key) return;
    loadedFileKey = key;
    void loadFilePreview(ideaId, currentRunId, file, projectProfileId);
  });
</script>

<div class="project-file-preview" class:project-file-preview-fill={fill} aria-live="polite">
  {#if selectedFile}
    <div class="project-file-preview-head">
      <div>
        <h4 title={selectedFile.displayPath}>{selectedFile.displayPath}</h4>
        <span>
          {projectFileLayerLabel(selectedFile)}
          {#if selectedFile.size}
            / {projectFileSizeLabel(selectedFile.size)}
          {/if}
        </span>
      </div>
      <div class="project-file-preview-actions">
        {#if previewView.canEdit && !isEditingSelectedFile}
          <button
            type="button"
            class="project-file-edit-button"
            disabled={filePreviewLoading || fileSaveLoading}
            title="Edit this file in the thread draft"
            onclick={beginFileEdit}
          >
            <ConstellationIcon name="edit" size={12} />
            <span>Edit</span>
          </button>
        {/if}
        <span class="project-file-status" data-tone={projectFileStatusTone(selectedFile.status)}>
          {projectFileStatusLabel(selectedFile.status)}
        </span>
      </div>
    </div>

    <div class="project-file-context-row">
      <div class="project-file-layer-strip" aria-label="Selected file layers">
        {#if selectedFile.has_root === true}
          <span data-present="true">Root</span>
        {/if}
        {#if selectedFile.has_base === true}
          <span data-present="true">Base</span>
        {/if}
        {#if selectedFile.has_draft === true}
          <span data-present="true">Draft</span>
        {/if}
      </div>
      <span class="project-file-kind-chip" data-kind={selectedFileKind}>
        <ConstellationIcon name={selectedFileIcon} size={12} />
        {selectedFileKindLabel}
        {#if selectedFileExtension}
          / {selectedFileExtension}
        {/if}
      </span>
    </div>

    {#if showPreviewModeTabs}
      <div class="project-preview-mode-tabs" aria-label="Project file preview mode">
        <button
          type="button"
          class:project-preview-mode-active={activePreviewMode === 'review'}
          disabled={isEditingSelectedFile}
          onclick={() => setPreviewMode('review')}
        >
          Review
        </button>
        <button
          type="button"
          class:project-preview-mode-active={activePreviewMode === 'final'}
          disabled={isEditingSelectedFile}
          onclick={() => setPreviewMode('final')}
        >
          Final
        </button>
      </div>
    {/if}

    {#if isEditingSelectedFile}
      <ProjectDraftFilePreviewEditor
        bind:editorContent
        {fileSaveLoading}
        {fileSaveError}
        {fileSaveNotice}
        onCancel={cancelFileEdit}
        onSave={saveFileEdit}
      />
    {:else}
      <ProjectDraftFilePreviewContent
        {selectedFile}
        {selectedFileKind}
        {selectedFileIcon}
        {selectedFileKindLabel}
        {previewView}
        {activePreviewMode}
        {showPreviewModeTabs}
        {filePreviewLoading}
        {filePreviewError}
        {fileSaveNotice}
        layerBlobUrl={projectLayerBlobUrl}
      />
    {/if}
  {:else if missingFilePath}
    <div class="project-draft-empty project-draft-empty-warning">
      No preview found for {missingFilePath}.
    </div>
  {:else}
    <div class="project-draft-empty">No file selected.</div>
  {/if}
</div>

<style>
  .project-file-preview {
    display: grid;
    flex: 1 1 auto;
    align-content: start;
    gap: 9px;
    min-width: 0;
    max-height: min(72vh, 700px);
    overflow: auto;
    padding: 11px;
    color: rgba(239, 244, 251, 0.86);
    font-size: 12px;
  }

  .project-file-preview-fill {
    min-height: 100%;
    max-height: none;
    padding: 14px 16px 18px;
  }

  .project-file-preview-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
  }

  .project-file-preview-head h4 {
    margin: 0;
    overflow-wrap: anywhere;
    color: rgba(243, 247, 255, 0.92);
    font-size: 12px;
    font-weight: 650;
    line-height: 1.35;
  }

  .project-file-preview-head > div {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .project-file-preview-head span {
    color: rgba(231, 238, 247, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
  }

  .project-file-preview-head span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .project-file-preview-actions {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 6px;
    min-width: 0;
  }

  .project-file-status {
    flex: 0 0 auto;
    border-radius: 7px;
    padding: 3px 8px;
    background: rgba(255, 255, 255, 0.055);
    color: rgba(231, 238, 247, 0.66);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .project-file-context-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 6px;
    min-width: 0;
  }

  .project-file-layer-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    min-width: 0;
  }

  .project-file-layer-strip span,
  .project-file-kind-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border-radius: 6px;
    padding: 3px 7px;
    background: rgba(255, 255, 255, 0.045);
    color: rgba(231, 238, 247, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.2;
  }

  .project-file-edit-button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    min-height: 26px;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 7px;
    padding: 4px 8px;
    background: rgba(255, 255, 255, 0.045);
    color: rgba(239, 244, 251, 0.76);
    font-size: 10px;
    line-height: 1.1;
    cursor: pointer;
  }

  .project-file-edit-button:disabled {
    cursor: default;
    opacity: 0.48;
  }

  .project-preview-mode-tabs {
    display: inline-flex;
    align-items: center;
    width: fit-content;
    max-width: 100%;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.065);
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.035);
  }

  .project-preview-mode-tabs button {
    min-width: 64px;
    min-height: 26px;
    border: 0;
    border-radius: 0;
    padding: 4px 10px;
    background: transparent;
    color: rgba(231, 238, 247, 0.56);
    font-size: 10px;
    font-weight: 650;
    line-height: 1;
    cursor: pointer;
  }

  .project-preview-mode-tabs button.project-preview-mode-active {
    background: rgba(255, 255, 255, 0.075);
    color: rgba(239, 244, 251, 0.86);
  }

  .project-draft-empty {
    color: rgba(231, 238, 247, 0.48);
    font-size: 12px;
    line-height: 1.55;
    padding: 10px 0;
  }

  .project-draft-empty-warning {
    color: #e7bc77;
  }

  @media (max-width: 720px) {
    .project-file-preview {
      max-height: none;
    }
  }
</style>
