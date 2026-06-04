<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import {
    PROJECT_DRAFT_CHANGE_METRICS,
    type ProjectSelectorItem,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import type { ProjectDraftChangeKey } from '$lib/api/client';

  let {
    projectSelectorOpen = $bindable(false),
    projectSelectorQuery = $bindable(''),
    projectsLoading = false,
    projectSelectorItems = [],
    filteredProjectSelectorItems = [],
    selectedProjectProfileId = '',
    selectedProjectLabel = 'Project',
    selectedProjectSubtitle = '',
    selectedProjectContentLabels = [],
    signalTone = 'clean',
    signalLabel = '',
    runLabel = '',
    resourcesCount = 0,
    fileCount = 0,
    readinessDetail = '',
    aggregateCounts = {},
    publishOperationCount = 0,
    publishBlockedCount = 0,
    onSelectProjectProfile,
  }: {
    projectSelectorOpen: boolean;
    projectSelectorQuery: string;
    projectsLoading?: boolean;
    projectSelectorItems?: ProjectSelectorItem[];
    filteredProjectSelectorItems?: ProjectSelectorItem[];
    selectedProjectProfileId?: string;
    selectedProjectLabel?: string;
    selectedProjectSubtitle?: string;
    selectedProjectContentLabels?: string[];
    signalTone?: string;
    signalLabel?: string;
    runLabel?: string;
    resourcesCount?: number;
    fileCount?: number;
    readinessDetail?: string;
    aggregateCounts?: Partial<Record<ProjectDraftChangeKey, number>>;
    publishOperationCount?: number;
    publishBlockedCount?: number;
    onSelectProjectProfile: (projectId: string) => void;
  } = $props();

  const CHANGE_METRICS = PROJECT_DRAFT_CHANGE_METRICS;

  function metricCountLabel(value: number | undefined): string {
    return Number.isFinite(value) ? String(value) : '0';
  }
</script>

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
                        onclick={() => onSelectProjectProfile(project.id)}
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
    <span>{resourcesCount} resource{resourcesCount === 1 ? '' : 's'}</span>
    <span>{fileCount} file{fileCount === 1 ? '' : 's'}</span>
    <span>{readinessDetail}</span>
  </div>
  <div class="project-draft-summary-chips" aria-label="Project draft summary">
    {#each CHANGE_METRICS as metric (metric.key)}
      <span data-tone={metric.tone}>
        <strong>{metricCountLabel(aggregateCounts[metric.key])}</strong>
        {metric.label}
      </span>
    {/each}
    <span data-tone={publishBlockedCount > 0 ? 'conflicted' : 'clean'}>
      <strong>{publishOperationCount}</strong>
      publish ops
    </span>
  </div>
</div>

<style>
  .project-draft-summary {
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    padding: 12px;
    background: rgba(255, 255, 255, 0.03);
  }

  :global(:root[data-color-scheme='light']) .project-draft-summary {
    border-color: rgba(85, 104, 120, 0.13);
    background: rgba(250, 250, 246, 0.78);
  }

  .project-draft-kicker {
    margin: 0;
    color: rgba(240, 240, 250, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  :global(:root[data-color-scheme='light']) .project-draft-kicker {
    color: rgba(82, 98, 111, 0.66);
  }

  .project-draft-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
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

  .project-draft-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 10px;
    margin-top: 7px;
    color: rgba(231, 238, 247, 0.52);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.35;
  }

  .project-draft-meta span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  :global(:root[data-color-scheme='light']) .project-draft-meta {
    color: rgba(82, 98, 111, 0.66);
  }

  .project-draft-summary-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 9px;
  }

  .project-draft-summary-chips span {
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

  :global(:root[data-color-scheme='light']) .project-draft-summary-chips span {
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
</style>
