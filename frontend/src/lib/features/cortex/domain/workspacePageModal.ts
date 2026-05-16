export const WORKSPACE_PAGE_MODAL_PARAM = 'modal';

export type WorkspacePageModalId = 'cycles' | 'skills' | 'team' | 'vault' | 'system';

export type WorkspacePageModalSection = {
  id: WorkspacePageModalId;
  path: `/${WorkspacePageModalId}`;
  label: string;
  eyebrow: string;
  title: string;
  subtitle: string;
  glyph: WorkspacePageModalId | 'runtime';
};

export const WORKSPACE_PAGE_MODAL_SECTIONS: Record<WorkspacePageModalId, WorkspacePageModalSection> = {
  cycles: {
    id: 'cycles',
    path: '/cycles',
    label: 'Cycles',
    eyebrow: 'Workspace Control',
    title: 'Cycles',
    subtitle: 'Manage recurring prompts, scheduled follow-ups, and unattended work.',
    glyph: 'cycles',
  },
  skills: {
    id: 'skills',
    path: '/skills',
    label: 'Skills',
    eyebrow: 'Agent System',
    title: 'Skills',
    subtitle: 'Install, inspect, and refine the skills that shape Illo behavior.',
    glyph: 'skills',
  },
  team: {
    id: 'team',
    path: '/team',
    label: 'Team',
    eyebrow: 'Workspace Access',
    title: 'Team',
    subtitle: 'Review members, approvals, attribution, and shared usage.',
    glyph: 'team',
  },
  vault: {
    id: 'vault',
    path: '/vault',
    label: 'Vault',
    eyebrow: 'Secure Context',
    title: 'Vault',
    subtitle: 'Manage secrets, grants, and agent access without leaving Cortex.',
    glyph: 'vault',
  },
  system: {
    id: 'system',
    path: '/system',
    label: 'AI Runtime',
    eyebrow: 'Runtime',
    title: 'AI Runtime',
    subtitle: 'Configure models, memory, updates, and local runtime connection.',
    glyph: 'runtime',
  },
};

const WORKSPACE_PAGE_MODAL_IDS = new Set<WorkspacePageModalId>(
  Object.keys(WORKSPACE_PAGE_MODAL_SECTIONS) as WorkspacePageModalId[],
);

const WORKSPACE_PAGE_MODAL_PATHS = new Map<string, WorkspacePageModalId>(
  Object.values(WORKSPACE_PAGE_MODAL_SECTIONS).map((section) => [section.path, section.id]),
);

function normalizedPath(pathname: string): string {
  const path = pathname.replace(/\/+$/, '');
  return path || '/';
}

export function isWorkspacePageModalId(value: string | null | undefined): value is WorkspacePageModalId {
  return Boolean(value && WORKSPACE_PAGE_MODAL_IDS.has(value as WorkspacePageModalId));
}

export function workspacePageModalIdForPath(pathname: string): WorkspacePageModalId | null {
  return WORKSPACE_PAGE_MODAL_PATHS.get(normalizedPath(pathname)) ?? null;
}

export function buildCortexWorkspacePageHref(
  id: WorkspacePageModalId,
  sourceParams?: URLSearchParams,
): string {
  const params = new URLSearchParams(sourceParams);
  params.set(WORKSPACE_PAGE_MODAL_PARAM, id);
  const query = params.toString();
  return `/cortex${query ? `?${query}` : ''}`;
}

export function buildCortexHrefWithoutWorkspacePage(sourceParams?: URLSearchParams): string {
  const params = new URLSearchParams(sourceParams);
  params.delete(WORKSPACE_PAGE_MODAL_PARAM);
  const query = params.toString();
  return `/cortex${query ? `?${query}` : ''}`;
}
