export type ThreadStageRightDockTabKind = 'browser' | 'activity' | 'app' | 'vault';

export type ThreadStageRightDockTab = {
  id: string;
  label: string;
  kind: ThreadStageRightDockTabKind;
  appId?: string | null;
  closeable?: boolean;
};

export type ThreadStageRightDockAddMenuItem = {
  id: string;
  label: string;
  description?: string;
  kind: ThreadStageRightDockTabKind;
  appId?: string | null;
  disabled?: boolean;
};

export type ThreadSidePanelAppLike = {
  id: string;
  key?: string | null;
  name?: string | null;
  description?: string | null;
};

export type ThreadSidePanelTabState = {
  tabs: ThreadStageRightDockTab[];
  activeTabId: string | null;
  nextBrowserTabIndex: number;
};

export function createDefaultThreadSidePanelTabs(): ThreadStageRightDockTab[] {
  return [{ id: 'activity', label: 'Activity', kind: 'activity', closeable: true }];
}

export function activeThreadSidePanelTab(
  tabs: readonly ThreadStageRightDockTab[],
  activeTabId: string | null,
): ThreadStageRightDockTab | null {
  return tabs.find((tab) => tab.id === activeTabId) ?? tabs[0] ?? null;
}

export function buildThreadSidePanelAddMenuItems(
  tabs: readonly ThreadStageRightDockTab[],
  visibleApps: readonly ThreadSidePanelAppLike[],
): ThreadStageRightDockAddMenuItem[] {
  const browserCount = tabs.filter((tab) => tab.kind === 'browser').length;
  const hasActivity = tabs.some((tab) => tab.kind === 'activity');
  const hasVault = tabs.some((tab) => tab.kind === 'vault');
  const openAppIds = new Set(
    tabs
      .filter((tab) => tab.kind === 'app' && tab.appId)
      .map((tab) => String(tab.appId)),
  );
  const items: ThreadStageRightDockAddMenuItem[] = [
    {
      id: 'new-browser',
      kind: 'browser',
      label: browserCount > 0 ? 'New browser' : 'Browser',
      description: browserCount > 0 ? 'Open another browser panel' : 'Open browser panel',
    },
  ];

  if (!hasVault) {
    items.push({
      id: 'vault',
      kind: 'vault',
      label: 'Vault',
      description: 'Add or review thread keys',
    });
  }

  if (!hasActivity) {
    items.push({
      id: 'activity',
      kind: 'activity',
      label: 'Activity',
      description: 'Open run activity',
    });
  }

  const availableApps = visibleApps.filter((app) => !openAppIds.has(app.id));
  for (const app of availableApps) {
    items.push({
      id: `app:${app.id}`,
      kind: 'app',
      appId: app.id,
      label: app.name || app.key || 'Generated app',
      description: app.description || 'Generated workspace app',
    });
  }

  if (availableApps.length === 0) {
    items.push({
      id: 'no-apps',
      kind: 'app',
      label: visibleApps.length > 0 ? 'All apps are open' : 'No apps available',
      description: visibleApps.length > 0 ? 'Close an app tab to reopen it here' : 'Generated apps appear here',
      disabled: true,
    });
  }

  return items;
}

export function activateThreadSidePanelTab(
  tabs: readonly ThreadStageRightDockTab[],
  tabId: string | null,
): string | null {
  const nextTab = tabId ? tabs.find((tab) => tab.id === tabId) : null;
  return nextTab?.id ?? tabs[0]?.id ?? null;
}

export function addBrowserThreadSidePanelTab(
  state: ThreadSidePanelTabState,
): ThreadSidePanelTabState {
  const browserCount = state.tabs.filter((tab) => tab.kind === 'browser').length;
  const id = `browser-${state.nextBrowserTabIndex}`;
  const nextTab: ThreadStageRightDockTab = {
    id,
    kind: 'browser',
    label: browserCount === 0 ? 'Browser' : `Browser ${browserCount + 1}`,
    closeable: true,
  };
  return {
    tabs: [...state.tabs, nextTab],
    activeTabId: id,
    nextBrowserTabIndex: state.nextBrowserTabIndex + 1,
  };
}

export function openBrowserThreadSidePanelTab(
  state: ThreadSidePanelTabState,
): ThreadSidePanelTabState {
  const existing = state.tabs.find((tab) => tab.kind === 'browser');
  if (existing) return { ...state, activeTabId: existing.id };
  return addBrowserThreadSidePanelTab(state);
}

export function openSingletonThreadSidePanelTab(
  state: ThreadSidePanelTabState,
  kind: 'activity' | 'vault',
): ThreadSidePanelTabState {
  const existing = state.tabs.find((tab) => tab.kind === kind);
  if (existing) return { ...state, activeTabId: existing.id };

  const label = kind === 'vault' ? 'Vault' : 'Activity';
  return {
    ...state,
    tabs: [
      ...state.tabs,
      { id: kind, label, kind, closeable: true },
    ],
    activeTabId: kind,
  };
}

export function openAppThreadSidePanelTab(
  state: ThreadSidePanelTabState,
  appId: string | null | undefined,
  app?: ThreadSidePanelAppLike | null,
): ThreadSidePanelTabState {
  if (!appId) return state;

  const existing = state.tabs.find((tab) => tab.kind === 'app' && tab.appId === appId);
  if (existing) return { ...state, activeTabId: existing.id };

  const id = `app-${appId}`;
  return {
    ...state,
    tabs: [
      ...state.tabs,
      {
        id,
        kind: 'app',
        appId,
        label: app?.name || app?.key || 'App',
        closeable: true,
      },
    ],
    activeTabId: id,
  };
}

export function closeThreadSidePanelTab(
  tabs: readonly ThreadStageRightDockTab[],
  activeTabId: string | null,
  tabId: string,
): { tabs: ThreadStageRightDockTab[]; activeTabId: string | null } {
  const tabIndex = tabs.findIndex((tab) => tab.id === tabId);
  if (tabIndex < 0) return { tabs: [...tabs], activeTabId };

  const wasActive = activeTabId === tabId;
  const nextTabs = tabs.filter((tab) => tab.id !== tabId);
  return {
    tabs: nextTabs,
    activeTabId: wasActive
      ? nextTabs[tabIndex]?.id ?? nextTabs[tabIndex - 1]?.id ?? nextTabs[0]?.id ?? null
      : activeTabId,
  };
}
