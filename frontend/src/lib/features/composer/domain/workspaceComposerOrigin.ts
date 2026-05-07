export interface WorkspaceComposerContextLike {
  screenX: number;
  screenY: number;
  worldX?: number;
  worldY?: number;
}

export interface WorkspaceComposerScreenOrigin {
  x: number;
  y: number;
}

export interface WorkspaceComposerWorldOrigin {
  x: number;
  y: number;
}

export function getWorkspaceComposerScreenOrigin(
  context: WorkspaceComposerContextLike | null | undefined,
  viewport: Pick<Window, 'innerWidth' | 'innerHeight'>,
): WorkspaceComposerScreenOrigin {
  if (context) {
    return { x: context.screenX, y: context.screenY };
  }
  return {
    x: viewport.innerWidth / 2,
    y: viewport.innerHeight * 0.6,
  };
}

export function getWorkspaceComposerWorldOrigin(
  context: WorkspaceComposerContextLike | null | undefined,
): WorkspaceComposerWorldOrigin | null {
  if (typeof context?.worldX === 'number' && typeof context?.worldY === 'number') {
    return { x: context.worldX, y: context.worldY };
  }
  return null;
}
