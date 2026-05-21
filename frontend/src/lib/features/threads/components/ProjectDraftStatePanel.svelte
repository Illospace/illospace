<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import { getIdeaProjectDraftState } from '$lib/features/threads/api/threadApi';
  import {
    buildProjectDraftPanelView,
    cleanLabel,
    countResourceChange,
    fileCountLabel,
    latestGroupVersion,
    PROJECT_DRAFT_CHANGE_METRICS,
    publishGroupTitle,
    publishOperationLabel,
    publishOperationPath,
    publishStatus,
    publishTargetLabel,
    resourceMeta,
    resourceStatus,
    resourceTitle,
    restoreTitle,
    versionTitle,
  } from '$lib/features/threads/domain/projectDraftStatePresenter';
  import { relativeTimeAgo } from '$lib/utils/datetime';
  import type {
    ProjectDraftStateResponse,
    ProjectDraftStateRead,
    ProjectRootVersionState,
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

  const draftView = $derived.by(() =>
    buildProjectDraftPanelView({ draftState, loading, loadError, runId }),
  );
  const statePayload = $derived(draftView.statePayload);
  const resources = $derived(draftView.resources);
  const aggregateCounts = $derived(draftView.aggregateCounts);
  const outOfDatePaths = $derived(draftView.outOfDatePaths);
  const fileGroups = $derived(draftView.fileGroups);
  const publishPlan = $derived(draftView.publishPlan);
  const rootVersions = $derived(draftView.rootVersions);
  const runLabel = $derived(draftView.runLabel);
  const readiness = $derived(draftView.readiness);
  const signalTone = $derived(readiness.tone);
  const signalLabel = $derived(readiness.label);

  function formatBytes(value: unknown): string {
    const size = Number(value);
    if (!Number.isFinite(size) || size <= 0) return '';
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function versionMeta(version: ProjectRootVersionState | null): string {
    if (!version) return 'No root versions yet';
    return [
      cleanLabel(version.label, 'Version'),
      version.created_at ? relativeTimeAgo(version.created_at) : '',
      formatBytes(version.total_size),
    ].filter(Boolean).join(' / ');
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
</script>

<section class="project-draft-panel" aria-label="Project draft state">
  <div class="project-draft-summary">
    <div class="project-draft-summary-main">
      <div class="project-draft-kicker">Project draft</div>
      <div class="project-draft-title-row">
        <span class="project-draft-title">Root + thread overlay</span>
        <span class="project-draft-signal" data-tone={signalTone}>{signalLabel}</span>
      </div>
      <div class="project-draft-meta">
        <span>{runLabel}</span>
        {#if resources.length > 0}
          <span>{resources.length} resource{resources.length === 1 ? '' : 's'}</span>
        {/if}
        <span>{readiness.detail}</span>
      </div>
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
    <div class="project-draft-layers" aria-label="Project workspace layers">
      <div class="project-draft-layer" data-layer="root">
        <div class="project-draft-layer-icon">
          <ConstellationIcon name="lock" size={14} />
        </div>
        <div>
          <strong>Project root</strong>
          <span>Read-only source</span>
        </div>
      </div>
      <div class="project-draft-layer-arrow" aria-hidden="true">
        <ConstellationIcon name="forward" size={14} />
      </div>
      <div class="project-draft-layer" data-layer="draft">
        <div class="project-draft-layer-icon">
          <ConstellationIcon name="edit" size={14} />
        </div>
        <div>
          <strong>Thread draft</strong>
          <span>Overlay workspace</span>
        </div>
      </div>
    </div>

    <div class="project-draft-counts" aria-label="Draft change counts">
      {#each CHANGE_METRICS as metric (metric.key)}
        <div class="project-draft-count" data-tone={metric.tone}>
          <span>{metric.label}</span>
          <strong>{aggregateCounts[metric.key]}</strong>
        </div>
      {/each}
    </div>

    {#if outOfDatePaths.length > 0}
      <div class="project-draft-alert" data-tone="warning">
        <strong>Out of date</strong>
        <span>{outOfDatePaths.length} path{outOfDatePaths.length === 1 ? '' : 's'} need attention.</span>
      </div>
    {/if}

    {#if aggregateCounts.conflicted_paths > 0}
      <div class="project-draft-alert" data-tone="conflict">
        <strong>Conflicts</strong>
        <span>{aggregateCounts.conflicted_paths} conflicted path{aggregateCounts.conflicted_paths === 1 ? '' : 's'} detected.</span>
      </div>
    {/if}

    <div class="project-draft-section">
      <div class="project-draft-section-head">
        <h4>Files</h4>
        <span>{fileGroups.length}</span>
      </div>
      {#if fileGroups.length === 0}
        <div class="project-draft-empty">No changed files in the thread draft.</div>
      {:else}
        <div class="project-draft-file-list">
          {#each fileGroups as group (group.key)}
            <div class="project-draft-path-group" data-tone={group.tone}>
              <div class="project-draft-path-label">
                <span>{group.label}</span>
                <strong>{group.paths.length}</strong>
              </div>
              <div class="project-draft-paths">
                {#each group.paths.slice(0, 10) as path (path)}
                  <code title={path}>{path}</code>
                {/each}
                {#if group.paths.length > 10}
                  <span class="project-draft-more">+{group.paths.length - 10} more</span>
                {/if}
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>

    <div class="project-draft-section">
      <div class="project-draft-section-head">
        <h4>Resources</h4>
        <span>{resources.length}</span>
      </div>
      {#if resources.length === 0}
        <div class="project-draft-empty">No Project resources found for this run.</div>
      {:else}
        <div class="project-draft-resource-list">
          {#each resources as resource (resource.id)}
            <div class="project-draft-resource" data-status={resourceStatus(resource)}>
              <div class="project-draft-resource-head">
                <div class="project-draft-resource-copy">
                  <h5>{resourceTitle(resource)}</h5>
                  {#if resourceMeta(resource)}
                    <p>{resourceMeta(resource)}</p>
                  {/if}
                </div>
                <span class="project-draft-resource-status">{resourceStatus(resource)}</span>
              </div>

              <div class="project-draft-resource-paths">
                <div>
                  <span>Root</span>
                  <code title={resource.source_path || publishTargetLabel({ publish_target: { kind: 'unknown' } })}>
                    {resource.source_path || resource.repo || 'read-only source unavailable'}
                  </code>
                </div>
                <div>
                  <span>Draft</span>
                  <code title={resource.workspace_path || resource.resource_path || ''}>
                    {resource.workspace_path || resource.resource_path || 'thread overlay unavailable'}
                  </code>
                </div>
              </div>

              <div class="project-draft-resource-counts">
                {#each CHANGE_METRICS as metric (metric.key)}
                  <span data-tone={metric.tone}>{metric.label} {countResourceChange(resource, metric.key)}</span>
                {/each}
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>

    <div class="project-draft-section">
      <div class="project-draft-section-head">
        <h4>Publish plan</h4>
        <span>{publishPlan.planOnly && !publishPlan.mutatesProjectRoot ? 'Preview' : 'Mutation'}</span>
      </div>
      <div class="project-draft-version-summary project-draft-publish-summary">
        <div>
          <strong>{publishPlan.resourceCount}</strong>
          <span>resource{publishPlan.resourceCount === 1 ? '' : 's'}</span>
        </div>
        <div>
          <strong>{publishPlan.operationCount}</strong>
          <span>operation{publishPlan.operationCount === 1 ? '' : 's'}</span>
        </div>
        <div data-tone={publishPlan.blockedCount > 0 ? 'conflicted' : 'changed'}>
          <strong>{publishPlan.blockedCount}</strong>
          <span>blocked</span>
        </div>
        <div>
          <strong>{publishPlan.readyCount}</strong>
          <span>ready</span>
        </div>
      </div>
      {#if publishPlan.groups.length === 0}
        <div class="project-draft-empty">No publish plan is available for this run.</div>
      {:else}
        <div class="project-draft-publish-groups">
          {#each publishPlan.groups as group (group.resource_id ?? group.mount_path ?? group.label)}
            {@const operations = Array.isArray(group.operations) ? group.operations : []}
            <div class="project-draft-publish-group" data-status={publishStatus(group)}>
              <div class="project-draft-publish-head">
                <div>
                  <strong>{publishGroupTitle(group)}</strong>
                  <span>{publishTargetLabel(group)}</span>
                </div>
                <span class="project-draft-resource-status">{publishStatus(group)}</span>
              </div>
              {#if group.blocked_reasons?.length}
                <div class="project-draft-blockers">
                  {#each group.blocked_reasons as reason (reason)}
                    <span>{cleanLabel(reason)}</span>
                  {/each}
                </div>
              {/if}
              {#if operations.length > 0}
                <div class="project-draft-operations">
                  {#each operations.slice(0, 6) as operation (`${operation.operation}:${operation.path}:${operation.target_path}`)}
                    <span data-operation={publishOperationLabel(operation)}>
                      <strong>{publishOperationLabel(operation)}</strong>
                      <code title={publishOperationPath(operation)}>{publishOperationPath(operation)}</code>
                    </span>
                  {/each}
                  {#if operations.length > 6}
                    <span class="project-draft-more">+{operations.length - 6} more</span>
                  {/if}
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>

    <div class="project-draft-section">
      <div class="project-draft-section-head">
        <h4>Root versions</h4>
        <span>{rootVersions.versionCount}</span>
      </div>
      <div class="project-draft-version-summary">
        <div>
          <strong>{rootVersions.resourceCount}</strong>
          <span>resource{rootVersions.resourceCount === 1 ? '' : 's'}</span>
        </div>
        <div>
          <strong>{rootVersions.versionCount}</strong>
          <span>version{rootVersions.versionCount === 1 ? '' : 's'}</span>
        </div>
      </div>
      <div class="project-draft-version-latest">
        {versionMeta(rootVersions.latestVersion)}
      </div>

      {#if rootVersions.groups.length > 0}
        <div class="project-draft-version-groups">
          {#each rootVersions.groups as group (group.resource_id ?? group.mount_path ?? group.label)}
            {@const versions = Array.isArray(group.versions) ? group.versions : []}
            {@const latest = latestGroupVersion(group)}
            <div class="project-draft-version-group">
              <div>
                <strong>{group.mount_path || group.label || group.resource_id || 'Project root'}</strong>
                <span>{versionMeta(latest)}</span>
              </div>
              <span>{versions.length}</span>
            </div>
            {#if versions.length > 0}
              <div class="project-draft-version-list">
                {#each versions.slice(0, 3) as version (version.version_id ?? version.id ?? version.label)}
                  <div class="project-draft-version-row">
                    <div>
                      <strong>{versionTitle(version)}</strong>
                      <span>{[version.created_at ? relativeTimeAgo(version.created_at) : '', fileCountLabel(version), formatBytes(version.total_size)].filter(Boolean).join(' / ')}</span>
                    </div>
                    <button type="button" class="project-draft-restore" disabled title={restoreTitle(version)} aria-label={restoreTitle(version)}>
                      <ConstellationIcon name="cycles" size={13} />
                    </button>
                  </div>
                {/each}
              </div>
            {/if}
          {/each}
        </div>
      {:else}
        <div class="project-draft-empty">No restorable root versions found.</div>
      {/if}
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
  .project-draft-alert {
    border: 1px solid rgba(255, 255, 255, 0.055);
    border-radius: 8px;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.018));
  }

  :global(:root[data-color-scheme='light']) .project-draft-summary,
  :global(:root[data-color-scheme='light']) .project-draft-alert {
    border-color: rgba(126, 92, 52, 0.09);
    background: rgba(255, 253, 247, 0.7);
  }

  .project-draft-summary {
    padding: 12px;
  }

  .project-draft-kicker,
  .project-draft-section-head h4,
  .project-draft-path-label span {
    margin: 0;
    color: rgba(240, 240, 250, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  :global(:root[data-color-scheme='light']) .project-draft-kicker,
  :global(:root[data-color-scheme='light']) .project-draft-section-head h4,
  :global(:root[data-color-scheme='light']) .project-draft-path-label span {
    color: rgba(82, 98, 111, 0.66);
  }

  .project-draft-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
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
  .project-draft-resource-status {
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
    color: rgba(231, 238, 247, 0.5);
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

  .project-draft-layers {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 20px minmax(0, 1fr);
    align-items: stretch;
    gap: 6px;
  }

  .project-draft-layer {
    display: grid;
    grid-template-columns: 28px minmax(0, 1fr);
    align-items: center;
    gap: 8px;
    min-width: 0;
    min-height: 52px;
    padding: 8px;
    border: 1px solid rgba(255, 255, 255, 0.055);
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.028);
  }

  :global(:root[data-color-scheme='light']) .project-draft-layer {
    border-color: rgba(126, 92, 52, 0.08);
    background: rgba(248, 250, 248, 0.72);
  }

  .project-draft-layer-icon {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.05);
    color: rgba(231, 238, 247, 0.72);
  }

  .project-draft-layer[data-layer='root'] .project-draft-layer-icon {
    color: #9cb7e4;
  }

  .project-draft-layer[data-layer='draft'] .project-draft-layer-icon {
    color: color-mix(in srgb, var(--thread-accent, #57CFA0) 86%, white);
  }

  .project-draft-layer strong,
  .project-draft-layer span {
    display: block;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .project-draft-layer strong {
    color: rgba(243, 247, 255, 0.9);
    font-size: 12px;
    font-weight: 650;
  }

  .project-draft-layer span {
    margin-top: 2px;
    color: rgba(231, 238, 247, 0.48);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
  }

  .project-draft-layer-arrow {
    display: grid;
    place-items: center;
    color: rgba(231, 238, 247, 0.42);
  }

  .project-draft-counts,
  .project-draft-version-summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 6px;
  }

  .project-draft-count,
  .project-draft-version-summary > div {
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
  :global(:root[data-color-scheme='light']) .project-draft-version-summary > div {
    border-color: rgba(126, 92, 52, 0.08);
    background: rgba(248, 250, 248, 0.74);
  }

  .project-draft-count span,
  .project-draft-version-summary span {
    color: rgba(231, 238, 247, 0.48);
    font-size: 9px;
    line-height: 1.15;
  }

  .project-draft-count strong,
  .project-draft-version-summary strong {
    color: rgba(243, 247, 255, 0.92);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 16px;
    line-height: 1;
  }

  :global(:root[data-color-scheme='light']) .project-draft-count span,
  :global(:root[data-color-scheme='light']) .project-draft-version-summary span {
    color: rgba(82, 98, 111, 0.64);
  }

  :global(:root[data-color-scheme='light']) .project-draft-count strong,
  :global(:root[data-color-scheme='light']) .project-draft-version-summary strong {
    color: rgba(20, 29, 38, 0.9);
  }

  .project-draft-count[data-tone='conflicted'] strong {
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

  .project-draft-alert[data-tone='conflict'] {
    border-color: rgba(212, 128, 143, 0.22);
  }

  .project-draft-section {
    display: grid;
    gap: 9px;
    padding-top: 12px;
    border-top: 1px solid rgba(255, 255, 255, 0.055);
  }

  :global(:root[data-color-scheme='light']) .project-draft-section {
    border-top-color: rgba(126, 92, 52, 0.09);
  }

  .project-draft-section-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }

  .project-draft-section-head > span {
    color: rgba(231, 238, 247, 0.48);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
  }

  .project-draft-resource-list,
  .project-draft-file-list,
  .project-draft-publish-groups,
  .project-draft-version-groups,
  .project-draft-version-list {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .project-draft-resource {
    display: grid;
    gap: 9px;
    min-width: 0;
    padding: 10px 0;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
  }

  .project-draft-resource[data-status*='conflict'] {
    border-top-color: rgba(212, 128, 143, 0.22);
  }

  .project-draft-resource-head,
  .project-draft-publish-head,
  .project-draft-version-group,
  .project-draft-version-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
  }

  .project-draft-resource-copy,
  .project-draft-publish-head > div,
  .project-draft-version-group > div,
  .project-draft-version-row > div {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .project-draft-resource-copy h5,
  .project-draft-publish-head strong,
  .project-draft-version-group strong,
  .project-draft-version-row strong {
    margin: 0;
    overflow-wrap: anywhere;
    color: rgba(243, 247, 255, 0.9);
    font-size: 12px;
    font-weight: 650;
    line-height: 1.3;
  }

  .project-draft-resource-copy p,
  .project-draft-publish-head span,
  .project-draft-version-group span,
  .project-draft-version-row span,
  .project-draft-version-latest {
    margin: 0;
    overflow-wrap: anywhere;
    color: rgba(231, 238, 247, 0.48);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.4;
  }

  .project-draft-resource-paths {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
    min-width: 0;
  }

  .project-draft-resource-paths div {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .project-draft-resource-paths span {
    color: rgba(231, 238, 247, 0.42);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    text-transform: uppercase;
  }

  .project-draft-resource-paths code {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    border-radius: 6px;
    padding: 5px 6px;
    background: rgba(255, 255, 255, 0.035);
    color: rgba(239, 244, 251, 0.7);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.25;
    white-space: nowrap;
  }

  .project-draft-resource-counts {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .project-draft-resource-counts span {
    border-radius: 7px;
    padding: 3px 7px;
    background: rgba(255, 255, 255, 0.045);
    color: rgba(231, 238, 247, 0.55);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.25;
  }

  .project-draft-path-group {
    display: grid;
    gap: 6px;
    min-width: 0;
    padding: 9px 0;
    border-top: 1px solid rgba(255, 255, 255, 0.045);
  }

  .project-draft-path-label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .project-draft-path-label strong {
    color: rgba(231, 238, 247, 0.5);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
  }

  .project-draft-paths {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    min-width: 0;
  }

  .project-draft-paths code,
  .project-draft-more,
  .project-draft-blockers span,
  .project-draft-operations span {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    border-radius: 6px;
    padding: 4px 6px;
    background: rgba(255, 255, 255, 0.045);
    color: rgba(239, 244, 251, 0.78);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
    line-height: 1.25;
    white-space: nowrap;
  }

  .project-draft-path-group[data-tone='warning'] .project-draft-paths code {
    background: rgba(236, 180, 95, 0.12);
    color: #e7bc77;
  }

  .project-draft-path-group[data-tone='conflicted'] .project-draft-paths code {
    background: rgba(212, 128, 143, 0.12);
    color: #efa5b0;
  }

  .project-draft-version-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .project-draft-publish-summary {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .project-draft-publish-summary [data-tone='conflicted'] strong {
    color: #efa5b0;
  }

  .project-draft-publish-group {
    display: grid;
    gap: 8px;
    min-width: 0;
    padding: 10px 0;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
  }

  .project-draft-publish-group[data-status='blocked'] {
    border-top-color: rgba(212, 128, 143, 0.22);
  }

  .project-draft-blockers,
  .project-draft-operations {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    min-width: 0;
  }

  .project-draft-blockers span {
    background: rgba(212, 128, 143, 0.12);
    color: #efa5b0;
  }

  .project-draft-operations span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }

  .project-draft-operations strong {
    color: rgba(231, 238, 247, 0.62);
    font-weight: 650;
  }

  .project-draft-operations code {
    overflow: hidden;
    text-overflow: ellipsis;
    color: rgba(239, 244, 251, 0.78);
  }

  .project-draft-version-latest {
    margin: 0;
  }

  .project-draft-version-groups {
    margin-top: 2px;
  }

  .project-draft-version-group {
    padding: 9px 0 4px;
    border-top: 1px solid rgba(255, 255, 255, 0.05);
  }

  .project-draft-version-group > span {
    flex: 0 0 auto;
    color: rgba(231, 238, 247, 0.5);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 10px;
  }

  .project-draft-version-row {
    min-height: 34px;
    padding-left: 8px;
    border-left: 1px solid rgba(255, 255, 255, 0.06);
  }

  .project-draft-restore {
    display: inline-grid;
    flex: 0 0 auto;
    place-items: center;
    width: 26px;
    height: 26px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 7px;
    background: rgba(255, 255, 255, 0.035);
    color: rgba(231, 238, 247, 0.42);
  }

  .project-draft-restore:disabled {
    cursor: not-allowed;
    opacity: 0.72;
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
    .project-draft-counts,
    .project-draft-publish-summary,
    .project-draft-layers,
    .project-draft-resource-paths {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .project-draft-layer-arrow {
      display: none;
    }
  }
</style>
