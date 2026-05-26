<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    getIdeaProjectDraftFile,
    getIdeaProjectDraftState,
    updateIdeaProjectDraftFile,
  } from '$lib/features/threads/api/threadApi';
  import {
    buildProjectDraftPanelView,
    buildProjectFilePreviewView,
    PROJECT_DRAFT_CHANGE_METRICS,
    projectDirectoryAncestorKeys,
    projectFileLayerLabel,
    projectFileSizeLabel,
    projectFileStatusLabel,
    projectFileStatusTone,
    visibleProjectExplorerRows,
    type ProjectExplorerFile,
    type ProjectExplorerDirectory,
    type ProjectExplorerRow,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
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
  const selectedFile = $derived(
    fileBrowser.files.find((file) => file.key === selectedFileKey) ?? null,
  );
  const visibleRows = $derived(visibleProjectExplorerRows(fileBrowser.rows, collapsedDirectoryKeys));
  const previewView = $derived(buildProjectFilePreviewView(filePreview, selectedFile));
  const isEditingSelectedFile = $derived(Boolean(selectedFile && editingFileKey === selectedFile.key));
  const activePreviewMode = $derived(previewMode === 'review' && previewView.mode === 'diff' ? 'review' : 'final');

  function metricCountLabel(value: number): string {
    return Number.isFinite(value) ? String(value) : '0';
  }

  function rowStyle(row: ProjectExplorerRow): string {
    return `--depth: ${Math.min(row.depth, 8)}`;
  }

  function rowTone(row: ProjectExplorerRow): ReturnType<typeof projectFileStatusTone> {
    return projectFileStatusTone(row.status);
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
      const result = await updateIdeaProjectDraftFile(ideaId, {
        runId: currentRunId,
        resourceId: file.resourceId,
        path: file.path,
        content: editorContent,
      });
      filePreview = result;
      editingFileKey = '';
      editorContent = '';
      loadedFileKey = `${ideaId}:${currentRunId ?? ''}:${file.resourceId}:${file.path}`;
      fileSaveNotice = 'Thread draft saved.';
      previewMode = 'final';
      await loadDraftState(ideaId, currentRunId);
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

  async function loadDraftState(ideaId: string, currentRunId: string | number | null) {
    const requestId = ++requestSeq;
    loading = true;
    loadError = '';
    try {
      const result = await getIdeaProjectDraftState(ideaId, { runId: currentRunId });
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
  ) {
    const requestId = ++fileRequestSeq;
    filePreviewLoading = true;
    filePreviewError = '';
    try {
      const result = await getIdeaProjectDraftFile(ideaId, {
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
    const currentRunId = runId ?? null;
    const key = `${ideaId ?? ''}:${currentRunId ?? ''}`;
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
    void loadDraftState(ideaId, currentRunId);
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
    const key = `${ideaId}:${currentRunId ?? ''}:${file.resourceId}:${file.path}`;
    if (loadedFileKey === key) return;
    loadedFileKey = key;
    void loadFilePreview(ideaId, currentRunId, file);
  });
</script>

<section class="project-draft-panel" aria-label="Project workspace">
  <div class="project-draft-summary">
    <div class="project-draft-kicker">Project</div>
    <div class="project-draft-title-row">
      <span class="project-draft-title">Root + thread overlay</span>
      <span class="project-draft-signal" data-tone={signalTone}>{signalLabel}</span>
    </div>
    <div class="project-draft-meta">
      <span>{runLabel}</span>
      <span>{resources.length} resource{resources.length === 1 ? '' : 's'}</span>
      <span>{fileBrowser.fileCount} file{fileBrowser.fileCount === 1 ? '' : 's'}</span>
      <span>{readiness.detail}</span>
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
    <div class="project-draft-counts" aria-label="Draft change counts">
      {#each CHANGE_METRICS as metric (metric.key)}
        <div class="project-draft-count" data-tone={metric.tone}>
          <span>{metric.label}</span>
          <strong>{metricCountLabel(aggregateCounts[metric.key])}</strong>
        </div>
      {/each}
    </div>

    {#if outOfDatePaths.length > 0}
      <div class="project-draft-alert" data-tone="warning">
        <strong>Out of date</strong>
        <span>{outOfDatePaths.length} path{outOfDatePaths.length === 1 ? '' : 's'} need attention.</span>
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
        <div class="project-browser-legend" aria-label="Project layers">
          <span><ConstellationIcon name="lock" size={12} /> Root</span>
          <span><ConstellationIcon name="edit" size={12} /> Draft</span>
        </div>
      </div>

      {#if fileBrowser.rows.length === 0}
        <div class="project-draft-empty">No browsable files found in this Project.</div>
      {:else}
        <div class="project-browser-layout">
          <div class="project-file-tree" aria-label="Project files">
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
                  <span class="project-tree-folder-glyph" aria-hidden="true">
                    <ConstellationIcon name={directoryCollapsed(row) ? 'chevron-right' : 'chevron-down'} size={11} />
                    <ConstellationIcon name="folder" size={14} />
                  </span>
                  <span title={row.displayPath}>{row.name}</span>
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
                  <ConstellationIcon name={row.extension === '.md' ? 'document' : 'file'} size={14} />
                  <span>{row.name}</span>
                  <small>{projectFileStatusLabel(row.status)}</small>
                </button>
              {/if}
            {/each}
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
                  <button
                    type="button"
                    class="project-file-edit-button"
                    disabled={!previewView.canEdit || filePreviewLoading || fileSaveLoading}
                    title={previewView.canEdit ? 'Edit this file in the thread draft' : 'This file cannot be edited as text here'}
                    onclick={isEditingSelectedFile ? cancelFileEdit : beginFileEdit}
                  >
                    <ConstellationIcon name={isEditingSelectedFile ? 'close' : 'edit'} size={12} />
                    <span>{isEditingSelectedFile ? 'Cancel' : 'Edit'}</span>
                  </button>
                  <span class="project-file-status" data-tone={projectFileStatusTone(selectedFile.status)}>
                    {projectFileStatusLabel(selectedFile.status)}
                  </span>
                </div>
              </div>

              <div class="project-file-layer-strip" aria-label="Selected file layers">
                <span data-present={selectedFile.has_root === true}>Root</span>
                <span data-present={selectedFile.has_base === true}>Base</span>
                <span data-present={selectedFile.has_draft === true}>Draft</span>
              </div>

              <div class="project-preview-mode-tabs" aria-label="Project file preview mode">
                <button
                  type="button"
                  class:project-preview-mode-active={activePreviewMode === 'review'}
                  disabled={previewView.mode !== 'diff' || isEditingSelectedFile}
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
                {#if activePreviewMode === 'review' && previewView.mode === 'diff'}
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
                {:else if previewView.finalLayer}
                  <div class="project-preview-layers">
                    <div class="project-preview-layer" data-layer={previewView.finalLayer.key}>
                      <div class="project-preview-layer-head">
                        <strong>{previewView.finalLayer.label}</strong>
                        <span>{previewView.finalLayer.detail}</span>
                      </div>
                      <pre>{previewView.finalLayer.content}</pre>
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

    <div class="project-publish-mini">
      <div class="project-draft-kicker">Publish plan</div>
      <div class="project-publish-metrics">
        <span><strong>{publishPlan.resourceCount}</strong> resources</span>
        <span><strong>{publishPlan.operationCount}</strong> operations</span>
        <span data-tone={publishPlan.blockedCount > 0 ? 'conflicted' : 'clean'}>
          <strong>{publishPlan.blockedCount}</strong> blocked
        </span>
        <span><strong>{publishPlan.readyCount}</strong> ready</span>
      </div>
    </div>
  {/if}
</section>

<style>
  .project-draft-panel {
    display: flex;
    flex-direction: column;
    gap: 10px;
    width: 100%;
    min-width: 0;
    color: rgba(239, 244, 251, 0.86);
    font-size: 12px;
  }

  :global(:root[data-color-scheme='light']) .project-draft-panel {
    color: rgba(29, 39, 49, 0.86);
  }

  .project-draft-summary,
  .project-draft-alert,
  .project-browser,
  .project-publish-mini {
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.03);
  }

  :global(:root[data-color-scheme='light']) .project-draft-summary,
  :global(:root[data-color-scheme='light']) .project-draft-alert,
  :global(:root[data-color-scheme='light']) .project-browser,
  :global(:root[data-color-scheme='light']) .project-publish-mini {
    border-color: rgba(85, 104, 120, 0.13);
    background: rgba(250, 250, 246, 0.78);
  }

  .project-draft-summary,
  .project-publish-mini {
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

  .project-draft-counts,
  .project-publish-metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
  }

  .project-draft-count,
  .project-publish-metrics span {
    display: grid;
    gap: 5px;
    min-width: 0;
    min-height: 48px;
    padding: 9px 8px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.035);
    border: 1px solid rgba(255, 255, 255, 0.04);
  }

  :global(:root[data-color-scheme='light']) .project-draft-count,
  :global(:root[data-color-scheme='light']) .project-publish-metrics span {
    border-color: rgba(85, 104, 120, 0.12);
    background: rgba(248, 250, 248, 0.74);
  }

  .project-draft-count span,
  .project-publish-metrics span {
    color: rgba(231, 238, 247, 0.5);
    font-size: 9px;
    line-height: 1.2;
  }

  .project-draft-count strong,
  .project-publish-metrics strong {
    color: rgba(243, 247, 255, 0.92);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 16px;
    line-height: 1;
  }

  :global(:root[data-color-scheme='light']) .project-draft-count span,
  :global(:root[data-color-scheme='light']) .project-publish-metrics span {
    color: rgba(82, 98, 111, 0.64);
  }

  :global(:root[data-color-scheme='light']) .project-draft-count strong,
  :global(:root[data-color-scheme='light']) .project-publish-metrics strong {
    color: rgba(20, 29, 38, 0.9);
  }

  .project-draft-count[data-tone='conflicted'] strong,
  .project-publish-metrics [data-tone='conflicted'] strong {
    color: #efa5b0;
  }

  .project-draft-count[data-tone='new'] strong {
    color: color-mix(in srgb, var(--positive, #6BC785) 82%, white);
  }

  .project-draft-count[data-tone='deleted'] strong {
    color: #e7bc77;
  }

  .project-draft-alert {
    display: grid;
    gap: 3px;
    padding: 10px 11px;
  }

  .project-draft-alert strong {
    color: rgba(243, 247, 255, 0.9);
    font-size: 12px;
  }

  .project-draft-alert span {
    color: rgba(231, 238, 247, 0.58);
    font-size: 11px;
    line-height: 1.35;
  }

  .project-draft-alert[data-tone='warning'] {
    border-color: rgba(236, 180, 95, 0.18);
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
    display: grid;
    grid-template-columns: minmax(170px, 0.9fr) minmax(0, 1.2fr);
    min-height: 420px;
  }

  .project-file-tree {
    min-width: 0;
    max-height: 540px;
    overflow: auto;
    padding: 6px;
    border-right: 1px solid rgba(255, 255, 255, 0.055);
  }

  :global(:root[data-color-scheme='light']) .project-file-tree {
    border-right-color: rgba(85, 104, 120, 0.12);
  }

  .project-tree-row {
    --depth: 0;
    display: grid;
    grid-template-columns: 26px minmax(0, 1fr) auto;
    align-items: center;
    gap: 7px;
    width: 100%;
    min-height: 30px;
    margin: 1px 0;
    padding: 5px 7px 5px calc(7px + (var(--depth) * 14px));
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

  .project-tree-folder-glyph {
    display: inline-flex;
    align-items: center;
    gap: 1px;
    min-width: 0;
    overflow: visible;
  }

  .project-tree-row span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
  }

  .project-tree-row .project-tree-folder-glyph {
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
    align-content: start;
    gap: 9px;
    min-width: 0;
    max-height: 540px;
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

  .project-file-layer-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }

  .project-file-layer-strip span[data-present='true'] {
    color: color-mix(in srgb, var(--positive, #6BC785) 78%, white);
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
    max-height: 260px;
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

  .project-publish-mini {
    display: grid;
    gap: 9px;
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
    .project-draft-counts,
    .project-publish-metrics {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .project-browser-layout {
      grid-template-columns: minmax(0, 1fr);
    }

    .project-file-tree {
      max-height: 260px;
      border-right: 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.055);
    }

    :global(:root[data-color-scheme='light']) .project-file-tree {
      border-bottom-color: rgba(85, 104, 120, 0.12);
    }

    .project-file-preview {
      max-height: none;
    }
  }
</style>
