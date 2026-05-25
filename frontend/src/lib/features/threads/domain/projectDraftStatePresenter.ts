import type {
  ProjectDraftChangeKey,
  ProjectDraftChangeSet,
  ProjectDraftFileEntry,
  ProjectDraftPublishGroup,
  ProjectDraftPublishOperation,
  ProjectDraftResourceState,
  ProjectDraftRootVersionGroup,
  ProjectDraftStateRead,
  ProjectDraftStateResponse,
  ProjectRootVersionState,
} from '$lib/api/client';

export type DraftChangeMetric = {
  key: ProjectDraftChangeKey;
  label: string;
  tone: 'changed' | 'new' | 'deleted' | 'conflicted';
};

export type NormalizedRootVersions = {
  groups: ProjectDraftRootVersionGroup[];
  versionCount: number;
  resourceCount: number;
  latestVersion: ProjectRootVersionState | null;
};

export type DraftFileGroup = {
  key: ProjectDraftChangeKey | 'out_of_date_paths';
  label: string;
  tone: DraftChangeMetric['tone'] | 'warning';
  paths: string[];
};

export type ProjectExplorerFile = ProjectDraftFileEntry & {
  kind: 'file';
  key: string;
  resourceId: string;
  resourceTitle: string;
  mountPath: string;
  displayPath: string;
  depth: number;
};

export type ProjectExplorerDirectory = {
  kind: 'directory';
  key: string;
  resourceId: string;
  resourceTitle: string;
  mountPath: string;
  path: string;
  name: string;
  displayPath: string;
  depth: number;
  status: string;
  fileCount: number;
};

export type ProjectExplorerRow = ProjectExplorerDirectory | ProjectExplorerFile;

export type ProjectFileBrowserView = {
  files: ProjectExplorerFile[];
  rows: ProjectExplorerRow[];
  fileCount: number;
  changedCount: number;
  visibleCount: number;
  truncatedCount: number;
};

export type PublishPlanSummary = {
  ok: boolean;
  planOnly: boolean;
  mutatesProjectRoot: boolean;
  resourceCount: number;
  operationCount: number;
  blockedCount: number;
  readyCount: number;
  groups: ProjectDraftPublishGroup[];
};

export type ReadinessTone = 'clean' | 'modified' | 'warning' | 'conflict';

export type ProjectDraftPanelView = {
  statePayload: ProjectDraftStateRead | null;
  resources: ProjectDraftResourceState[];
  aggregateCounts: Record<ProjectDraftChangeKey, number>;
  totalChangeCount: number;
  outOfDatePaths: string[];
  fileGroups: DraftFileGroup[];
  fileBrowser: ProjectFileBrowserView;
  publishPlan: PublishPlanSummary;
  rootVersions: NormalizedRootVersions;
  effectiveRunId: string | number | null | undefined;
  runLabel: string;
  readiness: { tone: ReadinessTone; label: string; detail: string };
};

export const PROJECT_DRAFT_CHANGE_METRICS: DraftChangeMetric[] = [
  { key: 'changed_paths', label: 'Changed', tone: 'changed' },
  { key: 'new_paths', label: 'New', tone: 'new' },
  { key: 'deleted_paths', label: 'Deleted', tone: 'deleted' },
  { key: 'conflicted_paths', label: 'Conflicted', tone: 'conflicted' },
];

type ProjectDraftStateLike = ProjectDraftStateResponse | ProjectDraftStateRead | null;

export function buildProjectDraftPanelView({
  draftState,
  loading,
  loadError,
  runId,
}: {
  draftState: ProjectDraftStateLike;
  loading: boolean;
  loadError: string;
  runId?: string | number | null;
}): ProjectDraftPanelView {
  const statePayload = projectDraftStatePayload(draftState);
  const resources = normalizeResources(statePayload);
  const aggregateCounts = countAggregateChanges(statePayload, resources);
  const totalChangeCount = PROJECT_DRAFT_CHANGE_METRICS.reduce(
    (sum, metric) => sum + aggregateCounts[metric.key],
    0,
  );
  const outOfDatePaths = collectOutOfDatePaths(statePayload, resources);
  const fileGroups = collectFileGroups(statePayload, resources, outOfDatePaths);
  const fileBrowser = buildProjectFileBrowserView(statePayload, resources);
  const publishPlan = summarizePublishPlan(draftState, resources);
  const rootVersions = summarizeRootVersions(draftState, resources);
  const effectiveRunId = statePayload?.run_id ?? draftState?.run_id ?? runId;

  return {
    statePayload,
    resources,
    aggregateCounts,
    totalChangeCount,
    outOfDatePaths,
    fileGroups,
    fileBrowser,
    publishPlan,
    rootVersions,
    effectiveRunId,
    runLabel: effectiveRunId ? `Run ${effectiveRunId}` : 'Latest run',
    readiness: summarizeReadiness({
      loading,
      loadError,
      statePayload,
      publishPlan,
      aggregateCounts,
      outOfDatePaths,
      totalChangeCount,
    }),
  };
}

function summarizeReadiness({
  loading,
  loadError,
  statePayload,
  publishPlan,
  aggregateCounts,
  outOfDatePaths,
  totalChangeCount,
}: {
  loading: boolean;
  loadError: string;
  statePayload: ProjectDraftStateRead | null;
  publishPlan: PublishPlanSummary;
  aggregateCounts: Record<ProjectDraftChangeKey, number>;
  outOfDatePaths: string[];
  totalChangeCount: number;
}): { tone: ReadinessTone; label: string; detail: string } {
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

function projectDraftStatePayload(payload: ProjectDraftStateLike): ProjectDraftStateRead | null {
  const nested = (payload as any)?.draft_status ?? (payload as any)?.draft_state;
  return (nested && typeof nested === 'object' ? nested : payload) as ProjectDraftStateRead | null;
}

function emptyChangeSet(): ProjectDraftChangeSet {
  return {
    changed_paths: [],
    new_paths: [],
    deleted_paths: [],
    conflicted_paths: [],
  };
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function pathList(value: unknown): string[] {
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

export function normalizeResources(payload: ProjectDraftStateRead | null): ProjectDraftResourceState[] {
  const raw = (payload as any)?.resources;
  return Array.isArray(raw) ? raw : [];
}

export function resourceChanges(resource: ProjectDraftResourceState): ProjectDraftChangeSet {
  const changes = emptyChangeSet();
  const rawChanges = (resource.changes ?? {}) as Record<string, unknown>;
  for (const metric of PROJECT_DRAFT_CHANGE_METRICS) {
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

export function countResourceChange(
  resource: ProjectDraftResourceState,
  key: ProjectDraftChangeKey,
): number {
  const explicit = resource.change_counts?.[key];
  if (typeof explicit === 'number' && Number.isFinite(explicit)) {
    return explicit;
  }
  return resourceChanges(resource)[key].length;
}

export function countAggregateChanges(
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
  for (const metric of PROJECT_DRAFT_CHANGE_METRICS) {
    const value = payloadCounts?.[metric.key];
    counts[metric.key] = typeof value === 'number' && Number.isFinite(value)
      ? value
      : resourceList.reduce((sum, resource) => sum + countResourceChange(resource, metric.key), 0);
  }
  return counts;
}

export function collectOutOfDatePaths(
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

export function prefixedResourcePaths(
  resource: ProjectDraftResourceState,
  key: ProjectDraftChangeKey,
): string[] {
  const prefix = cleanLabel(resource.mount_path || resource.label || resource.id);
  return resourceChanges(resource)[key].map((path) => prefix ? `${prefix}/${path}` : path);
}

export function collectFileGroups(
  payload: ProjectDraftStateRead | null,
  resourceList: ProjectDraftResourceState[],
  stalePaths: string[],
): DraftFileGroup[] {
  const groups: DraftFileGroup[] = PROJECT_DRAFT_CHANGE_METRICS.map((metric) => {
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

function browserEntriesFromPayload(
  payload: ProjectDraftStateRead | null,
  resourceList: ProjectDraftResourceState[],
): ProjectDraftFileEntry[] {
  const topLevelEntries = (payload as any)?.file_browser?.entries;
  if (Array.isArray(topLevelEntries) && topLevelEntries.length > 0) {
    return topLevelEntries.filter((entry) => entry && typeof entry === 'object') as ProjectDraftFileEntry[];
  }
  return resourceList.flatMap((resource) => {
    const entries = (resource as any)?.file_browser?.entries;
    if (!Array.isArray(entries)) return [];
    return entries
      .filter((entry) => entry && typeof entry === 'object')
      .map((entry) => ({
        ...(entry as ProjectDraftFileEntry),
        resource_id: resource.id,
        mount_path: resource.mount_path,
        resource_label: resource.label,
      }));
  });
}

export function buildProjectFileBrowserView(
  payload: ProjectDraftStateRead | null,
  resourceList: ProjectDraftResourceState[],
): ProjectFileBrowserView {
  const resourcesById = new Map(resourceList.map((resource) => [resource.id, resource]));
  const files = browserEntriesFromPayload(payload, resourceList)
    .map((entry, index): ProjectExplorerFile | null => {
      const path = cleanProjectPath(entry.path);
      if (!path) return null;
      const resourceId = cleanLabel(entry.resource_id, 'project-root');
      const resource = resourcesById.get(resourceId);
      const mountPath = cleanProjectMount(entry.mount_path ?? resource?.mount_path ?? '/');
      const resourceName = cleanLabel(entry.resource_label ?? resource?.label ?? mountPath, 'Project root');
      return {
        ...entry,
        kind: 'file',
        key: `${resourceId}:${path}:${index}`,
        resourceId,
        resourceTitle: resourceName,
        mountPath,
        path,
        name: cleanLabel(entry.name, path.split('/').at(-1) ?? path),
        displayPath: joinProjectDisplayPath(mountPath, path),
        depth: Math.max(0, path.split('/').length - 1),
      };
    })
    .filter((entry): entry is ProjectExplorerFile => Boolean(entry))
    .sort((left, right) => left.displayPath.localeCompare(right.displayPath));
  const rows = buildProjectExplorerRows(files);
  const changedCount = files.filter((file) => projectFileStatusTone(file.status) !== 'clean').length;
  const summary = (payload as any)?.file_browser?.summary ?? {};
  const visibleCount = Number(summary.visible_count);
  const fileCount = Number(summary.file_count);
  const truncatedCount = Number(summary.truncated);
  return {
    files,
    rows,
    fileCount: Number.isFinite(fileCount) ? fileCount : files.length,
    changedCount,
    visibleCount: Number.isFinite(visibleCount) ? visibleCount : files.length,
    truncatedCount: Number.isFinite(truncatedCount) ? truncatedCount : 0,
  };
}

function buildProjectExplorerRows(files: ProjectExplorerFile[]): ProjectExplorerRow[] {
  const directories = new Map<string, ProjectExplorerDirectory>();
  for (const file of files) {
    const parts = cleanProjectPath(file.path).split('/').filter(Boolean);
    let current = '';
    for (const part of parts.slice(0, -1)) {
      current = current ? `${current}/${part}` : part;
      const key = `${file.resourceId}:dir:${current}`;
      const existing = directories.get(key);
      if (existing) {
        existing.fileCount += 1;
        existing.status = combineFileStatuses(existing.status, file.status);
        continue;
      }
      directories.set(key, {
        kind: 'directory',
        key,
        resourceId: file.resourceId,
        resourceTitle: file.resourceTitle,
        mountPath: file.mountPath,
        path: current,
        name: part,
        displayPath: joinProjectDisplayPath(file.mountPath, current),
        depth: current.split('/').length - 1,
        status: cleanLabel(file.status, 'clean'),
        fileCount: 1,
      });
    }
  }
  return [...directories.values(), ...files]
    .sort((left, right) => {
      const byPath = left.displayPath.localeCompare(right.displayPath);
      if (byPath !== 0) return byPath;
      return left.kind === right.kind ? 0 : left.kind === 'directory' ? -1 : 1;
    });
}

function combineFileStatuses(left: unknown, right: unknown): string {
  const priority = ['conflicted', 'out_of_date', 'deleted', 'changed', 'new', 'clean'];
  const leftStatus = normaliseProjectFileStatus(left);
  const rightStatus = normaliseProjectFileStatus(right);
  const leftIndex = priority.indexOf(leftStatus);
  const rightIndex = priority.indexOf(rightStatus);
  if (leftIndex === -1) return rightStatus;
  if (rightIndex === -1) return leftStatus;
  return leftIndex <= rightIndex ? leftStatus : rightStatus;
}

function normaliseProjectFileStatus(status: unknown): string {
  const value = String(status ?? 'clean').trim().toLowerCase().replaceAll(' ', '_').replaceAll('-', '_');
  return value || 'clean';
}

export function cleanProjectPath(path: unknown): string {
  return String(path ?? '').replaceAll('\\', '/').replace(/^\/+/, '').trim();
}

export function cleanProjectMount(path: unknown): string {
  const cleaned = String(path ?? '').replaceAll('\\', '/').trim();
  if (!cleaned || cleaned === '.') return '/';
  return cleaned.startsWith('/') ? cleaned : `/${cleaned}`;
}

export function joinProjectDisplayPath(mountPath: unknown, filePath: unknown): string {
  const mount = cleanProjectMount(mountPath);
  const path = cleanProjectPath(filePath);
  if (!path) return mount;
  if (mount === '/') return `/${path}`;
  return `${mount.replace(/\/+$/, '')}/${path}`;
}

export function projectFileStatusLabel(status: unknown): string {
  const value = normaliseProjectFileStatus(status);
  if (value === 'out_of_date') return 'out of date';
  return cleanLabel(value, 'clean');
}

export function projectFileStatusTone(status: unknown): 'clean' | 'changed' | 'new' | 'deleted' | 'conflicted' | 'warning' {
  const value = normaliseProjectFileStatus(status);
  if (value === 'conflicted') return 'conflicted';
  if (value === 'out_of_date') return 'warning';
  if (value === 'deleted') return 'deleted';
  if (value === 'new') return 'new';
  if (value === 'changed' || value === 'modified') return 'changed';
  return 'clean';
}

export function projectFileLayerLabel(file: ProjectDraftFileEntry | null | undefined): string {
  if (!file) return 'No file selected';
  if (file.has_draft) return file.has_root ? 'draft overlay' : 'new draft file';
  if (file.has_root) return 'project root';
  return cleanLabel(file.layer, 'unknown layer');
}

export function projectFileSizeLabel(value: unknown): string {
  const size = Number(value);
  if (!Number.isFinite(size) || size < 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function publishPlanPayload(payload: ProjectDraftStateLike): Record<string, any> | null {
  const plan = (payload as any)?.plan_publish;
  return plan && typeof plan === 'object' ? plan : null;
}

export function publishPlanGroups(
  payload: ProjectDraftStateLike,
  resourceList: ProjectDraftResourceState[],
): ProjectDraftPublishGroup[] {
  const planGroups = publishPlanPayload(payload)?.groups;
  if (Array.isArray(planGroups)) return planGroups;
  return resourceList.map((resource) => {
    const operations = PROJECT_DRAFT_CHANGE_METRICS.flatMap((metric) =>
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

export function summarizePublishPlan(
  payload: ProjectDraftStateLike,
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

function rootVersionGroupsFromPayload(payload: ProjectDraftStateLike): ProjectDraftRootVersionGroup[] {
  const rootVersions = (payload as any)?.root_versions;
  if (Array.isArray(rootVersions)) return rootVersions;
  if (Array.isArray(rootVersions?.groups)) return rootVersions.groups;
  if (Array.isArray((payload as any)?.root_version_groups)) return (payload as any).root_version_groups;
  return [];
}

function rootVersionGroupsFromResources(
  resourcesWithVersions: ProjectDraftResourceState[],
): ProjectDraftRootVersionGroup[] {
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

export function summarizeRootVersions(
  payload: ProjectDraftStateLike,
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

export function cleanLabel(value: unknown, fallback = ''): string {
  const text = String(value ?? '').replaceAll('_', ' ').trim();
  return text || fallback;
}

export function resourceTitle(resource: ProjectDraftResourceState): string {
  return cleanLabel(resource.mount_path || resource.label || resource.id, 'Project resource');
}

export function resourceMeta(resource: ProjectDraftResourceState): string {
  return [
    cleanLabel(resource.kind),
    cleanLabel(resource.provider),
    resource.repo ? String(resource.repo) : '',
    cleanLabel(resource.change_source),
  ].filter(Boolean).join(' / ');
}

export function resourceStatus(resource: ProjectDraftResourceState): string {
  const status = cleanLabel(resource.status);
  if (status) return status;
  const total = PROJECT_DRAFT_CHANGE_METRICS.reduce(
    (sum, metric) => sum + countResourceChange(resource, metric.key),
    0,
  );
  return total > 0 ? 'modified' : 'clean';
}

export function fileCountLabel(version: ProjectRootVersionState | null): string {
  const count = Number(version?.file_count);
  if (!Number.isFinite(count)) return '';
  return `${count} file${count === 1 ? '' : 's'}`;
}

export function versionTitle(version: ProjectRootVersionState): string {
  return cleanLabel(version.label || version.version_id || version.id, 'Root version');
}

export function restoreTitle(version: ProjectRootVersionState): string {
  return `Restore ${versionTitle(version)} unavailable in this client`;
}

export function latestGroupVersion(group: ProjectDraftRootVersionGroup): ProjectRootVersionState | null {
  const versions = Array.isArray(group.versions) ? group.versions : [];
  return versions
    .slice()
    .sort((left, right) =>
      new Date(right.created_at ?? 0).getTime() - new Date(left.created_at ?? 0).getTime(),
    )[0] ?? null;
}

export function publishGroupTitle(group: ProjectDraftPublishGroup): string {
  return cleanLabel(group.mount_path || group.label || group.resource_id, 'Project resource');
}

export function publishTargetLabel(group: ProjectDraftPublishGroup): string {
  const target = group.publish_target ?? {};
  if (target.kind === 'local_path' && target.path) return target.path;
  if (target.kind === 'git_repository' && target.repo) return target.repo;
  return cleanLabel(target.kind, 'Target unavailable');
}

export function publishStatus(group: ProjectDraftPublishGroup): string {
  return cleanLabel(group.status, 'clean');
}

export function publishOperationLabel(operation: ProjectDraftPublishOperation): string {
  return cleanLabel(operation.operation, 'change');
}

export function publishOperationPath(operation: ProjectDraftPublishOperation): string {
  return cleanLabel(operation.path || operation.target_path || operation.draft_path, 'Project path');
}
