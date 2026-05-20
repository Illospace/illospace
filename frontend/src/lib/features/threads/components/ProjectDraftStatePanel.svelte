<script lang="ts">
  import ConstellationIcon from '$lib/components/constellation/ConstellationIcon.svelte';
  import { getIdeaProjectDraftState } from '$lib/features/threads/api/threadApi';
  import { relativeTimeAgo } from '$lib/utils/datetime';
  import type {
    ProjectDraftChangeKey,
    ProjectDraftChangeSet,
    ProjectDraftPublishGroup,
    ProjectDraftPublishOperation,
    ProjectDraftResourceState,
    ProjectDraftRootVersionGroup,
    ProjectDraftStateResponse,
    ProjectDraftStateRead,
    ProjectRootVersionState,
  } from '$lib/api/client';

  type DraftIdea = {
    id?: string | null;
  } | null;

  type DraftChangeMetric = {
    key: ProjectDraftChangeKey;
    label: string;
    tone: 'changed' | 'new' | 'deleted' | 'conflicted';
  };

  type NormalizedRootVersions = {
    groups: ProjectDraftRootVersionGroup[];
    versionCount: number;
    resourceCount: number;
    latestVersion: ProjectRootVersionState | null;
  };

  type DraftFileGroup = {
    key: ProjectDraftChangeKey | 'out_of_date_paths';
    label: string;
    tone: DraftChangeMetric['tone'] | 'warning';
    paths: string[];
  };

  type PublishPlanSummary = {
    ok: boolean;
    planOnly: boolean;
    mutatesProjectRoot: boolean;
    resourceCount: number;
    operationCount: number;
    blockedCount: number;
    readyCount: number;
    groups: ProjectDraftPublishGroup[];
  };

  type ReadinessTone = 'clean' | 'modified' | 'warning' | 'conflict';

  const CHANGE_METRICS: DraftChangeMetric[] = [
    { key: 'changed_paths', label: 'Changed', tone: 'changed' },
    { key: 'new_paths', label: 'New', tone: 'new' },
    { key: 'deleted_paths', label: 'Deleted', tone: 'deleted' },
    { key: 'conflicted_paths', label: 'Conflicted', tone: 'conflicted' },
  ];

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

  const statePayload = $derived.by(() => {
    const nested = (draftState as any)?.draft_status ?? (draftState as any)?.draft_state;
    return (nested && typeof nested === 'object' ? nested : draftState) as ProjectDraftStateRead | null;
  });
  const resources = $derived.by(() => normalizeResources(statePayload));
  const aggregateCounts = $derived.by(() => countAggregateChanges(statePayload, resources));
  const totalChangeCount = $derived(
    CHANGE_METRICS.reduce((sum, metric) => sum + aggregateCounts[metric.key], 0),
  );
  const outOfDatePaths = $derived.by(() => collectOutOfDatePaths(statePayload, resources));
  const fileGroups = $derived.by(() => collectFileGroups(statePayload, resources, outOfDatePaths));
  const publishPlan = $derived.by(() => summarizePublishPlan(draftState, resources));
  const rootVersions = $derived.by(() => summarizeRootVersions(draftState, resources));
  const effectiveRunId = $derived(statePayload?.run_id ?? draftState?.run_id ?? runId);
  const runLabel = $derived(effectiveRunId ? `Run ${effectiveRunId}` : 'Latest run');
  const readiness = $derived.by(() => summarizeReadiness());
  const signalTone = $derived(readiness.tone);
  const signalLabel = $derived(readiness.label);

  function summarizeReadiness(): { tone: ReadinessTone; label: string; detail: string } {
    if (loading) return { tone: 'warning', label: 'Loading', detail: 'Loading Project draft state.' };
    if (loadError) return { tone: 'warning', label: 'Unavailable', detail: 'Draft state could not be loaded.' };
    if (statePayload?.ok === false) return { tone: 'warning', label: 'Not bound', detail: 'No Project draft state is bound to this run.' };
    if (!publishPlan.ok) return { tone: 'warning', label: 'Plan unavailable', detail: 'Publish preview could not be loaded.' };
    if (aggregateCounts.conflicted_paths > 0 || publishPlan.blockedCount > 0) {
      return { tone: 'conflict', label: 'Blocked', detail: 'Resolve conflicts before publish.' };
    }
    if (outOfDatePaths.length > 0) {
      return { tone: 'warning', label: 'Needs refresh', detail: 'Root changed after this thread draft was made.' };
    }
    if (publishPlan.operationCount > 0) {
      return { tone: 'modified', label: 'Ready', detail: 'Plan-only publish preview is ready.' };
    }
    if (totalChangeCount > 0) {
      return { tone: 'modified', label: 'Draft changes', detail: 'Thread overlay has local changes.' };
    }
    return { tone: 'clean', label: 'Clean', detail: 'Thread overlay matches Project root.' };
  }

  function emptyChangeSet(): ProjectDraftChangeSet {
    return {
      changed_paths: [],
      new_paths: [],
      deleted_paths: [],
      conflicted_paths: [],
    };
  }

  function asArray(value: unknown): unknown[] {
    return Array.isArray(value) ? value : [];
  }

  function pathList(value: unknown): string[] {
    if (typeof value === 'string' && value.trim()) {
      return [value.trim()];
    }
    return asArray(value)
      .map((item) => {
        if (typeof item === 'string') return item.trim();
        if (!item || typeof item !== 'object') return '';
        const record = item as Record<string, unknown>;
        return String(record.path ?? record.relative_path ?? record.name ?? '').trim();
      })
      .filter(Boolean);
  }

  function normalizeResources(payload: ProjectDraftStateRead | null): ProjectDraftResourceState[] {
    const raw = (payload as any)?.resources;
    return Array.isArray(raw) ? raw : [];
  }

  function resourceChanges(resource: ProjectDraftResourceState): ProjectDraftChangeSet {
    const changes = emptyChangeSet();
    const rawChanges = (resource.changes ?? {}) as Record<string, unknown>;
    for (const metric of CHANGE_METRICS) {
      changes[metric.key] = pathList(rawChanges[metric.key]);
    }
    changes.out_of_date_paths = pathList(
      rawChanges.out_of_date_paths
        ?? rawChanges.out_of_date
        ?? (resource as any).out_of_date_paths
        ?? (resource as any).out_of_date,
    );
    return changes;
  }

  function countResourceChange(resource: ProjectDraftResourceState, key: ProjectDraftChangeKey): number {
    const explicit = resource.change_counts?.[key];
    if (typeof explicit === 'number' && Number.isFinite(explicit)) {
      return explicit;
    }
    return resourceChanges(resource)[key].length;
  }

  function countAggregateChanges(
    payload: ProjectDraftStateRead | null,
    resourceList: ProjectDraftResourceState[],
  ): Record<ProjectDraftChangeKey, number> {
    const counts = {
      changed_paths: 0,
      new_paths: 0,
      deleted_paths: 0,
      conflicted_paths: 0,
    };
    const payloadCounts = payload?.changes?.counts;
    for (const metric of CHANGE_METRICS) {
      const value = payloadCounts?.[metric.key];
      counts[metric.key] = typeof value === 'number' && Number.isFinite(value)
        ? value
        : resourceList.reduce((sum, resource) => sum + countResourceChange(resource, metric.key), 0);
    }
    return counts;
  }

  function collectOutOfDatePaths(
    payload: ProjectDraftStateRead | null,
    resourceList: ProjectDraftResourceState[],
  ): string[] {
    const payloadOutOfDate = (payload as any)?.out_of_date;
    const paths = [
      ...pathList((payload?.changes as any)?.out_of_date_paths),
      ...pathList(payloadOutOfDate === true ? null : payloadOutOfDate),
    ];

    if (payloadOutOfDate === true) {
      paths.push('Project draft');
    }

    for (const resource of resourceList) {
      const resourcePaths = resourceChanges(resource).out_of_date_paths ?? [];
      if (resourcePaths.length > 0) {
        const prefix = resource.mount_path || resource.label || resource.id;
        paths.push(...resourcePaths.map((path) => prefix ? `${prefix}/${path}` : path));
        continue;
      }
      const status = resourceStatus(resource).toLowerCase();
      if ((resource as any).out_of_date === true || status.includes('out of date') || status.includes('out-of-date')) {
        paths.push(resourceTitle(resource));
      }
    }

    return Array.from(new Set(paths)).sort();
  }

  function prefixedResourcePaths(
    resource: ProjectDraftResourceState,
    key: ProjectDraftChangeKey,
  ): string[] {
    const prefix = cleanLabel(resource.mount_path || resource.label || resource.id);
    return resourceChanges(resource)[key].map((path) => prefix ? `${prefix}/${path}` : path);
  }

  function collectFileGroups(
    payload: ProjectDraftStateRead | null,
    resourceList: ProjectDraftResourceState[],
    stalePaths: string[],
  ): DraftFileGroup[] {
    const groups: DraftFileGroup[] = CHANGE_METRICS.map((metric) => {
      const payloadPaths = pathList((payload?.changes as any)?.[metric.key]);
      const resourcePaths = resourceList.flatMap((resource) => prefixedResourcePaths(resource, metric.key));
      const paths = resourcePaths.length > 0 ? resourcePaths : payloadPaths;
      return {
        key: metric.key,
        label: metric.label,
        tone: metric.tone,
        paths: Array.from(new Set(paths)).sort(),
      };
    });
    groups.push({
      key: 'out_of_date_paths',
      label: 'Out of date',
      tone: 'warning',
      paths: stalePaths,
    });
    return groups.filter((group) => group.paths.length > 0);
  }

  function publishPlanPayload(
    payload: ProjectDraftStateResponse | ProjectDraftStateRead | null,
  ): Record<string, any> | null {
    const plan = (payload as any)?.plan_publish;
    return plan && typeof plan === 'object' ? plan : null;
  }

  function publishPlanGroups(
    payload: ProjectDraftStateResponse | ProjectDraftStateRead | null,
    resourceList: ProjectDraftResourceState[],
  ): ProjectDraftPublishGroup[] {
    const planGroups = publishPlanPayload(payload)?.groups;
    if (Array.isArray(planGroups)) return planGroups;
    return resourceList.map((resource) => {
      const operations = CHANGE_METRICS.flatMap((metric) =>
        resourceChanges(resource)[metric.key].map((path) => ({
          operation: metric.key === 'new_paths'
            ? 'create'
            : metric.key === 'deleted_paths'
              ? 'delete'
              : metric.key === 'conflicted_paths'
                ? 'resolve_conflict'
                : 'update',
          path,
        })),
      );
      return {
        resource_id: resource.id,
        mount_path: resource.mount_path,
        label: resource.label,
        workspace_path: resource.workspace_path,
        publish_target: resource.source_path ? { kind: 'local_path', path: resource.source_path } : { kind: 'unknown' },
        status: operations.some((operation) => operation.operation === 'resolve_conflict')
          ? 'blocked'
          : operations.length > 0
            ? 'ready'
            : 'clean',
        blocked_reasons: operations.some((operation) => operation.operation === 'resolve_conflict')
          ? ['conflicted_paths_require_resolution']
          : [],
        change_counts: resource.change_counts,
        operations,
      };
    });
  }

  function summarizePublishPlan(
    payload: ProjectDraftStateResponse | ProjectDraftStateRead | null,
    resourceList: ProjectDraftResourceState[],
  ): PublishPlanSummary {
    const plan = publishPlanPayload(payload);
    const groups = publishPlanGroups(payload, resourceList);
    const summary = plan?.summary ?? {};
    const explicitOperationCount = Number(summary.operation_count);
    const explicitBlockedCount = Number(summary.blocked_count);
    const explicitResourceCount = Number(summary.resource_count);
    const operationCount = Number.isFinite(explicitOperationCount)
      ? explicitOperationCount
      : groups.reduce((sum, group) => sum + asArray(group.operations).length, 0);
    const blockedCount = Number.isFinite(explicitBlockedCount)
      ? explicitBlockedCount
      : groups.filter((group) => cleanLabel(group.status).toLowerCase() === 'blocked').length;

    return {
      ok: plan?.ok !== false,
      planOnly: plan?.plan_only !== false,
      mutatesProjectRoot: plan?.mutates_project_root === true,
      resourceCount: Number.isFinite(explicitResourceCount) ? explicitResourceCount : groups.length,
      operationCount,
      blockedCount,
      readyCount: groups.filter((group) => cleanLabel(group.status).toLowerCase() === 'ready').length,
      groups,
    };
  }

  function rootVersionGroupsFromPayload(
    payload: ProjectDraftStateResponse | ProjectDraftStateRead | null,
  ): ProjectDraftRootVersionGroup[] {
    const rootVersions = (payload as any)?.root_versions;
    if (Array.isArray(rootVersions)) return rootVersions;
    if (Array.isArray(rootVersions?.groups)) return rootVersions.groups;
    if (Array.isArray((payload as any)?.root_version_groups)) return (payload as any).root_version_groups;
    return [];
  }

  function rootVersionGroupsFromResources(resourcesWithVersions: ProjectDraftResourceState[]): ProjectDraftRootVersionGroup[] {
    return resourcesWithVersions.flatMap((resource) => {
      const raw = (resource as any).root_versions;
      const versions = Array.isArray(raw?.versions)
        ? raw.versions
        : Array.isArray(raw)
          ? raw
          : [];
      if (versions.length === 0) return [];
      return [{
        resource_id: resource.id,
        mount_path: resource.mount_path,
        label: resource.label,
        source_path: resource.source_path,
        workspace_path: resource.workspace_path,
        versions,
      }];
    });
  }

  function summarizeRootVersions(
    payload: ProjectDraftStateResponse | ProjectDraftStateRead | null,
    resourceList: ProjectDraftResourceState[],
  ): NormalizedRootVersions {
    const groups = [
      ...rootVersionGroupsFromPayload(payload),
      ...rootVersionGroupsFromResources(resourceList),
    ];
    const versions = groups.flatMap((group) => Array.isArray(group.versions) ? group.versions : []);
    const latestVersion = versions
      .slice()
      .sort((left, right) =>
        new Date(right.created_at ?? 0).getTime() - new Date(left.created_at ?? 0).getTime(),
      )[0] ?? null;
    const summary = (payload as any)?.root_versions?.summary ?? (payload as any)?.root_versions_summary;
    const summaryVersionCount = Number(summary?.version_count);
    const summaryResourceCount = Number(summary?.resource_count);

    return {
      groups,
      versionCount: Number.isFinite(summaryVersionCount) ? summaryVersionCount : versions.length,
      resourceCount: Number.isFinite(summaryResourceCount) ? summaryResourceCount : groups.length,
      latestVersion,
    };
  }

  function cleanLabel(value: unknown, fallback = ''): string {
    const text = String(value ?? '').replaceAll('_', ' ').trim();
    return text || fallback;
  }

  function resourceTitle(resource: ProjectDraftResourceState): string {
    return cleanLabel(resource.mount_path || resource.label || resource.id, 'Project resource');
  }

  function resourceMeta(resource: ProjectDraftResourceState): string {
    return [
      cleanLabel(resource.kind),
      cleanLabel(resource.provider),
      resource.repo ? String(resource.repo) : '',
      cleanLabel(resource.change_source),
    ].filter(Boolean).join(' / ');
  }

  function resourceStatus(resource: ProjectDraftResourceState): string {
    const status = cleanLabel(resource.status);
    if (status) return status;
    const total = CHANGE_METRICS.reduce((sum, metric) => sum + countResourceChange(resource, metric.key), 0);
    return total > 0 ? 'modified' : 'clean';
  }

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

  function fileCountLabel(version: ProjectRootVersionState | null): string {
    const count = Number(version?.file_count);
    if (!Number.isFinite(count)) return '';
    return `${count} file${count === 1 ? '' : 's'}`;
  }

  function versionTitle(version: ProjectRootVersionState): string {
    return cleanLabel(version.label || version.version_id || version.id, 'Root version');
  }

  function restoreTitle(version: ProjectRootVersionState): string {
    return `Restore ${versionTitle(version)} unavailable in this client`;
  }

  function latestGroupVersion(group: ProjectDraftRootVersionGroup): ProjectRootVersionState | null {
    const versions = Array.isArray(group.versions) ? group.versions : [];
    return versions
      .slice()
      .sort((left, right) =>
        new Date(right.created_at ?? 0).getTime() - new Date(left.created_at ?? 0).getTime(),
      )[0] ?? null;
  }

  function publishGroupTitle(group: ProjectDraftPublishGroup): string {
    return cleanLabel(group.mount_path || group.label || group.resource_id, 'Project resource');
  }

  function publishTargetLabel(group: ProjectDraftPublishGroup): string {
    const target = group.publish_target ?? {};
    if (target.kind === 'local_path' && target.path) return target.path;
    if (target.kind === 'git_repository' && target.repo) return target.repo;
    return cleanLabel(target.kind, 'Target unavailable');
  }

  function publishStatus(group: ProjectDraftPublishGroup): string {
    return cleanLabel(group.status, 'clean');
  }

  function publishOperationLabel(operation: ProjectDraftPublishOperation): string {
    return cleanLabel(operation.operation, 'change');
  }

  function publishOperationPath(operation: ProjectDraftPublishOperation): string {
    return cleanLabel(operation.path || operation.target_path || operation.draft_path, 'Project path');
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
