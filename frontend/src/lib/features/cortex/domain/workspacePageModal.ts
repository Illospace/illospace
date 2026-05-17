export const WORKSPACE_PAGE_MODAL_PARAM = 'modal';

export type WorkspacePageModalId = 'cycles' | 'skills' | 'team' | 'vault' | 'system';

export type WorkspacePageModalSection = {
  id: WorkspacePageModalId;
  path: `/${WorkspacePageModalId}`;
  label: string;
  title: string;
  subtitle: string;
  glyph: WorkspacePageModalId | 'runtime';
};

export const WORKSPACE_PAGE_MODAL_SECTIONS: Record<WorkspacePageModalId, WorkspacePageModalSection> = {
  cycles: {
    id: 'cycles',
    path: '/cycles',
    label: 'Cycles',
    title: 'Cycles',
    subtitle: 'Manage recurring work without leaving Cortex.',
    glyph: 'cycles',
  },
  skills: {
    id: 'skills',
    path: '/skills',
    label: 'Skills',
    title: 'Skills',
    subtitle: 'Shape the workflows Illo can use.',
    glyph: 'skills',
  },
  team: {
    id: 'team',
    path: '/team',
    label: 'Team',
    title: 'Team',
    subtitle: 'Manage members, approvals, and access.',
    glyph: 'team',
  },
  vault: {
    id: 'vault',
    path: '/vault',
    label: 'Vault',
    title: 'Vault',
    subtitle: 'Manage secrets and agent access.',
    glyph: 'vault',
  },
  system: {
    id: 'system',
    path: '/system',
    label: 'AI Runtime',
    title: 'AI Runtime',
    subtitle: 'Configure providers, model routing, and memory.',
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
