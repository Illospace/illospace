export type ThreadArtifactDeepLinkDecision =
  | { action: 'idle' }
  | { action: 'already-opened'; appId: string }
  | { action: 'request-refresh'; appId: string }
  | { action: 'wait-for-app'; appId: string }
  | { action: 'wait-for-thread'; appId: string }
  | { action: 'ignore-wrong-thread'; appId: string }
  | { action: 'open'; appId: string };

export interface ThreadArtifactDeepLinkInput {
  requestedAppId?: string | null;
  lastAutoOpenedAppId?: string | null;
  appExists: boolean;
  appBelongsToCurrentThread: boolean;
  currentThreadLoaded: boolean;
  loadRequestedForAppId?: string | null;
  appsLoading: boolean;
}

export function decideThreadArtifactDeepLink({
  requestedAppId,
  lastAutoOpenedAppId,
  appExists,
  appBelongsToCurrentThread,
  currentThreadLoaded,
  loadRequestedForAppId,
  appsLoading,
}: ThreadArtifactDeepLinkInput): ThreadArtifactDeepLinkDecision {
  const appId = String(requestedAppId ?? '').trim();
  if (!appId) return { action: 'idle' };
  if (appId === lastAutoOpenedAppId) return { action: 'already-opened', appId };

  if (!appExists) {
    if (appsLoading || loadRequestedForAppId === appId) {
      return { action: 'wait-for-app', appId };
    }
    return { action: 'request-refresh', appId };
  }

  if (!currentThreadLoaded) return { action: 'wait-for-thread', appId };
  if (!appBelongsToCurrentThread) return { action: 'ignore-wrong-thread', appId };
  return { action: 'open', appId };
}
