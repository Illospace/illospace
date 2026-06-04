<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
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
    projectSpreadsheetPreviewRows,
    type ProjectExplorerFile,
    type ProjectFileKind,
    type ProjectPreviewLayerKey,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';
  import type { ProjectDraftFileResponse } from '$lib/api/client';

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
  let lastSelectedFileKey = $state('');

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

  function diffMarker(kind: 'context' | 'removed' | 'added'): string {
    if (kind === 'added') return '+';
    if (kind === 'removed') return '-';
    return ' ';
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
    const fileKey = selectedFile?.key ?? '';
    if (lastSelectedFileKey === fileKey) return;
    lastSelectedFileKey = fileKey;
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

  .project-file-preview-head,
  .project-preview-layer-head {
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

  .project-file-preview-head span,
  .project-preview-layer-head span {
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

  .project-preview-layer-head strong {
    color: rgba(243, 247, 255, 0.86);
    font-size: 11px;
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
    border-collapse: collapse;
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10.5px;
    line-height: 1.35;
  }

  .project-sheet-preview th,
  .project-sheet-preview td {
    min-width: 118px;
    max-width: 320px;
    border: 1px solid rgba(255, 255, 255, 0.055);
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
    background: rgba(255, 255, 255, 0.05);
    color: rgba(240, 245, 251, 0.88);
    font-weight: 650;
  }

  .project-sheet-preview .project-sheet-index {
    position: sticky;
    left: 0;
    width: 46px;
    min-width: 46px;
    max-width: 46px;
    background: rgba(12, 18, 26, 0.92);
    color: rgba(180, 194, 208, 0.78);
    text-align: right;
    font-weight: 600;
  }

  .project-sheet-preview tbody tr[data-kind='added'] td {
    background: rgba(47, 166, 107, 0.16);
  }

  .project-sheet-preview tbody tr[data-kind='removed'] td {
    background: rgba(220, 92, 92, 0.15);
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

  .project-file-editor-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
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

  .project-draft-empty-warning {
    color: #e7bc77;
  }

  @media (max-width: 720px) {
    .project-file-preview {
      max-height: none;
    }
  }
</style>
