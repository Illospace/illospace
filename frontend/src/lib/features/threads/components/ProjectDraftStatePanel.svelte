<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    getIdeaProjectDraftFile,
    getIdeaProjectDraftFileBlobUrl,
    getIdeaProjectDraftState,
    getIdeaProjectProfileDraftFile,
    getIdeaProjectProfileDraftFileBlobUrl,
    getIdeaProjectProfileDraftState,
    listIdeaProjectContext,
    listProjectContextProfiles,
    updateIdeaProjectDraftFile,
    updateIdeaProjectProfileDraftFile,
    type ThreadProjectContextAttachment,
    type ThreadProjectContextProfile,
  } from '$lib/features/threads/api/threadApi';
  import {
    buildProjectDraftPanelView,
    buildProjectFilePreviewView,
    PROJECT_DRAFT_CHANGE_METRICS,
    projectDirectoryAncestorKeys,
    projectFileExtension,
    projectFileKind,
    projectFileKindLabel,
    projectFileLayerLabel,
    projectFileSizeLabel,
    projectFileStatusLabel,
    projectFileStatusTone,
    projectSpreadsheetPreviewRows,
    filterProjectSelectorItems,
    projectSelectorOptions,
    visibleProjectExplorerRows,
    type ProjectFileKind,
    type ProjectExplorerFile,
    type ProjectExplorerDirectory,
    type ProjectExplorerRow,
    type ProjectPreviewLayerKey,
    type ProjectSelectorItem,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';
  import type {
    ProjectDraftFileResponse,
    ProjectDraftStateRead,
    ProjectDraftStateResponse,
  } from '$lib/api/client';

  type DraftIdea = {
    id?: string | null;
  } | null;

  const CHANGE_METRICS = PROJECT_DRAFT_CHANGE_METRICS;

  let {
    idea,
    runId = null,
  }: {
    idea: DraftIdea;
    runId?: string | number | null;
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
  let filePreview = $state<ProjectDraftFileResponse | null>(null);
  let filePreviewLoading = $state(false);
  let filePreviewError = $state('');
  let loadedFileKey = $state('');
  let fileRequestSeq = 0;
  let collapsedDirectoryKeys = $state<string[]>([]);
  let editingFileKey = $state('');
  let editorContent = $state('');
  let fileSaveLoading = $state(false);
  let fileSaveError = $state('');
  let fileSaveNotice = $state('');
  let previewMode = $state<'review' | 'final'>('review');

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
  const selectedFile = $derived(
    fileBrowser.files.find((file) => file.key === selectedFileKey) ?? null,
  );
  const visibleRows = $derived(visibleProjectExplorerRows(fileBrowser.rows, collapsedDirectoryKeys));
  const previewView = $derived(buildProjectFilePreviewView(filePreview, selectedFile));
  const isEditingSelectedFile = $derived(Boolean(selectedFile && editingFileKey === selectedFile.key));
  const selectedFileKind = $derived(projectFileKind(selectedFile));
  const selectedFileTone = $derived(projectFileStatusTone(selectedFile?.status));
  const selectedFileIsRich = $derived(canEmbedFinalKind(selectedFileKind));
  const showPreviewModeTabs = $derived(previewView.mode === 'diff' && !selectedFileIsRich);
  const activePreviewMode = $derived(showPreviewModeTabs && previewMode === 'review' ? 'review' : 'final');
  const selectedFileKindLabel = $derived(projectFileKindLabel(selectedFile));
  const selectedFileIcon = $derived(projectFileIconName(selectedFileKind));
  const selectedFileExtension = $derived(projectFileExtension(selectedFile).replace(/^\./, '').toUpperCase());
  const finalBlobUrl = $derived(projectLayerBlobUrl(previewView.finalLayer?.key ?? null));
  const rootPreviewLayer = $derived(previewView.layers.find((item) => item.key === 'root') ?? null);
  const draftPreviewLayer = $derived(previewView.layers.find((item) => item.key === 'draft') ?? null);
  const rootBlobUrl = $derived(projectLayerBlobUrl('root'));
  const draftBlobUrl = $derived(projectLayerBlobUrl('draft'));
  const showRichReplacementCompare = $derived(
    selectedFileIsRich
      && selectedFileTone !== 'clean'
      && Boolean(rootPreviewLayer?.layer?.exists)
      && Boolean(draftPreviewLayer?.layer?.exists),
  );
  const finalSheetRows = $derived(
    projectSpreadsheetPreviewRows(previewView.finalLayer?.content ?? '', projectFileExtension(selectedFile)),
  );
  const finalSheetColumnIndexes = $derived(sheetColumnIndexes(finalSheetRows));
  const finalSheetHeader = $derived(sheetHeaderCells(finalSheetRows, finalSheetColumnIndexes));
  const finalSheetBodyRows = $derived(finalSheetRows.slice(1));
  const reviewSheetRows = $derived.by(() => {
    if (previewView.mode !== 'diff') return [];
    return previewView.lines
      .map((line) => ({
        kind: line.kind,
        cells: projectSpreadsheetPreviewRows(line.text, projectFileExtension(selectedFile), 1)[0] ?? [''],
      }))
      .filter((row) => row.cells.some((cell) => cell.trim()));
  });
  const reviewSheetColumnIndexes = $derived(
    sheetColumnIndexes(reviewSheetRows.map((row) => row.cells)),
  );
  const reviewSheetHeader = $derived(
    sheetHeaderCells(reviewSheetRows.map((row) => row.cells), reviewSheetColumnIndexes),
  );
  const reviewSheetBodyRows = $derived(reviewSheetRows.slice(1));

  function metricCountLabel(value: number): string {
    return Number.isFinite(value) ? String(value) : '0';
  }

  function rowStyle(row: ProjectExplorerRow): string {
    return `--depth: ${Math.min(row.depth, 8)}`;
  }

  function rowTone(row: ProjectExplorerRow): ReturnType<typeof projectFileStatusTone> {
    return projectFileStatusTone(row.status);
  }

  function projectFileIconName(kind: ProjectFileKind): ConstellationIconName {
    if (kind === 'image') return 'image';
    if (kind === 'pdf') return 'pdf';
    if (kind === 'spreadsheet' || kind === 'data') return 'database';
    if (kind === 'code' || kind === 'graph') return 'code';
    if (kind === 'video') return 'video';
    if (kind === 'archive') return 'archive';
    if (kind === 'markdown' || kind === 'document') return 'document';
    return 'file';
  }

  function finalLayerSourceLabel(key: 'root' | 'base' | 'draft'): string {
    if (key === 'root') return 'project root';
    if (key === 'base') return 'thread base';
    return 'thread draft';
  }

  function projectLayerBlobUrl(layer: ProjectPreviewLayerKey | null): string {
    const ideaId = idea?.id ?? null;
    const file = selectedFile;
    if (!ideaId || !file || !layer) return '';
    if (selectedProjectProfileId) {
      return getIdeaProjectProfileDraftFileBlobUrl(ideaId, {
        runId: runId ?? statePayload?.run_id ?? null,
        projectProfileId: selectedProjectProfileId,
        path: file.path,
        layer,
      });
    }
    return getIdeaProjectDraftFileBlobUrl(ideaId, {
      runId: runId ?? statePayload?.run_id ?? null,
      resourceId: file.resourceId,
      path: file.path,
      layer,
    });
  }

  function primaryPreviewTitle(layer: ProjectPreviewLayerKey): string {
    if (showPreviewModeTabs) return 'Final';
    if (layer === 'root') return 'Project root';
    if (layer === 'base') return 'Thread base';
    return 'Thread draft';
  }

  function sheetColumnIndexes(rows: string[][]): number[] {
    const count = rows.reduce((max, row) => Math.max(max, row.length), 0);
    return Array.from({ length: count }, (_value, index) => index);
  }

  function sheetHeaderCells(rows: string[][], columnIndexes: number[]): string[] {
    const header = rows[0] ?? [];
    return columnIndexes.map((columnIndex) => header[columnIndex] || `Column ${columnIndex + 1}`);
  }

  function canEmbedFinalKind(kind: ProjectFileKind): boolean {
    return kind === 'image' || kind === 'pdf' || kind === 'video';
  }

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
    filePreview = null;
    loadedFileKey = '';
    collapsedDirectoryKeys = [];
    editingFileKey = '';
    editorContent = '';
    fileSaveError = '';
    fileSaveNotice = '';
    previewMode = 'review';
  }

  function selectFile(file: ProjectExplorerFile) {
    selectedFileKey = file.key;
    previewMode = 'review';
    fileSaveError = '';
    fileSaveNotice = '';
    if (editingFileKey && editingFileKey !== file.key) {
      editingFileKey = '';
      editorContent = '';
    }
  }

  function setPreviewMode(mode: 'review' | 'final') {
    previewMode = mode;
  }

  function directoryCollapsed(row: ProjectExplorerDirectory): boolean {
    return collapsedDirectoryKeys.includes(row.key);
  }

  function toggleDirectory(row: ProjectExplorerDirectory) {
    fileSaveError = '';
    fileSaveNotice = '';
    collapsedDirectoryKeys = directoryCollapsed(row)
      ? collapsedDirectoryKeys.filter((key) => key !== row.key)
      : [...collapsedDirectoryKeys, row.key];
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
    const currentRunId = runId ?? statePayload?.run_id ?? null;
    if (!ideaId || !file) return;
    fileSaveLoading = true;
    fileSaveError = '';
    fileSaveNotice = '';
    try {
      const result = selectedProjectProfileId
        ? await updateIdeaProjectProfileDraftFile(ideaId, {
            runId: currentRunId,
            projectProfileId: selectedProjectProfileId,
            resourceId: file.resourceId,
            path: file.path,
            content: editorContent,
          })
        : await updateIdeaProjectDraftFile(ideaId, {
            runId: currentRunId,
            resourceId: file.resourceId,
            path: file.path,
            content: editorContent,
          });
      filePreview = result;
      editingFileKey = '';
      editorContent = '';
      loadedFileKey = `${ideaId}:${selectedProjectProfileId}:${currentRunId ?? ''}:${file.resourceId}:${file.path}`;
      fileSaveNotice = 'Thread draft saved.';
      previewMode = 'final';
      await loadDraftState(ideaId, currentRunId, selectedProjectProfileId);
    } catch (error: any) {
      fileSaveError = error?.detail || error?.message || 'Project draft file could not be updated.';
    } finally {
      fileSaveLoading = false;
    }
  }

  function diffMarker(kind: 'context' | 'removed' | 'added'): string {
    if (kind === 'added') return '+';
    if (kind === 'removed') return '-';
    return ' ';
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

  async function loadFilePreview(
    ideaId: string,
    currentRunId: string | number | null,
    file: ProjectExplorerFile,
    projectProfileId: string,
  ) {
    const requestId = ++fileRequestSeq;
    filePreviewLoading = true;
    filePreviewError = '';
    try {
      const result = projectProfileId
        ? await getIdeaProjectProfileDraftFile(ideaId, {
            runId: currentRunId,
            projectProfileId,
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
    if (fileBrowser.files.length === 0) {
      selectedFileKey = '';
      collapsedDirectoryKeys = [];
      previewMode = 'review';
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

  $effect(() => {
    const ideaId = idea?.id ?? null;
    const file = selectedFile;
    const currentRunId = runId ?? statePayload?.run_id ?? null;
    if (!ideaId || !file) {
      fileRequestSeq += 1;
      filePreview = null;
      filePreviewError = '';
      filePreviewLoading = false;
      loadedFileKey = '';
      return;
    }
    const key = `${ideaId}:${selectedProjectProfileId}:${currentRunId ?? ''}:${file.resourceId}:${file.path}`;
    if (loadedFileKey === key) return;
    loadedFileKey = key;
    void loadFilePreview(ideaId, currentRunId, file, selectedProjectProfileId);
  });
</script>

<section class="project-draft-panel" aria-label="Project workspace">
  <div class="project-draft-summary">
    <div class="project-selector-row">
      <div class="project-selector-anchor">
        <button
          type="button"
          class="project-selector-button"
          aria-expanded={projectSelectorOpen}
          aria-haspopup="listbox"
          disabled={projectsLoading || projectSelectorItems.length === 0}
          onclick={() => { projectSelectorOpen = !projectSelectorOpen; }}
        >
          <span class="project-selector-copy">
            <span class="project-draft-kicker">Project</span>
            <strong>{selectedProjectLabel}</strong>
            <small>{selectedProjectSubtitle}</small>
            {#if selectedProjectContentLabels.length > 0}
              <span class="project-selector-counts" aria-label="Selected Project contents">
                {#each selectedProjectContentLabels as label (label)}
                  <span>{label}</span>
                {/each}
              </span>
            {/if}
          </span>
          <span class="project-selector-action">
            {projectSelectorItems.length > 1 ? 'Switch' : projectsLoading ? 'Loading' : 'Current'}
            {#if projectSelectorItems.length > 1}
              <ConstellationIcon name="chevron-down" size={11} />
            {/if}
          </span>
        </button>
        {#if projectSelectorOpen && projectSelectorItems.length > 0}
          <div class="project-selector-menu" role="listbox" aria-label="Accessible Projects">
            <div class="project-selector-search">
              <input
                type="search"
                bind:value={projectSelectorQuery}
                placeholder={`Search ${projectSelectorItems.length} Projects`}
                aria-label="Search accessible Projects"
              />
              <small>
                {filteredProjectSelectorItems.length} of {projectSelectorItems.length} shown. Attached Projects stay first.
              </small>
            </div>
            <div class="project-selector-menu-scroll">
              {#if filteredProjectSelectorItems.length === 0}
                <div class="project-selector-empty">No Projects match that search.</div>
              {:else}
                {#each ['attached', 'recent'] as group (group)}
                  {@const groupItems = filteredProjectSelectorItems.filter((item) => item.group === group)}
                  {#if groupItems.length > 0}
                    <div class="project-selector-menu-section">
                      <div class="project-selector-menu-label">
                        {group === 'attached' ? 'Attached projects' : 'Recent projects'} / {groupItems.length}
                      </div>
                      {#each groupItems as project (project.id)}
                        <button
                          type="button"
                          class="project-selector-option"
                          class:project-selector-option-active={project.id === selectedProjectProfileId}
                          role="option"
                          aria-selected={project.id === selectedProjectProfileId}
                          onclick={() => selectProjectProfile(project.id)}
                        >
                          <span class="project-selector-option-copy">
                            <strong>{project.name}</strong>
                            <small>{project.subtitle}</small>
                          </span>
                          <span class="project-selector-option-aside">
                            <span class="project-selector-option-counts" aria-label="Project contents">
                              {#each project.contentLabels as label (label)}
                                <span>{label}</span>
                              {/each}
                            </span>
                            <em>{project.id === selectedProjectProfileId ? 'Current' : 'Open'}</em>
                          </span>
                        </button>
                      {/each}
                    </div>
                  {/if}
                {/each}
              {/if}
            </div>
          </div>
        {/if}
      </div>
      <span class="project-draft-signal" data-tone={signalTone}>{signalLabel}</span>
    </div>
    <div class="project-draft-title-row">
      <span class="project-draft-title">Root + thread overlay</span>
    </div>
    <div class="project-draft-meta">
      <span>{runLabel}</span>
      <span>{resources.length} resource{resources.length === 1 ? '' : 's'}</span>
      <span>{fileBrowser.fileCount} file{fileBrowser.fileCount === 1 ? '' : 's'}</span>
      <span>{readiness.detail}</span>
    </div>
    <div class="project-draft-summary-chips" aria-label="Project draft summary">
      {#each CHANGE_METRICS as metric (metric.key)}
        <span data-tone={metric.tone}>
          <strong>{metricCountLabel(aggregateCounts[metric.key])}</strong>
          {metric.label}
        </span>
      {/each}
      <span data-tone={publishPlan.blockedCount > 0 ? 'conflicted' : 'clean'}>
        <strong>{publishPlan.operationCount}</strong>
        publish ops
      </span>
    </div>
  </div>

  {#if loading && !statePayload}
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

    <div class="project-browser">
      <div class="project-browser-head">
        <div>
          <h4>Files</h4>
          <span>
            {fileBrowser.visibleCount} shown
            {#if fileBrowser.truncatedCount > 0}
              / {fileBrowser.truncatedCount} hidden
            {/if}
          </span>
        </div>
        <div class="project-browser-tools">
          <div class="project-browser-legend" aria-label="Project layers">
            <span><ConstellationIcon name="lock" size={12} /> Root</span>
            <span><ConstellationIcon name="edit" size={12} /> Draft</span>
          </div>
        </div>
      </div>

      {#if fileBrowser.rows.length === 0}
        <div class="project-draft-empty">No browsable files found in this Project.</div>
      {:else}
        <div class="project-browser-layout">
          <div id="project-file-tree" class="project-file-tree" aria-label="Project files">
            <div
              class="project-tree-rail"
              aria-hidden="true"
              title="Project files"
            >
              <span class="project-tree-rail-label">Files</span>
              <small>{fileBrowser.visibleCount}</small>
            </div>

            <div class="project-tree-list">
              {#each visibleRows as row (row.key)}
                {#if row.kind === 'directory'}
                  <button
                    type="button"
                    class="project-tree-row project-tree-directory"
                    style={rowStyle(row)}
                    data-tone={rowTone(row)}
                    aria-expanded={!directoryCollapsed(row)}
                    title={row.displayPath}
                    onclick={() => toggleDirectory(row)}
                  >
                    <span class="project-tree-chevron" aria-hidden="true">
                      <ConstellationIcon name={directoryCollapsed(row) ? 'chevron-right' : 'chevron-down'} size={11} />
                    </span>
                    <span class="project-tree-label" title={row.displayPath}>{row.name}</span>
                    <small>{row.fileCount}</small>
                  </button>
                {:else}
                  <button
                    type="button"
                    class="project-tree-row project-tree-file"
                    class:project-tree-file-selected={selectedFileKey === row.key}
                    style={rowStyle(row)}
                    data-tone={rowTone(row)}
                    title={row.displayPath}
                    onclick={() => selectFile(row)}
                  >
                    <span class="project-tree-label">{row.name}</span>
                    <small>{projectFileStatusLabel(row.status)}</small>
                  </button>
                {/if}
              {/each}
            </div>
          </div>

          <div class="project-file-preview" aria-live="polite">
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

              {#if filePreviewLoading}
                <div class="project-draft-empty">Loading file preview...</div>
              {:else if filePreviewError}
                <div class="project-draft-empty project-draft-empty-warning">{filePreviewError}</div>
              {:else if previewView.mode === 'layers' && previewView.layers.length === 0}
                <div class="project-draft-empty">No readable preview for this file.</div>
              {:else if isEditingSelectedFile}
                <div class="project-file-editor">
                  <div class="project-preview-layer-head">
                    <strong>Edit thread draft</strong>
                    <span>Saved changes stay in the draft until publish.</span>
                  </div>
                  <textarea
                    bind:value={editorContent}
                    spellcheck="false"
                    aria-label="Thread draft file contents"
                  ></textarea>
                  {#if fileSaveError}
                    <div class="project-file-save-message" data-tone="warning">{fileSaveError}</div>
                  {:else if fileSaveNotice}
                    <div class="project-file-save-message" data-tone="clean">{fileSaveNotice}</div>
                  {/if}
                  <div class="project-file-editor-actions">
                    <button type="button" onclick={cancelFileEdit} disabled={fileSaveLoading}>Cancel</button>
                    <button type="button" data-primary="true" onclick={saveFileEdit} disabled={fileSaveLoading}>
                      <ConstellationIcon name="check" size={13} />
                      <span>{fileSaveLoading ? 'Saving...' : 'Save draft file'}</span>
                    </button>
                  </div>
                </div>
              {:else}
                {#if fileSaveNotice}
                  <div class="project-file-save-message" data-tone="clean">{fileSaveNotice}</div>
                {/if}
                {#if activePreviewMode === 'review' && previewView.mode === 'diff' && selectedFileKind === 'spreadsheet' && reviewSheetRows.length > 0}
                  <div class="project-preview-layer project-preview-sheet-review">
                    <div class="project-preview-layer-head">
                      <strong>{previewView.title}</strong>
                      <span>{previewView.detail}</span>
                    </div>
                    <div class="project-sheet-preview" data-review="true" aria-label="Project root to thread draft spreadsheet diff">
                      <table>
                        <thead>
                          <tr>
                            <th class="project-sheet-index" scope="col">#</th>
                            {#each reviewSheetColumnIndexes as columnIndex (`review-head-${columnIndex}`)}
                              <th scope="col">{reviewSheetHeader[columnIndex]}</th>
                            {/each}
                          </tr>
                        </thead>
                        <tbody>
                          {#each reviewSheetBodyRows as row, rowIndex (`review-row-${rowIndex}-${row.kind}`)}
                            <tr data-kind={row.kind}>
                              <th class="project-sheet-index" scope="row">
                                <span>{diffMarker(row.kind)}</span>{rowIndex + 1}
                              </th>
                              {#each reviewSheetColumnIndexes as columnIndex (`review-cell-${rowIndex}-${columnIndex}`)}
                                <td>{row.cells[columnIndex] ?? ''}</td>
                              {/each}
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                  </div>
                {:else if activePreviewMode === 'review' && previewView.mode === 'diff'}
                  <div class="project-preview-layer project-preview-diff">
                    <div class="project-preview-layer-head">
                      <strong>{previewView.title}</strong>
                      <span>{previewView.detail}</span>
                    </div>
                    <div class="project-diff-lines" aria-label="Project root to thread draft diff">
                      {#each previewView.lines as line, index (`${index}:${line.kind}:${line.text}`)}
                        <div class="project-diff-line" data-kind={line.kind}>
                          <span class="project-diff-marker">{diffMarker(line.kind)}</span>
                          <code>{line.text || ' '}</code>
                        </div>
                      {/each}
                    </div>
                  </div>
                {:else if showRichReplacementCompare}
                  <div class="project-rich-compare" data-kind={selectedFileKind}>
                    <div class="project-rich-compare-item">
                      <div class="project-preview-layer-head">
                        <strong>Project root</strong>
                        <span>{rootPreviewLayer?.detail}</span>
                      </div>
                      <div class="project-rich-preview" data-kind={selectedFileKind}>
                        {#if selectedFileKind === 'image'}
                          <img src={rootBlobUrl} alt={`${selectedFile.name} in project root`} />
                        {:else if selectedFileKind === 'pdf'}
                          <iframe src={rootBlobUrl} title={`${selectedFile.name} in project root`}></iframe>
                        {:else if selectedFileKind === 'video'}
                          <!-- svelte-ignore a11y_media_has_caption -->
                          <video src={rootBlobUrl} controls playsinline preload="metadata"></video>
                        {/if}
                      </div>
                    </div>
                    <div class="project-rich-compare-item">
                      <div class="project-preview-layer-head">
                        <strong>Thread draft</strong>
                        <span>{draftPreviewLayer?.detail}</span>
                      </div>
                      <div class="project-rich-preview" data-kind={selectedFileKind}>
                        {#if selectedFileKind === 'image'}
                          <img src={draftBlobUrl} alt={`${selectedFile.name} in thread draft`} />
                        {:else if selectedFileKind === 'pdf'}
                          <iframe src={draftBlobUrl} title={`${selectedFile.name} in thread draft`}></iframe>
                        {:else if selectedFileKind === 'video'}
                          <!-- svelte-ignore a11y_media_has_caption -->
                          <video src={draftBlobUrl} controls playsinline preload="metadata"></video>
                        {/if}
                      </div>
                    </div>
                  </div>
                {:else if previewView.finalLayer}
                  <div class="project-preview-layers">
                    <div class="project-preview-layer" data-layer={previewView.finalLayer.key}>
                      <div class="project-preview-layer-head">
                        <strong>{primaryPreviewTitle(previewView.finalLayer.key)}</strong>
                        <span>{previewView.finalLayer.detail} / {finalLayerSourceLabel(previewView.finalLayer.key)}</span>
                      </div>
                      {#if canEmbedFinalKind(selectedFileKind) && finalBlobUrl}
                        <div class="project-rich-preview" data-kind={selectedFileKind}>
                          {#if selectedFileKind === 'image'}
                            <img src={finalBlobUrl} alt={selectedFile.name} />
                          {:else if selectedFileKind === 'pdf'}
                            <iframe src={finalBlobUrl} title={selectedFile.name}></iframe>
                          {:else if selectedFileKind === 'video'}
                            <!-- svelte-ignore a11y_media_has_caption -->
                            <video src={finalBlobUrl} controls playsinline preload="metadata"></video>
                          {/if}
                        </div>
                      {:else if selectedFileKind === 'markdown' && previewView.finalLayer.content.trim()}
                        <div class="project-markdown-preview constellation-prose">
                          {@html renderReadableMarkdown(previewView.finalLayer.content)}
                        </div>
                      {:else if selectedFileKind === 'spreadsheet' && finalSheetRows.length > 0}
                        <div class="project-sheet-preview">
                          <table>
                            <thead>
                              <tr>
                                <th class="project-sheet-index" scope="col">#</th>
                                {#each finalSheetColumnIndexes as columnIndex (`final-head-${columnIndex}`)}
                                  <th scope="col">{finalSheetHeader[columnIndex]}</th>
                                {/each}
                              </tr>
                            </thead>
                            <tbody>
                              {#each finalSheetBodyRows as row, rowIndex (`row-${rowIndex}`)}
                                <tr>
                                  <th class="project-sheet-index" scope="row">{rowIndex + 1}</th>
                                  {#each finalSheetColumnIndexes as columnIndex (`cell-${rowIndex}-${columnIndex}`)}
                                    <td>{row[columnIndex] ?? ''}</td>
                                  {/each}
                                </tr>
                              {/each}
                            </tbody>
                          </table>
                        </div>
                      {:else if previewView.finalLayer.layer?.binary}
                        <div class="project-binary-preview" data-kind={selectedFileKind}>
                          <span aria-hidden="true">
                            <ConstellationIcon name={selectedFileIcon} size={30} stroke={1.5} />
                          </span>
                          <strong>{selectedFileKindLabel} preview</strong>
                          <p>{previewView.finalLayer.detail || 'Binary file'} is available in the Project, but cannot be rendered inline here.</p>
                          {#if finalBlobUrl}
                            <a href={finalBlobUrl} target="_blank" rel="noreferrer">Open file</a>
                          {/if}
                        </div>
                      {:else if selectedFileKind === 'code' || selectedFileKind === 'data' || selectedFileKind === 'graph'}
                        <pre class="project-code-preview" data-kind={selectedFileKind}>{previewView.finalLayer.content}</pre>
                      {:else}
                        <pre>{previewView.finalLayer.content}</pre>
                      {/if}
                    </div>
                  </div>
                {:else}
                  <div class="project-preview-layers">
                    {#each previewView.layers as item (item.key)}
                      <div class="project-preview-layer" data-layer={item.key}>
                        <div class="project-preview-layer-head">
                          <strong>{item.label}</strong>
                          <span>{item.detail}</span>
                        </div>
                        <pre>{item.content}</pre>
                      </div>
                    {/each}
                  </div>
                {/if}
              {/if}
            {:else}
              <div class="project-draft-empty">No file selected.</div>
            {/if}
          </div>
        </div>
      {/if}
    </div>

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

  :global(:root[data-color-scheme='light']) .project-draft-panel {
    color: rgba(29, 39, 49, 0.86);
  }

  .project-draft-summary,
  .project-draft-alert,
  .project-browser {
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.03);
  }

  :global(:root[data-color-scheme='light']) .project-draft-summary,
  :global(:root[data-color-scheme='light']) .project-draft-alert,
  :global(:root[data-color-scheme='light']) .project-browser {
    border-color: rgba(85, 104, 120, 0.13);
    background: rgba(250, 250, 246, 0.78);
  }

  .project-draft-summary {
    padding: 12px;
  }

  .project-draft-kicker,
  .project-browser-head h4 {
    margin: 0;
    color: rgba(240, 240, 250, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  :global(:root[data-color-scheme='light']) .project-draft-kicker,
  :global(:root[data-color-scheme='light']) .project-browser-head h4 {
    color: rgba(82, 98, 111, 0.66);
  }

  .project-draft-title-row,
  .project-browser-head,
  .project-file-preview-head,
  .project-preview-layer-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
  }

  .project-draft-title-row {
    align-items: center;
    margin-top: 7px;
  }

  .project-selector-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: 10px;
  }

  .project-selector-anchor {
    position: relative;
    min-width: 0;
  }

  .project-selector-button {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    width: 100%;
    min-width: 0;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 8px;
    padding: 8px 10px;
    background: rgba(255, 255, 255, 0.035);
    color: inherit;
    text-align: left;
    font: inherit;
    cursor: pointer;
  }

  .project-selector-button:disabled {
    cursor: default;
    opacity: 0.72;
  }

  :global(:root[data-color-scheme='light']) .project-selector-button {
    border-color: rgba(85, 104, 120, 0.14);
    background: rgba(255, 253, 248, 0.82);
  }

  .project-selector-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .project-selector-copy strong {
    display: block;
    overflow: hidden;
    color: rgba(243, 247, 255, 0.94);
    font-size: 14px;
    font-weight: 700;
    line-height: 1.25;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-selector-copy strong {
    color: rgba(20, 29, 38, 0.92);
  }

  .project-selector-copy small {
    display: block;
    overflow: hidden;
    color: rgba(231, 238, 247, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-selector-copy small {
    color: rgba(82, 98, 111, 0.66);
  }

  .project-selector-counts,
  .project-selector-option-counts {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    min-width: 0;
  }

  .project-selector-counts span,
  .project-selector-option-counts span {
    display: inline-flex;
    align-items: center;
    min-height: 18px;
    border-radius: 6px;
    padding: 2px 6px;
    background: rgba(255, 255, 255, 0.055);
    color: rgba(231, 238, 247, 0.62);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-selector-counts span,
  :global(:root[data-color-scheme='light']) .project-selector-option-counts span {
    background: rgba(85, 104, 120, 0.06);
    color: rgba(57, 70, 82, 0.7);
  }

  .project-selector-action {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    border-radius: 7px;
    padding: 4px 7px;
    background: rgba(255, 255, 255, 0.055);
    color: rgba(231, 238, 247, 0.6);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-selector-action {
    background: rgba(85, 104, 120, 0.06);
    color: rgba(57, 70, 82, 0.66);
  }

  .project-selector-menu {
    position: absolute;
    z-index: 5;
    top: calc(100% + 6px);
    left: 0;
    width: 100%;
    max-height: min(68vh, 560px);
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.09);
    border-radius: 10px;
    background: #10151b;
    box-shadow: 0 16px 40px rgba(0, 0, 0, 0.26);
  }

  :global(:root[data-color-scheme='light']) .project-selector-menu {
    border-color: rgba(85, 104, 120, 0.16);
    background: #fffdf8;
    box-shadow: 0 16px 40px rgba(29, 39, 49, 0.16);
  }

  .project-selector-search {
    display: grid;
    gap: 5px;
    padding: 9px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.055);
    background: #10151b;
  }

  :global(:root[data-color-scheme='light']) .project-selector-search {
    border-bottom-color: rgba(85, 104, 120, 0.1);
    background: #fffdf8;
  }

  .project-selector-search input {
    width: 100%;
    height: 30px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 7px;
    padding: 0 9px;
    background: rgba(255, 255, 255, 0.04);
    color: inherit;
    font: inherit;
    font-size: 11px;
  }

  :global(:root[data-color-scheme='light']) .project-selector-search input {
    border-color: rgba(85, 104, 120, 0.14);
    background: rgba(250, 250, 246, 0.92);
  }

  .project-selector-search small,
  .project-selector-empty {
    color: rgba(231, 238, 247, 0.5);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.35;
  }

  :global(:root[data-color-scheme='light']) .project-selector-search small,
  :global(:root[data-color-scheme='light']) .project-selector-empty {
    color: rgba(82, 98, 111, 0.62);
  }

  .project-selector-menu-scroll {
    max-height: min(58vh, 460px);
    overflow: auto;
  }

  .project-selector-empty {
    padding: 14px 16px;
  }

  .project-selector-menu-section {
    padding: 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.055);
  }

  .project-selector-menu-section:last-child {
    border-bottom: 0;
  }

  :global(:root[data-color-scheme='light']) .project-selector-menu-section {
    border-bottom-color: rgba(85, 104, 120, 0.1);
  }

  .project-selector-menu-label {
    margin: 2px 2px 6px;
    color: rgba(231, 238, 247, 0.46);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 8px;
    font-weight: 650;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  :global(:root[data-color-scheme='light']) .project-selector-menu-label {
    color: rgba(82, 98, 111, 0.62);
  }

  .project-selector-option {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    width: 100%;
    min-height: 44px;
    border: 0;
    border-radius: 7px;
    padding: 8px;
    background: transparent;
    color: inherit;
    text-align: left;
    font: inherit;
    cursor: pointer;
  }

  .project-selector-option-copy,
  .project-selector-option-aside {
    min-width: 0;
  }

  .project-selector-option-aside {
    display: grid;
    justify-items: end;
    gap: 4px;
  }

  .project-selector-option-counts {
    justify-content: flex-end;
  }

  .project-selector-option:hover,
  .project-selector-option-active {
    background: rgba(255, 255, 255, 0.06);
  }

  :global(:root[data-color-scheme='light']) .project-selector-option:hover,
  :global(:root[data-color-scheme='light']) .project-selector-option-active {
    background: rgba(82, 117, 139, 0.08);
  }

  .project-selector-option strong {
    display: block;
    overflow: hidden;
    color: rgba(243, 247, 255, 0.9);
    font-size: 12px;
    line-height: 1.25;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-selector-option strong {
    color: rgba(29, 39, 49, 0.88);
  }

  .project-selector-option small {
    display: block;
    margin-top: 2px;
    overflow: hidden;
    color: rgba(231, 238, 247, 0.5);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.35;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-selector-option small {
    color: rgba(82, 98, 111, 0.62);
  }

  .project-selector-option em {
    color: rgba(231, 238, 247, 0.46);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 8px;
    font-style: normal;
    font-weight: 650;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-selector-option em {
    color: rgba(82, 98, 111, 0.52);
  }

  .project-draft-title {
    min-width: 0;
    color: rgba(243, 247, 255, 0.94);
    font-size: 14px;
    font-weight: 650;
    line-height: 1.25;
  }

  :global(:root[data-color-scheme='light']) .project-draft-title {
    color: rgba(20, 29, 38, 0.92);
  }

  .project-draft-signal,
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

  .project-draft-signal[data-tone='clean'],
  .project-file-status[data-tone='clean'] {
    background: color-mix(in srgb, var(--positive, #6BC785) 16%, transparent);
    color: color-mix(in srgb, var(--positive, #6BC785) 82%, white);
  }

  .project-draft-signal[data-tone='modified'],
  .project-file-status[data-tone='changed'],
  .project-file-status[data-tone='new'] {
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 14%, transparent);
    color: color-mix(in srgb, var(--thread-accent, #57CFA0) 78%, white);
  }

  .project-draft-signal[data-tone='warning'],
  .project-file-status[data-tone='warning'],
  .project-file-status[data-tone='deleted'] {
    background: rgba(236, 180, 95, 0.13);
    color: #e7bc77;
  }

  .project-draft-signal[data-tone='conflict'],
  .project-file-status[data-tone='conflicted'] {
    background: rgba(212, 128, 143, 0.14);
    color: #efa5b0;
  }

  .project-draft-meta,
  .project-browser-head span,
  .project-file-preview-head span,
  .project-preview-layer-head span {
    color: rgba(231, 238, 247, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
  }

  .project-draft-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 10px;
    margin-top: 7px;
  }

  .project-draft-meta span,
  .project-browser-head span,
  .project-file-preview-head span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  :global(:root[data-color-scheme='light']) .project-draft-meta,
  :global(:root[data-color-scheme='light']) .project-browser-head span,
  :global(:root[data-color-scheme='light']) .project-file-preview-head span,
  :global(:root[data-color-scheme='light']) .project-preview-layer-head span {
    color: rgba(82, 98, 111, 0.66);
  }

  .project-draft-summary-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 9px;
  }

  .project-draft-summary-chips span,
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

  .project-draft-summary-chips strong {
    color: rgba(243, 247, 255, 0.86);
    font-size: 10px;
  }

  :global(:root[data-color-scheme='light']) .project-draft-summary-chips span,
  :global(:root[data-color-scheme='light']) .project-draft-alert {
    background: rgba(85, 104, 120, 0.055);
    color: rgba(82, 98, 111, 0.72);
  }

  :global(:root[data-color-scheme='light']) .project-draft-summary-chips strong {
    color: rgba(20, 29, 38, 0.86);
  }

  .project-draft-summary-chips span[data-tone='new'] strong,
  .project-draft-summary-chips span[data-tone='clean'] strong {
    color: color-mix(in srgb, var(--positive, #6BC785) 82%, white);
  }

  .project-draft-summary-chips span[data-tone='deleted'] strong {
    color: #e7bc77;
  }

  .project-draft-summary-chips span[data-tone='conflicted'] strong {
    color: #efa5b0;
  }

  .project-draft-alert[data-tone='warning'] {
    border-color: rgba(236, 180, 95, 0.18);
    color: #e7bc77;
  }

  .project-browser {
    display: grid;
    gap: 0;
    overflow: hidden;
  }

  .project-browser-head {
    align-items: center;
    padding: 11px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.055);
  }

  :global(:root[data-color-scheme='light']) .project-browser-head {
    border-bottom-color: rgba(85, 104, 120, 0.12);
  }

  .project-browser-head > div:first-child {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .project-browser-tools {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 6px;
    min-width: 0;
  }

  .project-browser-legend {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 5px;
  }

  .project-browser-legend span,
  .project-file-layer-strip span {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    border-radius: 7px;
    padding: 3px 7px;
    background: rgba(255, 255, 255, 0.045);
    color: rgba(231, 238, 247, 0.6);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
  }

  :global(:root[data-color-scheme='light']) .project-browser-legend span,
  :global(:root[data-color-scheme='light']) .project-file-layer-strip span {
    background: rgba(85, 104, 120, 0.06);
    color: rgba(57, 70, 82, 0.72);
  }

  .project-browser-layout {
    --project-tree-collapsed-width: 62px;
    --project-tree-expanded-width: 260px;
    display: flex;
    min-height: min(72vh, 700px);
  }

  .project-file-tree {
    position: relative;
    z-index: 1;
    flex: 0 0 var(--project-tree-collapsed-width);
    min-width: 0;
    max-height: min(72vh, 700px);
    overflow: hidden;
    border-right: 1px solid rgba(255, 255, 255, 0.055);
    background: #10151b;
    transition: flex-basis 220ms cubic-bezier(0.2, 0, 0, 1);
  }

  .project-file-tree:hover,
  .project-file-tree:focus-within {
    flex-basis: var(--project-tree-expanded-width);
  }

  .project-tree-rail,
  .project-tree-list {
    position: absolute;
    inset: 0;
  }

  .project-tree-rail {
    display: grid;
    grid-template-rows: minmax(0, 1fr) auto;
    justify-items: center;
    gap: 10px;
    width: 100%;
    border: 0;
    padding: 12px 6px;
    background: #10151b;
    color: rgba(231, 238, 247, 0.68);
    cursor: default;
    transition: visibility 0s linear 120ms;
  }

  .project-tree-rail-label {
    align-self: center;
    color: rgba(239, 244, 251, 0.78);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.16em;
    line-height: 1;
    text-transform: uppercase;
    transform: rotate(180deg);
    writing-mode: vertical-rl;
  }

  .project-tree-rail small {
    min-width: 24px;
    border-radius: 999px;
    padding: 3px 5px;
    background: rgba(255, 255, 255, 0.055);
    color: rgba(231, 238, 247, 0.58);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    font-weight: 650;
    line-height: 1;
    text-align: center;
  }

  .project-tree-list {
    min-width: 0;
    overflow: auto;
    padding: 6px;
    border-right: 1px solid rgba(255, 255, 255, 0.055);
    background: #10151b;
    clip-path: inset(0 100% 0 0);
    pointer-events: none;
    transition: clip-path 220ms cubic-bezier(0.2, 0, 0, 1);
    visibility: hidden;
  }

  :global(:root[data-color-scheme='light']) .project-file-tree {
    border-right-color: rgba(85, 104, 120, 0.12);
    background: #fbfaf5;
  }

  :global(:root[data-color-scheme='light']) .project-tree-rail,
  :global(:root[data-color-scheme='light']) .project-tree-list {
    background: #fbfaf5;
  }

  :global(:root[data-color-scheme='light']) .project-tree-rail {
    color: rgba(82, 98, 111, 0.68);
  }

  :global(:root[data-color-scheme='light']) .project-tree-rail small {
    background: rgba(85, 104, 120, 0.07);
    color: rgba(82, 98, 111, 0.76);
  }

  :global(:root[data-color-scheme='light']) .project-tree-rail-label {
    color: rgba(57, 70, 82, 0.82);
  }

  .project-file-tree:hover .project-tree-rail,
  .project-file-tree:focus-within .project-tree-rail {
    pointer-events: none;
    transition-delay: 0s;
    visibility: hidden;
  }

  .project-file-tree:hover .project-tree-list,
  .project-file-tree:focus-within .project-tree-list {
    clip-path: inset(0 0 0 0);
    pointer-events: auto;
    visibility: visible;
  }

  .project-tree-label,
  .project-tree-row small {
    transition:
      max-width 180ms ease,
      opacity 180ms ease,
      transform 180ms ease;
  }

  .project-tree-row {
    --depth: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 7px;
    width: 100%;
    min-height: 30px;
    margin: 1px 0;
    padding: 5px 7px 5px calc(20px + (var(--depth) * 14px));
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: rgba(239, 244, 251, 0.74);
    text-align: left;
  }

  :global(:root[data-color-scheme='light']) .project-tree-row {
    color: rgba(29, 39, 49, 0.78);
  }

  .project-tree-file,
  .project-tree-directory {
    cursor: pointer;
  }

  .project-tree-file:hover,
  .project-tree-directory:hover,
  .project-tree-file-selected {
    background: rgba(255, 255, 255, 0.055);
  }

  :global(:root[data-color-scheme='light']) .project-tree-file:hover,
  :global(:root[data-color-scheme='light']) .project-tree-directory:hover,
  :global(:root[data-color-scheme='light']) .project-tree-file-selected {
    background: rgba(82, 117, 139, 0.08);
  }

  .project-tree-directory {
    color: rgba(231, 238, 247, 0.58);
  }

  :global(:root[data-color-scheme='light']) .project-tree-directory {
    color: rgba(82, 98, 111, 0.72);
  }

  .project-tree-directory {
    grid-template-columns: 14px minmax(0, 1fr) auto;
    padding-left: calc(7px + (var(--depth) * 14px));
  }

  .project-tree-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 0;
    overflow: visible;
  }

  .project-file-kind-chip[data-kind='image'] {
    color: #8bd4bd;
  }

  .project-file-kind-chip[data-kind='pdf'] {
    color: #efa5b0;
  }

  .project-file-kind-chip[data-kind='spreadsheet'],
  .project-file-kind-chip[data-kind='data'] {
    color: #e7bc77;
  }

  .project-file-kind-chip[data-kind='code'],
  .project-file-kind-chip[data-kind='graph'] {
    color: #9dc2ff;
  }

  .project-tree-row span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
  }

  .project-tree-row .project-tree-chevron {
    overflow: visible;
    white-space: normal;
  }

  .project-tree-row small {
    min-width: 0;
    max-width: 78px;
    overflow: hidden;
    text-overflow: ellipsis;
    color: rgba(231, 238, 247, 0.46);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 8px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  :global(:root[data-color-scheme='light']) .project-tree-row small {
    color: rgba(82, 98, 111, 0.58);
  }

  .project-tree-row[data-tone='changed'] small,
  .project-tree-row[data-tone='new'] small {
    color: color-mix(in srgb, var(--thread-accent, #57CFA0) 82%, white);
  }

  .project-tree-row[data-tone='warning'] small,
  .project-tree-row[data-tone='deleted'] small {
    color: #e7bc77;
  }

  .project-tree-row[data-tone='conflicted'] small {
    color: #efa5b0;
  }

  .project-file-preview {
    display: grid;
    flex: 1 1 auto;
    align-content: start;
    gap: 9px;
    min-width: 0;
    max-height: min(72vh, 700px);
    overflow: auto;
    padding: 11px;
  }

  .project-file-preview-head h4 {
    margin: 0;
    overflow-wrap: anywhere;
    color: rgba(243, 247, 255, 0.92);
    font-size: 12px;
    font-weight: 650;
    line-height: 1.35;
  }

  :global(:root[data-color-scheme='light']) .project-file-preview-head h4 {
    color: rgba(20, 29, 38, 0.92);
  }

  .project-file-preview-head > div {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .project-file-preview-actions {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 6px;
    min-width: 0;
  }

  .project-file-edit-button,
  .project-file-editor-actions button {
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

  .project-file-edit-button:disabled,
  .project-file-editor-actions button:disabled {
    cursor: default;
    opacity: 0.48;
  }

  :global(:root[data-color-scheme='light']) .project-file-edit-button,
  :global(:root[data-color-scheme='light']) .project-file-editor-actions button {
    border-color: rgba(85, 104, 120, 0.14);
    background: rgba(85, 104, 120, 0.055);
    color: rgba(29, 39, 49, 0.78);
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

  .project-file-kind-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
    border-radius: 6px;
    padding: 3px 7px;
    background: rgba(255, 255, 255, 0.045);
    color: rgba(231, 238, 247, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.2;
  }

  .project-file-layer-strip span[data-present='true'] {
    color: color-mix(in srgb, var(--positive, #6BC785) 78%, white);
  }

  :global(:root[data-color-scheme='light']) .project-file-kind-chip {
    background: rgba(85, 104, 120, 0.06);
    color: rgba(82, 98, 111, 0.72);
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

  .project-preview-mode-tabs button:disabled {
    cursor: default;
    opacity: 0.42;
  }

  .project-preview-mode-tabs button.project-preview-mode-active {
    background: rgba(255, 255, 255, 0.075);
    color: rgba(239, 244, 251, 0.86);
  }

  :global(:root[data-color-scheme='light']) .project-preview-mode-tabs {
    border-color: rgba(85, 104, 120, 0.13);
    background: rgba(85, 104, 120, 0.045);
  }

  :global(:root[data-color-scheme='light']) .project-preview-mode-tabs button {
    color: rgba(57, 70, 82, 0.58);
  }

  :global(:root[data-color-scheme='light']) .project-preview-mode-tabs button.project-preview-mode-active {
    background: rgba(255, 255, 255, 0.7);
    color: rgba(29, 39, 49, 0.86);
  }

  .project-preview-layers {
    display: grid;
    gap: 9px;
    min-width: 0;
  }

  .project-preview-layer {
    display: grid;
    gap: 7px;
    min-width: 0;
    border-top: 1px solid rgba(255, 255, 255, 0.055);
    padding-top: 9px;
  }

  :global(:root[data-color-scheme='light']) .project-preview-layer {
    border-top-color: rgba(85, 104, 120, 0.12);
  }

  .project-preview-layer-head strong {
    color: rgba(243, 247, 255, 0.86);
    font-size: 11px;
  }

  :global(:root[data-color-scheme='light']) .project-preview-layer-head strong {
    color: rgba(20, 29, 38, 0.86);
  }

  .project-preview-layer pre {
    max-height: min(58vh, 560px);
    min-width: 0;
    overflow: auto;
    margin: 0;
    border-radius: 7px;
    padding: 9px;
    background: rgba(6, 10, 15, 0.34);
    color: rgba(240, 245, 251, 0.82);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.5;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  :global(:root[data-color-scheme='light']) .project-preview-layer pre {
    background: rgba(246, 248, 248, 0.95);
    color: rgba(28, 36, 45, 0.86);
  }

  .project-rich-compare {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
    min-width: 0;
  }

  .project-rich-compare-item {
    display: grid;
    align-content: start;
    gap: 7px;
    min-width: 0;
  }

  .project-rich-preview {
    min-height: min(58vh, 520px);
    max-height: min(72vh, 680px);
    overflow: hidden;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    background:
      linear-gradient(135deg, rgba(255, 255, 255, 0.055), rgba(255, 255, 255, 0.018)),
      rgba(5, 9, 14, 0.34);
  }

  .project-rich-preview img,
  .project-rich-preview iframe,
  .project-rich-preview video {
    display: block;
    width: 100%;
    height: 100%;
    min-height: min(58vh, 520px);
    border: 0;
    background: rgba(255, 255, 255, 0.94);
  }

  .project-rich-preview img,
  .project-rich-preview video {
    object-fit: contain;
    background: rgba(5, 9, 14, 0.82);
  }

  :global(:root[data-color-scheme='light']) .project-rich-preview {
    border-color: rgba(85, 104, 120, 0.12);
    background: rgba(246, 248, 248, 0.95);
  }

  @media (max-width: 980px) {
    .project-rich-compare {
      grid-template-columns: minmax(0, 1fr);
    }
  }

  .project-markdown-preview {
    max-height: 520px;
    min-width: 0;
    overflow: auto;
    border-radius: 8px;
    padding: 14px;
    background: rgba(6, 10, 15, 0.28);
    color: rgba(240, 245, 251, 0.88);
    font-size: 12px;
    line-height: 1.6;
  }

  :global(:root[data-color-scheme='light']) .project-markdown-preview {
    background: rgba(246, 248, 248, 0.95);
    color: rgba(28, 36, 45, 0.9);
  }

  .project-sheet-preview {
    max-height: min(54vh, 520px);
    overflow: auto;
    border: 1px solid rgba(255, 255, 255, 0.065);
    border-radius: 8px;
    background: rgba(6, 10, 15, 0.3);
  }

  .project-sheet-preview table {
    width: max-content;
    min-width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10.5px;
    line-height: 1.35;
  }

  .project-sheet-preview th,
  .project-sheet-preview td {
    min-width: 118px;
    max-width: 320px;
    border-right: 1px solid rgba(255, 255, 255, 0.055);
    border-bottom: 1px solid rgba(255, 255, 255, 0.055);
    padding: 7px 9px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: left;
    vertical-align: top;
  }

  .project-sheet-preview thead th {
    position: sticky;
    top: 0;
    z-index: 2;
    background: rgba(255, 255, 255, 0.05);
    color: rgba(240, 245, 251, 0.88);
    font-weight: 650;
  }

  .project-sheet-preview .project-sheet-index {
    position: sticky;
    left: 0;
    z-index: 1;
    width: 46px;
    min-width: 46px;
    max-width: 46px;
    background: rgba(12, 18, 26, 0.92);
    color: rgba(180, 194, 208, 0.78);
    text-align: right;
    font-weight: 600;
  }

  .project-sheet-preview thead .project-sheet-index {
    z-index: 3;
  }

  .project-sheet-preview .project-sheet-index span {
    margin-right: 5px;
    font-weight: 800;
  }

  .project-sheet-preview tbody tr[data-kind='added'] td {
    background: rgba(47, 166, 107, 0.16);
    color: rgba(218, 248, 231, 0.94);
  }

  .project-sheet-preview tbody tr[data-kind='removed'] td {
    background: rgba(220, 92, 92, 0.15);
    color: rgba(255, 223, 223, 0.94);
  }

  .project-sheet-preview tbody tr[data-kind='context'] td {
    color: rgba(223, 231, 240, 0.78);
  }

  :global(:root[data-color-scheme='light']) .project-sheet-preview {
    border-color: rgba(85, 104, 120, 0.12);
    background: rgba(246, 248, 248, 0.95);
  }

  :global(:root[data-color-scheme='light']) .project-sheet-preview th,
  :global(:root[data-color-scheme='light']) .project-sheet-preview td {
    border-color: rgba(85, 104, 120, 0.12);
  }

  :global(:root[data-color-scheme='light']) .project-sheet-preview thead th {
    background: rgba(85, 104, 120, 0.075);
    color: rgba(20, 29, 38, 0.88);
  }

  :global(:root[data-color-scheme='light']) .project-sheet-preview .project-sheet-index {
    background: rgba(250, 251, 250, 0.96);
    color: rgba(82, 98, 111, 0.74);
  }

  :global(:root[data-color-scheme='light']) .project-sheet-preview tbody tr[data-kind='added'] td {
    background: rgba(47, 166, 107, 0.14);
    color: rgba(17, 92, 54, 0.94);
  }

  :global(:root[data-color-scheme='light']) .project-sheet-preview tbody tr[data-kind='removed'] td {
    background: rgba(220, 92, 92, 0.13);
    color: rgba(134, 34, 42, 0.94);
  }

  :global(:root[data-color-scheme='light']) .project-sheet-preview tbody tr[data-kind='context'] td {
    color: rgba(57, 70, 82, 0.82);
  }

  .project-code-preview {
    border-left: 3px solid rgba(157, 194, 255, 0.34);
  }

  .project-code-preview[data-kind='data'],
  .project-code-preview[data-kind='graph'] {
    border-left-color: rgba(231, 188, 119, 0.42);
  }

  .project-binary-preview {
    display: grid;
    justify-items: center;
    gap: 8px;
    min-height: 220px;
    border-radius: 8px;
    padding: 26px 18px;
    background:
      linear-gradient(135deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.018)),
      rgba(6, 10, 15, 0.28);
    color: rgba(231, 238, 247, 0.68);
    text-align: center;
  }

  .project-binary-preview strong {
    color: rgba(243, 247, 255, 0.9);
    font-size: 12px;
  }

  .project-binary-preview p {
    max-width: 340px;
    margin: 0;
    font-size: 11px;
    line-height: 1.5;
  }

  .project-binary-preview a {
    border-radius: 7px;
    padding: 5px 9px;
    background: rgba(255, 255, 255, 0.06);
    color: rgba(240, 245, 251, 0.88);
    font-size: 11px;
    text-decoration: none;
  }

  :global(:root[data-color-scheme='light']) .project-binary-preview {
    background: rgba(246, 248, 248, 0.95);
    color: rgba(82, 98, 111, 0.72);
  }

  :global(:root[data-color-scheme='light']) .project-binary-preview strong {
    color: rgba(20, 29, 38, 0.9);
  }

  :global(:root[data-color-scheme='light']) .project-binary-preview a {
    background: rgba(85, 104, 120, 0.08);
    color: rgba(29, 39, 49, 0.84);
  }

  .project-diff-lines {
    display: grid;
    max-height: 360px;
    min-width: 0;
    overflow: auto;
    border-radius: 7px;
    background: rgba(6, 10, 15, 0.34);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.5;
  }

  :global(:root[data-color-scheme='light']) .project-diff-lines {
    background: rgba(246, 248, 248, 0.95);
  }

  .project-diff-line {
    display: grid;
    grid-template-columns: 20px minmax(0, 1fr);
    min-width: 0;
    padding: 0 8px 0 0;
    color: rgba(240, 245, 251, 0.82);
  }

  .project-diff-line[data-kind='removed'] {
    background: rgba(220, 92, 103, 0.16);
    color: #f1b0b7;
  }

  .project-diff-line[data-kind='added'] {
    background: rgba(72, 181, 118, 0.16);
    color: #a7e5bd;
  }

  :global(:root[data-color-scheme='light']) .project-diff-line {
    color: rgba(28, 36, 45, 0.86);
  }

  :global(:root[data-color-scheme='light']) .project-diff-line[data-kind='removed'] {
    background: rgba(207, 73, 87, 0.12);
    color: rgba(132, 37, 49, 0.92);
  }

  :global(:root[data-color-scheme='light']) .project-diff-line[data-kind='added'] {
    background: rgba(35, 151, 86, 0.13);
    color: rgba(21, 100, 56, 0.94);
  }

  .project-diff-marker {
    display: inline-flex;
    justify-content: center;
    user-select: none;
    opacity: 0.72;
  }

  .project-diff-line code {
    min-width: 0;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
  }

  .project-file-editor {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .project-file-editor textarea {
    width: 100%;
    min-height: 280px;
    resize: vertical;
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 7px;
    padding: 10px;
    background: rgba(6, 10, 15, 0.34);
    color: rgba(240, 245, 251, 0.86);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 11px;
    line-height: 1.55;
    outline: none;
    white-space: pre-wrap;
  }

  .project-file-editor textarea:focus {
    border-color: color-mix(in srgb, var(--thread-accent, #57CFA0) 45%, transparent);
  }

  :global(:root[data-color-scheme='light']) .project-file-editor textarea {
    border-color: rgba(85, 104, 120, 0.14);
    background: rgba(246, 248, 248, 0.95);
    color: rgba(28, 36, 45, 0.9);
  }

  .project-file-editor-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
  }

  .project-file-editor-actions button[data-primary='true'] {
    border-color: color-mix(in srgb, var(--thread-accent, #57CFA0) 28%, transparent);
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 14%, transparent);
    color: color-mix(in srgb, var(--thread-accent, #57CFA0) 78%, white);
  }

  .project-file-save-message {
    border-radius: 7px;
    padding: 7px 8px;
    font-size: 11px;
    line-height: 1.35;
  }

  .project-file-save-message[data-tone='clean'] {
    background: color-mix(in srgb, var(--positive, #6BC785) 12%, transparent);
    color: color-mix(in srgb, var(--positive, #6BC785) 78%, white);
  }

  .project-file-save-message[data-tone='warning'] {
    background: rgba(236, 180, 95, 0.11);
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

  @media (max-width: 720px) {
    .project-browser-layout {
      flex-direction: column;
    }

    .project-file-tree,
    .project-file-tree:hover,
    .project-file-tree:focus-within {
      flex-basis: auto;
      max-height: 260px;
      border-right: 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.055);
    }

    .project-tree-rail {
      display: none;
    }

    .project-tree-list {
      position: static;
      clip-path: none;
      pointer-events: auto;
      visibility: visible;
    }

    :global(:root[data-color-scheme='light']) .project-file-tree {
      border-bottom-color: rgba(85, 104, 120, 0.12);
    }

    .project-file-preview {
      max-height: none;
    }
  }
</style>
