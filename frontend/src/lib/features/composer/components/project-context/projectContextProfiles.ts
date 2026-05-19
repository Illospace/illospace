import type { ProjectContextResource } from '$lib/utils/projectContext';

export type ProjectContextProfile = {
  id: string;
  name: string;
  description: string;
  resources: ProjectContextResource[];
  serverProfileId?: string;
  userId?: string;
  visibility?: ProjectVisibility;
  access?: ProjectProfileAccess[];
};

export type ConnectorMode = 'menu' | 'github' | 'local';
export type ProjectVisibility = 'private' | 'public';

export type ProjectProfileAccess = {
  user_id: string;
  name: string;
  email?: string | null;
  shared_by_user_id?: string | null;
  created_at?: string | null;
};

const RESOURCE_TYPE_LABELS: Record<string, string> = {
  repo: 'GitHub',
  repository: 'GitHub',
  folder: 'Folder',
  file: 'File',
  doc: 'Doc',
};

export const BUILTIN_PROJECT_CONTEXT_PROFILES: ProjectContextProfile[] = [
  {
    id: 'none',
    name: 'No project context',
    description: 'Send without a scoped project.',
    resources: [],
  },
];

export function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 120) || 'project';
}

export function mapServerProjectProfile(profile: any): ProjectContextProfile {
  return {
    id: `server:${profile.id}`,
    serverProfileId: profile.id,
    userId: profile.user_id,
    name: profile.name,
    description: profile.description ?? 'Saved project profile',
    visibility: profile.visibility === 'public' ? 'public' : 'private',
    access: Array.isArray(profile.access) ? profile.access : [],
    resources: Array.isArray(profile.project_context?.resources)
      ? profile.project_context.resources
      : [],
  };
}

export function resourceLabel(resource: ProjectContextResource): string {
  const label = resource.label ?? resource.path ?? resource.name ?? resource.repo ?? resource.uri ?? 'Project resource';
  if (typeof resource.file_count === 'number' && resource.file_count > 0) {
    return `${label} (${resource.file_count} files)`;
  }
  return label;
}

export function resourceLocator(resource: ProjectContextResource): string {
  return resource.path ?? resource.name ?? resource.repo ?? resource.uri ?? resource.label ?? '';
}

export function resourceKind(resource: ProjectContextResource): string {
  return (
    RESOURCE_TYPE_LABELS[resource.type ?? '']
    ?? RESOURCE_TYPE_LABELS[resource.kind ?? '']
    ?? String(resource.type ?? resource.kind ?? 'Resource')
  );
}

export function projectContextErrorDetail(err: any, fallback: string): string {
  const detail = err?.detail ?? err?.message;
  if (typeof detail === 'string' && detail.trim()) return detail;
  return fallback;
}

export function vaultProjectContextErrorMessage(err: any, fallback = 'Vault unavailable.'): string {
  const detail = projectContextErrorDetail(err, fallback);
  if (detail.includes('VAULT_MASTER_KEY') || detail.toLowerCase().includes('vault master key')) {
    return 'Vault is not configured on this server. Set VAULT_MASTER_KEY to save or reveal tokens.';
  }
  return detail;
}
