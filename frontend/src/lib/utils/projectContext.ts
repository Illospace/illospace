export type ProjectContextResource = {
  type?: string;
  kind?: string;
  label?: string;
  id?: string;
  path?: string;
  uri?: string;
  repo?: string;
  name?: string;
  access?: 'read' | 'write' | string;
  branch?: string;
  source?: string;
  file_manifest?: string[];
  file_count?: number;
  uploaded_files?: Array<Record<string, any>>;
  uploaded_file_count?: number;
  allowed_paths?: string[];
  files?: string[];
  folders?: string[];
  forbidden_paths?: string[];
  denied_paths?: string[];
  permissions?: Record<string, any>;
  credential_ref?: {
    type?: string;
    provider?: string;
    key_name?: string;
  };
  size?: number;
  mime?: string;
  last_modified?: number;
};

export type ProjectContextSnapshotLike = {
  id?: string;
  name?: string;
  description?: string;
  validation_status?: string;
  status?: string;
  resources?: ProjectContextResource[];
  targets?: ProjectContextResource[];
  project_profile_id?: string;
  selected_profile_id?: string;
  selected_profile_name?: string;
  profile_name?: string;
};

export type ProjectContextPickerState = {
  snapshot: ProjectContextSnapshotLike | null;
  valid: boolean;
  error: string | null;
  resourceCount: number;
};

export function countProjectContextResources(context: ProjectContextSnapshotLike | null | undefined): number {
  const resources = context?.resources ?? context?.targets ?? [];
  return Array.isArray(resources) ? resources.length : 0;
}

export function projectContextStatusCopy(context: ProjectContextSnapshotLike | null | undefined): string {
  const status = context?.validation_status ?? context?.status;
  if (!context) return 'No project context attached';
  if (status === 'client_invalid' || status === 'invalid') return 'Needs attention';
  if (status === 'valid' || status === 'validated' || status === 'client_validated') return 'Ready for workers';
  return 'Attached';
}

export function projectContextDisplayName(context: ProjectContextSnapshotLike | null | undefined): string {
  return context?.selected_profile_name?.trim()
    || context?.profile_name?.trim()
    || 'Project Context';
}

export function buildProjectContextMessageAttachment(context: ProjectContextSnapshotLike) {
  return {
    type: 'project_context',
    name: projectContextDisplayName(context),
    project_context: context,
  };
}

export function buildProjectContextAttachPayload(context: ProjectContextSnapshotLike) {
  return context.project_profile_id
    ? { project_profile_id: context.project_profile_id }
    : { project_context: context };
}

export function extractIdeaProjectContext(currentIdea: any): ProjectContextSnapshotLike | null {
  if (!currentIdea) return null;
  if (currentIdea.project_context) return currentIdea.project_context;
  const details = currentIdea.agent_details ?? currentIdea.metadata ?? {};
  return details.project_context ?? details.project_context_snapshot ?? null;
}

export function projectContextErrorMessage(err: any, fallback = 'Could not attach project context to this thought.'): string {
  const detail = err?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail?.validation_errors) && detail.validation_errors.length) {
    return String(detail.validation_errors[0]);
  }
  return fallback;
}

export function inferProjectContextResource(value: string): ProjectContextResource {
  const trimmed = value.trim();
  const type = trimmed.startsWith('http') || trimmed.endsWith('.md') ? 'doc'
    : trimmed.includes('.') && !trimmed.includes('/') ? 'repo'
    : trimmed.includes('.') ? 'file'
    : 'folder';
  return normalizeProjectContextResource({
    type,
    label: trimmed,
    path: type === 'repo' ? undefined : trimmed,
    repo: type === 'repo' ? trimmed : undefined,
  });
}

export function normalizeProjectContextResource(
  resource: ProjectContextResource,
  index = 0,
): ProjectContextResource {
  const kind = resource.kind ?? resource.type ?? 'resource';
  const label = resource.label ?? resource.name ?? resource.repo ?? resource.path ?? resource.uri ?? `Resource ${index + 1}`;
  return {
    ...resource,
    id: resource.id ?? `resource-${index + 1}`,
    type: resource.type ?? kind,
    kind,
    label,
    name: resource.name ?? label,
  };
}

export function normalizeProjectContextResources(resources: ProjectContextResource[]): ProjectContextResource[] {
  return resources.map((resource, index) => normalizeProjectContextResource(resource, index));
}

export function validateProjectContextResources(resources: ProjectContextResource[]): { valid: boolean; errors: string[] } {
  const errors: string[] = [];
  if (!resources.length) {
    errors.push('Project context needs at least one repo, folder, file, or doc.');
  }
  const seen = new Set<string>();
  for (const resource of normalizeProjectContextResources(resources)) {
    const key = (resource.path ?? resource.repo ?? resource.uri ?? resource.name ?? resource.label ?? '').trim();
    if (!key) errors.push('Every project resource needs a repo, folder, file, or doc path.');
    if (
      resource.uri
      && /^browser(?:-|:)/.test(resource.uri)
      && !resource.path
      && !resource.uploaded_files?.length
      && !resource.allowed_paths?.length
    ) {
      errors.push('Local files and folders must be uploaded before agents can use them.');
    }
    if (key && seen.has(key)) errors.push(`Duplicate project resource: ${key}`);
    seen.add(key);
  }
  return { valid: errors.length === 0, errors };
}
