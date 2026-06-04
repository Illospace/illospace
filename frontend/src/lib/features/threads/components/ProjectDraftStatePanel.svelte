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
    PROJECT_DRAFT_CHANGE_METRICS,
    projectDirectoryAncestorKeys,
    projectFileStatusLabel,
    projectFileStatusTone,
    filterProjectSelectorItems,
    projectSelectorOptions,
    visibleProjectExplorerRows,
    type ProjectExplorerFile,
    type ProjectExplorerDirectory,
    type ProjectExplorerRow,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import ProjectDraftFilePreview from './ProjectDraftFilePreview.svelte';
  import type {
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
  const selectedFile = $derived(
    fileBrowser.files.find((file) => file.key === selectedFileKey) ?? null,
  );
  const visibleRows = $derived(visibleProjectExplorerRows(fileBrowser.rows, collapsedDirectoryKeys));

  function metricCountLabel(value: number): string {
    return Number.isFinite(value) ? String(value) : '0';
  }

  function rowStyle(row: ProjectExplorerRow): string {
    return `--depth: ${Math.min(row.depth, 8)}`;
  }

  function rowTone(row: ProjectExplorerRow): ReturnType<typeof projectFileStatusTone> {
    return projectFileStatusTone(row.status);
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
    collapsedDirectoryKeys = [];
  }

  function selectFile(file: ProjectExplorerFile) {
    selectedFileKey = file.key;
  }

  function directoryCollapsed(row: ProjectExplorerDirectory): boolean {
    return collapsedDirectoryKeys.includes(row.key);
  }

  function toggleDirectory(row: ProjectExplorerDirectory) {
    collapsedDirectoryKeys = directoryCollapsed(row)
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
          <ProjectDraftFilePreview
            {idea}
            runId={runId ?? statePayload?.run_id ?? null}
            projectProfileId={selectedProjectProfileId}
            selectedFile={selectedFile}
            onDraftStateChanged={reloadDraftStateAfterFileSave}
          />
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
  .project-browser-head {
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

  .project-draft-signal {
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

  .project-draft-signal[data-tone='clean'] {
    background: color-mix(in srgb, var(--positive, #6BC785) 16%, transparent);
    color: color-mix(in srgb, var(--positive, #6BC785) 82%, white);
  }

  .project-draft-signal[data-tone='modified'] {
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 14%, transparent);
    color: color-mix(in srgb, var(--thread-accent, #57CFA0) 78%, white);
  }

  .project-draft-signal[data-tone='warning'] {
    background: rgba(236, 180, 95, 0.13);
    color: #e7bc77;
  }

  .project-draft-signal[data-tone='conflict'] {
    background: rgba(212, 128, 143, 0.14);
    color: #efa5b0;
  }

  .project-draft-meta,
  .project-browser-head span {
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
  .project-browser-head span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  :global(:root[data-color-scheme='light']) .project-draft-meta,
  :global(:root[data-color-scheme='light']) .project-browser-head span {
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

  }
</style>
