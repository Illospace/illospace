import type { WorkspaceAppRead } from '$lib/features/workspace-apps/api/workspaceAppsApi';

export function workspaceAppOrbitOrder(app: WorkspaceAppRead): number | null {
  const fallbackOrderByKey: Record<string, number> = {
    'todo-surface': 10,
    'outreach-radar': 20,
    'pipeline-snapshot': 30,
    'notes-board': 40,
  };
  const rawOrder = app.visual_spec?.orbit_order
    ?? app.metadata?.prototype_order
    ?? fallbackOrderByKey[app.key];
  const order = Number(rawOrder);
  return Number.isFinite(order) ? order : null;
}

export function workspaceAppStoredPosition(app: WorkspaceAppRead): { x: number; y: number } | null {
  const position = app.visual_spec?.position;
  const rawX = app.visual_spec?.position_x ?? app.visual_spec?.workspace_x ?? position?.x;
  const rawY = app.visual_spec?.position_y ?? app.visual_spec?.workspace_y ?? position?.y;
  const x = Number(rawX);
  const y = Number(rawY);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}
