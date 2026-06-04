<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    projectFileExtension,
    projectFileStatusTone,
    projectSpreadsheetPreviewRows,
    type ProjectExplorerFile,
    type ProjectFileKind,
    type ProjectFilePreviewView,
    type ProjectPreviewLayerKey,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';
  import {
    canEmbedProjectFileKind,
    diffMarker,
    finalLayerSourceLabel,
    projectPreviewPrimaryTitle,
  } from './projectDraftFilePreviewPresentation';

  let {
    selectedFile,
    selectedFileKind,
    selectedFileIcon,
    selectedFileKindLabel,
    previewView,
    activePreviewMode,
    showPreviewModeTabs,
    filePreviewLoading = false,
    filePreviewError = '',
    fileSaveNotice = '',
    layerBlobUrl,
  }: {
    selectedFile: ProjectExplorerFile;
    selectedFileKind: ProjectFileKind;
    selectedFileIcon: ConstellationIconName;
    selectedFileKindLabel: string;
    previewView: ProjectFilePreviewView;
    activePreviewMode: 'review' | 'final';
    showPreviewModeTabs: boolean;
    filePreviewLoading?: boolean;
    filePreviewError?: string;
    fileSaveNotice?: string;
    layerBlobUrl: (layer: ProjectPreviewLayerKey | null) => string;
  } = $props();

  const selectedFileTone = $derived(projectFileStatusTone(selectedFile?.status));
  const selectedFileIsRich = $derived(canEmbedProjectFileKind(selectedFileKind));
  const rootPreviewLayer = $derived(previewView.layers.find((item) => item.key === 'root') ?? null);
  const draftPreviewLayer = $derived(previewView.layers.find((item) => item.key === 'draft') ?? null);
  const finalBlobUrl = $derived(layerBlobUrl(previewView.finalLayer?.key ?? null));
  const rootBlobUrl = $derived(layerBlobUrl('root'));
  const draftBlobUrl = $derived(layerBlobUrl('draft'));
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

  function sheetColumnIndexes(rows: string[][]): number[] {
    const count = rows.reduce((max, row) => Math.max(max, row.length), 0);
    return Array.from({ length: count }, (_value, index) => index);
  }

  function sheetHeaderCells(rows: string[][], columnIndexes: number[]): string[] {
    const header = rows[0] ?? [];
    return columnIndexes.map((columnIndex) => header[columnIndex] || `Column ${columnIndex + 1}`);
  }
</script>

{#if filePreviewLoading}
  <div class="project-draft-empty">Loading file preview...</div>
{:else if filePreviewError}
  <div class="project-draft-empty project-draft-empty-warning">{filePreviewError}</div>
{:else if previewView.mode === 'layers' && previewView.layers.length === 0}
  <div class="project-draft-empty">No readable preview for this file.</div>
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
          <strong>{projectPreviewPrimaryTitle(showPreviewModeTabs, previewView.finalLayer.key)}</strong>
          <span>{previewView.finalLayer.detail} / {finalLayerSourceLabel(previewView.finalLayer.key)}</span>
        </div>
        {#if canEmbedProjectFileKind(selectedFileKind) && finalBlobUrl}
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

<style>
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

  .project-preview-layer-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
  }

  .project-preview-layer-head strong {
    color: rgba(243, 247, 255, 0.86);
    font-size: 11px;
  }

  .project-preview-layer-head span {
    color: rgba(231, 238, 247, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
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

  .project-draft-empty {
    color: rgba(231, 238, 247, 0.48);
    font-size: 12px;
    line-height: 1.55;
    padding: 10px 0;
  }

  .project-draft-empty-warning {
    color: #e7bc77;
  }
</style>
