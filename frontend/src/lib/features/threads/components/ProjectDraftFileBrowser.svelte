<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    projectFileStatusLabel,
    projectFileStatusTone,
    type ProjectExplorerDirectory,
    type ProjectExplorerFile,
    type ProjectExplorerRow,
    type ProjectFileBrowserView,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import ProjectDraftFilePreview from './ProjectDraftFilePreview.svelte';

  type DraftIdea = {
    id?: string | null;
  } | null;

  let {
    idea,
    runId = null,
    projectProfileId = '',
    fileBrowser,
    visibleRows = [],
    selectedFileKey = '',
    selectedFile = null,
    collapsedDirectoryKeys = [],
    onSelectFile,
    onToggleDirectory,
    onDraftStateChanged,
  }: {
    idea: DraftIdea;
    runId?: string | number | null;
    projectProfileId?: string;
    fileBrowser: ProjectFileBrowserView;
    visibleRows?: ProjectExplorerRow[];
    selectedFileKey?: string;
    selectedFile?: ProjectExplorerFile | null;
    collapsedDirectoryKeys?: string[];
    onSelectFile: (file: ProjectExplorerFile) => void;
    onToggleDirectory: (directory: ProjectExplorerDirectory) => void;
    onDraftStateChanged?: () => void | Promise<void>;
  } = $props();

  function rowStyle(row: ProjectExplorerRow): string {
    return `--depth: ${Math.min(row.depth, 8)}`;
  }

  function rowTone(row: ProjectExplorerRow): ReturnType<typeof projectFileStatusTone> {
    return projectFileStatusTone(row.status);
  }

  function directoryCollapsed(row: ProjectExplorerDirectory): boolean {
    return collapsedDirectoryKeys.includes(row.key);
  }
</script>

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
                onclick={() => onToggleDirectory(row)}
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
                onclick={() => onSelectFile(row)}
              >
                <span class="project-tree-label">{row.name}</span>
                <small>{projectFileStatusLabel(row.status)}</small>
              </button>
            {/if}
          {/each}
        </div>
      </div>
      <ProjectDraftFilePreview
        {idea}
        {runId}
        {projectProfileId}
        {selectedFile}
        {onDraftStateChanged}
      />
    </div>
  {/if}
</div>

<style>
  .project-browser {
    display: grid;
    gap: 0;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.03);
  }

  :global(:root[data-color-scheme='light']) .project-browser {
    border-color: rgba(85, 104, 120, 0.13);
    background: rgba(250, 250, 246, 0.78);
  }

  .project-browser-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
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

  .project-browser-head h4 {
    margin: 0;
    color: rgba(240, 240, 250, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  :global(:root[data-color-scheme='light']) .project-browser-head h4 {
    color: rgba(82, 98, 111, 0.66);
  }

  .project-browser-head span {
    min-width: 0;
    overflow-wrap: anywhere;
    color: rgba(231, 238, 247, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
  }

  :global(:root[data-color-scheme='light']) .project-browser-head span {
    color: rgba(82, 98, 111, 0.66);
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

  .project-browser-legend span {
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

  :global(:root[data-color-scheme='light']) .project-browser-legend span {
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
    grid-template-columns: 14px minmax(0, 1fr) auto;
    padding-left: calc(7px + (var(--depth) * 14px));
    color: rgba(231, 238, 247, 0.58);
  }

  :global(:root[data-color-scheme='light']) .project-tree-directory {
    color: rgba(82, 98, 111, 0.72);
  }

  .project-tree-chevron {
    display: inline-flex;
    align-items: center;
    justify-content: center;
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

  .project-draft-empty {
    color: rgba(231, 238, 247, 0.48);
    font-size: 12px;
    line-height: 1.55;
    padding: 10px 12px;
  }

  :global(:root[data-color-scheme='light']) .project-draft-empty {
    color: rgba(82, 98, 111, 0.68);
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
  }
</style>
