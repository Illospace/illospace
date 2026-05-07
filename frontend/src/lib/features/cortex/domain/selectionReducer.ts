export type ThreadStageMode = 'closed' | 'opening' | 'open' | 'dismissing';

export type CortexWorkspaceSurface =
  | {
      kind: 'workspace';
      canvasOpen: boolean;
    }
  | {
      kind: 'thread';
      ideaId: string;
      mode: ThreadStageMode;
    }
  | {
      kind: 'generatedApp';
      appId: string;
      ideaId?: string | null;
    };

export type CortexSelectionSnapshot = {
  selectedIdeaId: string | null;
  panelOpen: boolean;
  canvasOpen: boolean;
  activeWorkspaceAppId?: string | null;
};

export function workspaceSurface(canvasOpen = false): CortexWorkspaceSurface {
  return { kind: 'workspace', canvasOpen };
}

export function threadSurface(ideaId: string, mode: ThreadStageMode = 'open'): CortexWorkspaceSurface {
  return { kind: 'thread', ideaId, mode };
}

export function generatedAppSurface(
  appId: string,
  ideaId: string | null | undefined = null,
): CortexWorkspaceSurface {
  return { kind: 'generatedApp', appId, ideaId };
}

export function cortexSurfaceFromSelection(
  snapshot: CortexSelectionSnapshot,
): CortexWorkspaceSurface {
  if (snapshot.panelOpen && snapshot.selectedIdeaId) {
    return threadSurface(snapshot.selectedIdeaId);
  }
  if (snapshot.activeWorkspaceAppId) {
    return generatedAppSurface(snapshot.activeWorkspaceAppId, snapshot.selectedIdeaId);
  }
  return workspaceSurface(snapshot.canvasOpen);
}

export function selectionFromCortexSurface(
  surface: CortexWorkspaceSurface,
): CortexSelectionSnapshot {
  if (surface.kind === 'thread') {
    return {
      selectedIdeaId: surface.ideaId,
      panelOpen: surface.mode !== 'closed',
      canvasOpen: false,
      activeWorkspaceAppId: null,
    };
  }
  if (surface.kind === 'generatedApp') {
    return {
      selectedIdeaId: surface.ideaId ?? null,
      panelOpen: false,
      canvasOpen: false,
      activeWorkspaceAppId: surface.appId,
    };
  }
  return {
    selectedIdeaId: null,
    panelOpen: false,
    canvasOpen: surface.canvasOpen,
    activeWorkspaceAppId: null,
  };
}

export function isThreadSurface(
  surface: CortexWorkspaceSurface,
): surface is Extract<CortexWorkspaceSurface, { kind: 'thread' }> {
  return surface.kind === 'thread';
}

export function isGeneratedAppSurface(
  surface: CortexWorkspaceSurface,
): surface is Extract<CortexWorkspaceSurface, { kind: 'generatedApp' }> {
  return surface.kind === 'generatedApp';
}
