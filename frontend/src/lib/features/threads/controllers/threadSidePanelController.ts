export type ThreadStageRightDockTabKind =
  | 'browser'
  | 'discussion'
  | 'activity'
  | 'handoff-summary'
  | 'project'
  | 'app'
  | 'vault'
  | 'cycles'
  | 'preview'
  | 'code-review';

export type ThreadStageRightDockDynamicKind = 'browser' | 'app';
export type ThreadStageRightDockSingletonKind = Exclude<
  ThreadStageRightDockTabKind,
  ThreadStageRightDockDynamicKind
>;

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

export type ThreadStageRightDockTabDefinition = {
  kind: ThreadStageRightDockSingletonKind;
  id: ThreadStageRightDockSingletonKind;
  label: string;
  menuDescription: string;
  icon: 'activity' | 'code' | 'cycles' | 'document' | 'folder' | 'reply-thread' | 'vault';
  closeable: boolean;
};

const SINGLETON_TAB_DEFINITIONS = {
  discussion: {
    id: 'discussion',
    kind: 'discussion',
    label: 'Discussion',
    menuDescription: 'Open thread comments',
    icon: 'reply-thread',
    closeable: true,
  },
  activity: {
    id: 'activity',
    kind: 'activity',
    label: 'Activity',
    menuDescription: 'Open run activity',
    icon: 'activity',
    closeable: true,
  },
  'handoff-summary': {
    id: 'handoff-summary',
    kind: 'handoff-summary',
    label: 'Handoff',
    menuDescription: 'Open durable agent summary',
    icon: 'document',
    closeable: true,
  },
  project: {
    id: 'project',
    kind: 'project',
    label: 'Project',
    menuDescription: 'Review draft state',
    icon: 'folder',
    closeable: true,
  },
  vault: {
    id: 'vault',
    kind: 'vault',
    label: 'Vault',
    menuDescription: 'Add or review thread keys',
    icon: 'vault',
    closeable: true,
  },
  cycles: {
    id: 'cycles',
    kind: 'cycles',
    label: 'Cycles',
    menuDescription: 'Review scheduled Illo work',
    icon: 'cycles',
    closeable: true,
  },
  preview: {
    id: 'preview',
    kind: 'preview',
    label: 'Preview',
    menuDescription: 'Review selected attachment',
    icon: 'document',
    closeable: true,
  },
  'code-review': {
    id: 'code-review',
    kind: 'code-review',
    label: 'Review files',
    menuDescription: 'See files Illo changed',
    icon: 'code',
    closeable: true,
  },
} satisfies Record<ThreadStageRightDockSingletonKind, ThreadStageRightDockTabDefinition>;

export const THREAD_SIDE_PANEL_DEFAULT_TAB_KINDS = [
  'discussion',
  'activity',
  'handoff-summary',
  'project',
] as const satisfies readonly ThreadStageRightDockSingletonKind[];

const THREAD_SIDE_PANEL_ADD_MENU_KINDS = [
  'vault',
  'discussion',
  'project',
  'cycles',
  'code-review',
  'activity',
  'handoff-summary',
] as const satisfies readonly ThreadStageRightDockSingletonKind[];

export const THREAD_SIDE_PANEL_SINGLETON_TAB_DEFINITIONS =
  Object.values(SINGLETON_TAB_DEFINITIONS);

export function isThreadSidePanelSingletonKind(
  kind: ThreadStageRightDockTabKind,
): kind is ThreadStageRightDockSingletonKind {
  return kind in SINGLETON_TAB_DEFINITIONS;
}

export function threadSidePanelDefinitionForKind(
  kind: ThreadStageRightDockSingletonKind,
): ThreadStageRightDockTabDefinition {
  return SINGLETON_TAB_DEFINITIONS[kind];
}

export function threadSidePanelIconForKind(kind: ThreadStageRightDockTabKind) {
  if (kind === 'browser') return 'preview';
  if (kind === 'app') return 'code';
  return threadSidePanelDefinitionForKind(kind).icon;
}

function createSingletonTab(kind: ThreadStageRightDockSingletonKind): ThreadStageRightDockTab {
  const definition = threadSidePanelDefinitionForKind(kind);
  return {
    id: definition.id,
    label: definition.label,
    kind: definition.kind,
    closeable: definition.closeable,
  };
}

export function createDefaultThreadSidePanelTabs(): ThreadStageRightDockTab[] {
  return THREAD_SIDE_PANEL_DEFAULT_TAB_KINDS.map(createSingletonTab);
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
  const openKinds = new Set(tabs.map((tab) => tab.kind));
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

  for (const kind of THREAD_SIDE_PANEL_ADD_MENU_KINDS) {
    if (openKinds.has(kind)) continue;
    const definition = threadSidePanelDefinitionForKind(kind);
    items.push({
      id: definition.id,
      kind: definition.kind,
      label: definition.label,
      description: definition.menuDescription,
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
  kind: ThreadStageRightDockSingletonKind,
): ThreadSidePanelTabState {
  const existing = state.tabs.find((tab) => tab.kind === kind);
  if (existing) return { ...state, activeTabId: existing.id };

  const definition = threadSidePanelDefinitionForKind(kind);
  return {
    ...state,
    tabs: [
      ...state.tabs,
      createSingletonTab(definition.kind),
    ],
    activeTabId: definition.id,
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
