<script lang="ts">
  import { browser } from '$app/environment';
  import { page } from '$app/stores';
  import { onMount, onDestroy } from 'svelte';
  import { fly } from 'svelte/transition';
  import type { AppNotification } from '$lib/features/notifications/api/notificationsApi';
  import type { WorkspaceAppRead } from '$lib/features/workspace-apps/api/workspaceAppsApi';
  import type { WorkspacePinRead } from '$lib/features/workspace-scene/api/workspacePinsApi';
  import { auth } from '$lib/stores/auth.svelte';
  import { cortex } from '$lib/features/cortex/controllers/cortexFacade.svelte';
  import {
    buildThreadAmbientTone,
    buildThreadPeripherySignals,
    createLazyComponentController,
    createWorkspaceFieldLayoutOptions,
    createWorkspaceOverlayController,
    createWorkspaceThreadStageController,
    handleWorkspaceKeydown,
    handleWorkspaceKeyup,
    type WorkspaceFieldLayoutOptions,
  } from '$lib/features/cortex/controllers';
  import type { Idea } from '$lib/types/cortex';
  import { chat } from '$lib/stores/chat.svelte';
  import { notifications } from '$lib/stores/notifications.svelte';
  import { presence } from '$lib/stores/presence.svelte';
  import { THEME_OPTIONS, theme, type ThemeId } from '$lib/stores/theme.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import {
    ConstellationSegmentedToggle,
    ConstellationWorkspaceBackdrop,
  } from '$lib/design-system/constellation';
  import type { CortexWorkspacePoint } from '$lib/features/workspace-scene/domain/workspacePoint';
  import { isLocalPreviewMemberId } from '$lib/utils/cortexLocalPreview';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import { workspacePins } from '$lib/stores/workspacePins.svelte';
  import CortexLocalPreviewControls from '$lib/features/cortex/components/LocalPreviewControls.svelte';
  import WorkspaceChatDock from '$lib/features/cortex/components/chat/WorkspaceChatDock.svelte';
  import type { CortexChatDockTopLevelMode } from '$lib/features/cortex/components/chat/ChatDockSeam.svelte';

  const RUNTIME_READY_ONBOARDING_PARAM = 'runtime-ready';
  const workspaceOverlay = createWorkspaceOverlayController();
  const threadStage = createWorkspaceThreadStageController();
  const lazyComponents = createLazyComponentController();
  let chatDockExpanded = $state(false);
  let chatTopLevelMode = $state<CortexChatDockTopLevelMode>('room');
  let chatSelectedThreadRootId = $state<string | null>(null);
  let chatSelectedConversationId = $state<string | null>(null);
  let chatSelectedPreviewMemberId = $state<string | null>(null);
  let chatDockForeground = $state(false);
  let workspacePinsWsReady = false;
  let workspaceEl: HTMLDivElement | undefined = $state();
  let workspaceSceneSidecarsReady = $state(false);
  let lastRequestedIdeaId = $state<string | null>(null);
  let initialDirectThreadIdeaId = $state<string | null>($page.url.searchParams.get('idea'));
  let directThreadUrlPending = $state(Boolean($page.url.searchParams.get('idea')));
  let lastAutoOpenedAppId = $state<string | null>(null);
  let CortexArchiveBinMenuComponent = $state<typeof import('$lib/features/cortex/components/ArchiveBinMenu.svelte').default | null>(null);
  let WorkspaceSceneComponent = $state<typeof import('$lib/features/workspace-scene/components/WorkspaceScene.svelte').default | null>(null);
  let CortexChatDockSeamComponent = $state<typeof import('$lib/features/cortex/components/chat/ChatDockSeam.svelte').default | null>(null);
  let CortexListViewComponent = $state<typeof import('$lib/features/cortex/components/ListView.svelte').default | null>(null);
  let CortexNotificationsMenuComponent = $state<typeof import('$lib/features/notifications/components/NotificationsMenu.svelte').default | null>(null);
  let CortexOpsComponent = $state<typeof import('$lib/features/cortex/components/OpsOverlay.svelte').default | null>(null);
  let ThreadStageScreenComponent = $state<typeof import('$lib/features/threads/components/ThreadStageScreen.svelte').default | null>(null);
  let CortexTimelineComponent = $state<typeof import('$lib/features/cortex/components/TimelineOverlay.svelte').default | null>(null);
  let CortexUserMenuComponent = $state<typeof import('$lib/features/cortex/components/menus/UserMenu.svelte').default | null>(null);
  let CortexWorkspaceMenuComponent = $state<typeof import('$lib/features/cortex/components/menus/WorkspaceMenu.svelte').default | null>(null);
  let CortexWorkspacePinMenuComponent = $state<typeof import('$lib/features/cortex/components/menus/WorkspacePinMenu.svelte').default | null>(null);
  let GeneratedAppRendererComponent = $state<typeof import('$lib/features/workspace-apps/components/GeneratedAppOverlay.svelte').default | null>(null);
  let WorkspaceComposerComponent = $state<typeof import('$lib/features/composer/components/WorkspaceComposer.svelte').default | null>(null);
  const activeWorkspaceApp = $derived(workspaceApps.appById(workspaceOverlay.activeWorkspaceAppId));
  const chatPreviewMembers = $derived.by(() => {
    return cortex.teamMembers
      .filter((member) => isLocalPreviewMemberId(member?.id))
      .map((member) => ({
        id: member.id,
        name: member.name,
        email: member.email,
        color: member.color,
        approved: true,
      }));
  });

  function isRuntimeReadyOnboardingUrl() {
    return $page.url.searchParams.get('onboarding') === RUNTIME_READY_ONBOARDING_PARAM;
  }

  function isRuntimeReadyIntroIdea(idea: Idea | null | undefined) {
    return idea?.origin === 'onboarding' && idea.agent_details?.onboarding?.intro === true;
  }

  function clearRuntimeReadyOnboardingUrl() {
    if (!browser || !isRuntimeReadyOnboardingUrl() || !isRuntimeReadyIntroIdea(cortex.selectedIdea)) return;
    const url = new URL(window.location.href);
    url.searchParams.delete('idea');
    url.searchParams.delete('onboarding');
    const nextPath = `${url.pathname}${url.search}${url.hash}`;
    window.history.replaceState(window.history.state, '', nextPath);
  }

  $effect(() => {
    const changedAppId = workspaceApps.lastChangedAppId;
    if (!changedAppId || changedAppId === lastAutoOpenedAppId) return;
    if (workspaceApps.lastChangeAction !== 'create') return;
    if (cortex.panelOpen) return;
    if (!workspaceApps.appById(changedAppId)) return;
    lastAutoOpenedAppId = changedAppId;
    workspaceOverlay.openWorkspaceApp(changedAppId);
  });

  function handleThreadOpen(origin: { x: number; y: number; id: string }) {
    workspaceOverlay.closeWorkspaceApp();
    workspaceOverlay.closeWorkspaceAndPinMenus();
    threadStage.setOriginFromClient(workspaceEl, origin.x, origin.y);
  }

  function handleArchiveDragState(state: { active: boolean; over: boolean }) {
    workspaceOverlay.setArchiveDragState(state);
  }

  function resetArchiveDragState() {
    workspaceOverlay.resetArchiveDragState();
  }

  function handleArchivePointerRelease() {
    resetArchiveDragState();
  }

  async function handleRestoreArchivedThread(idea: Idea, event: MouseEvent) {
    workspaceOverlay.closeWorkspaceApp();
    workspaceOverlay.closeWorkspaceAndPinMenus();
    workspaceOverlay.closeUserMenu();
    threadStage.setOriginFromClient(workspaceEl, event.clientX, event.clientY);
    const restored = await cortex.restoreIdea(idea.id);
    if (restored) {
      await cortex.selectIdea(restored.id);
      ui.toast('Thread restored', 'success');
    }
  }

  async function handleRestoreArchivedApp(app: WorkspaceAppRead, _event: MouseEvent) {
    workspaceOverlay.closeWorkspaceAndPinMenus();
    workspaceOverlay.closeUserMenu();
    const restored = await workspaceApps.restore(app.id);
    if (restored) {
      workspaceOverlay.openWorkspaceApp(restored.id);
      ui.toast('App restored', 'success');
    }
  }

  async function handleWorkspaceAppOpen(origin: { x: number; y: number; appId: string }) {
    workspaceOverlay.openWorkspaceAppOverlay(origin.appId);
    threadStage.setCenteredOrigin('50%', '50%');
    if (cortex.panelOpen) {
      await cortex.selectIdea(null);
    }
  }

  async function handleMoveWorkspaceAppToPosition(move: { appId: string; x: number; y: number }) {
    const app = workspaceApps.appById(move.appId);
    if (!app) return;

    try {
      await workspaceApps.moveToPosition(move.appId, move.x, move.y);
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to move workspace app', 'error');
      throw err;
    }
  }

  async function handleArchiveWorkspaceAppFromBin(archive: { appId: string }) {
    const app = workspaceApps.appById(archive.appId);
    if (!app) return;

    try {
      if (workspaceOverlay.activeWorkspaceAppId === archive.appId) {
        workspaceOverlay.closeWorkspaceApp();
      }
      await workspaceApps.archive(archive.appId);
      ui.toast('App archived. Domain data stayed intact.', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to delete workspace app', 'error');
      throw err;
    } finally {
      resetArchiveDragState();
    }
  }

  function handleWorkspaceContext(point: CortexWorkspacePoint) {
    workspaceOverlay.setComposerContext(point);
  }

  function handleWorkspaceContextMenu(point: CortexWorkspacePoint) {
    workspaceOverlay.openWorkspaceMenu(point);
  }

  async function openDmForUser(userId: string) {
    if (!userId || userId === auth.user?.id) return;
    if (isLocalPreviewMemberId(userId)) {
      workspaceOverlay.closeWorkspaceApp();
      chatDockExpanded = true;
      chatTopLevelMode = 'dms';
      chatSelectedThreadRootId = null;
      chatSelectedConversationId = null;
      chatSelectedPreviewMemberId = userId;
      return;
    }

    try {
      await chat.setup();
      workspaceOverlay.closeWorkspaceApp();
      chatDockExpanded = true;
      chatTopLevelMode = 'dms';
      chatSelectedThreadRootId = null;
      chatSelectedPreviewMemberId = null;
      const conversation = await chat.ensureDm(userId, { select: true });
      chatSelectedConversationId = conversation.id;
    } catch (err: any) {
      ui.toast(err?.detail || err?.message || 'Could not open DM', 'error');
    }
  }

  function handleOwnAstreClick(origin: { x: number; y: number; userId: string }) {
    if (origin.userId !== auth.user?.id) {
      void openDmForUser(origin.userId);
      return;
    }
    workspaceOverlay.openUserMenu({ x: origin.x, y: origin.y });
  }

  async function handleCreateWorkspacePin(point: CortexWorkspacePoint) {
    workspaceOverlay.closeWorkspaceMenu();
    try {
      await workspacePins.create({
        label: 'New Pin',
        position_x: point.worldX,
        position_y: point.worldY,
      });
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to create pin', 'error');
    }
  }

  function handleWorkspacePinMenu(origin: { x: number; y: number; pinId: string }) {
    const pin = workspacePins.pinById(origin.pinId);
    if (!pin || pin.created_by_user_id !== auth.user?.id) return;
    workspaceOverlay.openPinMenu({ x: origin.x, y: origin.y, pin });
  }

  async function handleRenameWorkspacePin(pin: WorkspacePinRead, label: string) {
    if (!label.trim()) {
      ui.toast('Pin name is required', 'error');
      return;
    }
    workspaceOverlay.setPinMenuSaving(true);
    try {
      const updated = await workspacePins.update(pin.id, { label: label.trim() });
      workspaceOverlay.updatePinMenuPin(pin.id, updated);
      ui.toast('Pin renamed', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to rename pin', 'error');
    } finally {
      workspaceOverlay.setPinMenuSaving(false);
    }
  }

  async function handleDeleteWorkspacePin(pin: WorkspacePinRead) {
    workspaceOverlay.setPinMenuDeleting(true);
    try {
      await workspacePins.deletePin(pin.id);
      workspaceOverlay.closePinMenu();
      ui.toast('Pin deleted', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to delete pin', 'error');
      workspaceOverlay.setPinMenuDeleting(false);
    }
  }

  async function handleDeleteWorkspacePinFromBin(deletePin: { pinId: string }) {
    const pin = workspacePins.pinById(deletePin.pinId);
    if (!pin) return;

    try {
      if (workspaceOverlay.pinMenuAnchor?.pin.id === deletePin.pinId) {
        workspaceOverlay.closePinMenu();
      }
      await workspacePins.deletePin(deletePin.pinId);
      ui.toast('Pin deleted', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to delete pin', 'error');
      throw err;
    } finally {
      resetArchiveDragState();
    }
  }

  async function handleMoveWorkspacePinToPosition(move: { pinId: string; x: number; y: number }) {
    const previous = workspacePins.pinById(move.pinId);
    workspacePins.patchLocal(move.pinId, {
      position_x: move.x,
      position_y: move.y,
    });
    try {
      await workspacePins.update(move.pinId, {
        position_x: move.x,
        position_y: move.y,
      });
    } catch (err: any) {
      if (previous) {
        workspacePins.patchLocal(move.pinId, {
          position_x: previous.position_x,
          position_y: previous.position_y,
        });
      }
      ui.toast(err?.detail || 'Failed to move pin', 'error');
      throw err;
    }
  }

  function handleWorkspaceThreadIntent(origin: { x: number; y: number }) {
    threadStage.setOriginFromClient(workspaceEl, origin.x, origin.y);
  }

  function handleThreadStageDismiss() {
    cortex.selectIdea(null);
    initialDirectThreadIdeaId = null;
    ensureWorkspaceRealtime();
    void cortex.loadTeamMembers();
    void loadWorkspaceSceneSidecars();
  }

  async function loadWorkspaceSceneSidecars() {
    workspaceSceneSidecarsReady = false;
    ensureWorkspacePinsWs();
    ensureWorkspaceRealtime();
    const bootstrap = await cortex.loadWorkspaceBootstrap();
    if (Array.isArray(bootstrap?.workspace_apps) && Array.isArray(bootstrap?.workspace_pins)) {
      workspaceApps.hydrate(bootstrap.workspace_apps);
      workspacePins.hydrate(bootstrap.workspace_pins);
      workspaceSceneSidecarsReady = true;
      return;
    }
    await Promise.all([
      workspaceApps.load({ silent: true }),
      workspacePins.load({ silent: true }),
    ]);
    workspaceSceneSidecarsReady = true;
  }

  function runWhenBrowserIdle(callback: () => void, delayMs = 0, timeout = 1500) {
    if (!browser) return;
    window.setTimeout(() => {
      const requestIdle = (window as typeof window & {
        requestIdleCallback?: (cb: () => void, options?: { timeout?: number }) => number;
      }).requestIdleCallback;
      if (requestIdle) {
        requestIdle(callback, { timeout });
        return;
      }
      window.setTimeout(callback, Math.min(timeout, 500));
    }, delayMs);
  }

  function schedulePostStartupHydration(requestedIdeaId: string | null) {
    runWhenBrowserIdle(() => {
      void notifications.setup();
    }, requestedIdeaId ? 1600 : 900, 2200);
    if (!requestedIdeaId) {
      runWhenBrowserIdle(() => {
        if (cortexSurfaceReady && !CortexChatDockSeamComponent) {
          void ensureCortexChatDockLoaded();
        }
      }, 2200, 4200);
    }
  }

  function handleThemeChange(nextThemeId: string) {
    if (nextThemeId === 'constellation' || nextThemeId === 'daylight') {
      theme.setTheme(nextThemeId as ThemeId);
    }
  }

  function handleChatTopLevelModeChange(nextMode: CortexChatDockTopLevelMode) {
    chatTopLevelMode = nextMode;
    if (nextMode === 'room') {
      chatSelectedPreviewMemberId = null;
    }
  }

  function handleChatOpenRoomThread(threadRootId: string) {
    chatSelectedThreadRootId = threadRootId;
    chatTopLevelMode = 'room';
    chatSelectedPreviewMemberId = null;
  }

  function handleChatCloseRoomThread() {
    chatSelectedThreadRootId = null;
  }

  function handleChatOpenConversation(conversationId: string) {
    chatSelectedConversationId = conversationId;
    chatTopLevelMode = 'dms';
    chatSelectedPreviewMemberId = null;
  }

  function handleChatCloseConversation() {
    chatSelectedConversationId = null;
  }

  function compactWorkspaceChat() {
    chatDockExpanded = false;
    chatDockForeground = false;
  }

  async function handleNotificationSelect(notification: AppNotification) {
    await notifications.markRead(notification.id);
    workspaceOverlay.closeWorkspaceApp();

    if (notification.idea_id) {
      await cortex.selectIdea(notification.idea_id);
      return;
    }

    if (!notification.conversation_id) return;

    if (cortex.panelOpen) {
      await cortex.selectIdea(null);
    }

    chatDockExpanded = true;
    chatSelectedConversationId = notification.conversation_id;

    if (notification.kind === 'chat.dm_message') {
      chatTopLevelMode = 'dms';
      chatSelectedThreadRootId = null;
      return;
    }

    chatTopLevelMode = 'room';
    chatSelectedThreadRootId = notification.thread_root_message_id != null
      ? String(notification.thread_root_message_id)
      : null;
  }

  $effect(() => {
    threadStage.syncPanelOpen(cortex.panelOpen);
  });

  const threadWorkspaceStyle = $derived.by(() => threadStage.workspaceStyle);

  const threadAmbientTone = $derived(buildThreadAmbientTone(cortex.selectedIdea?.status ?? 'idle'));
  const cortexVisualReady = $derived(Boolean(auth.user?.id) || cortex.teamMembersLoaded);
  const cortexSurfaceReady = $derived(
    cortexVisualReady && (!cortex.loading || cortex.ideas.length > 0 || cortex.connections.length > 0),
  );
  const directThreadActive = $derived(Boolean(initialDirectThreadIdeaId && cortex.panelOpen));
  const shouldRenderWorkspaceScene = $derived(
    cortexSurfaceReady
      && workspaceSceneSidecarsReady
      && !directThreadUrlPending
      && !directThreadActive,
  );
  const cortexWorkspaceLoading = $derived(
    cortex.loading || (!directThreadUrlPending && !directThreadActive && !workspaceSceneSidecarsReady),
  );

  const createLazyComponentLoader = lazyComponents.createLoader;

  const ensureCortexArchiveBinMenuLoaded = createLazyComponentLoader(
    'archive-bin',
    () => CortexArchiveBinMenuComponent,
    (component) => (CortexArchiveBinMenuComponent = component),
    () => import('$lib/features/cortex/components/ArchiveBinMenu.svelte'),
  );
  const ensureWorkspaceSceneLoaded = createLazyComponentLoader(
    'canvas',
    () => WorkspaceSceneComponent,
    (component) => (WorkspaceSceneComponent = component),
    () => import('$lib/features/workspace-scene/components/WorkspaceScene.svelte'),
  );
  const ensureCortexChatDockLoaded = createLazyComponentLoader(
    'chat-dock',
    () => CortexChatDockSeamComponent,
    (component) => (CortexChatDockSeamComponent = component),
    () => import('$lib/features/cortex/components/chat/ChatDockSeam.svelte'),
  );
  const ensureCortexListViewLoaded = createLazyComponentLoader(
    'list-view',
    () => CortexListViewComponent,
    (component) => (CortexListViewComponent = component),
    () => import('$lib/features/cortex/components/ListView.svelte'),
  );
  const ensureCortexNotificationsMenuLoaded = createLazyComponentLoader(
    'notifications-menu',
    () => CortexNotificationsMenuComponent,
    (component) => (CortexNotificationsMenuComponent = component),
    () => import('$lib/features/notifications/components/NotificationsMenu.svelte'),
  );
  const ensureCortexOpsLoaded = createLazyComponentLoader(
    'ops',
    () => CortexOpsComponent,
    (component) => (CortexOpsComponent = component),
    () => import('$lib/features/cortex/components/OpsOverlay.svelte'),
  );
  const ensureThreadStageScreenLoaded = createLazyComponentLoader(
    'thread-stage',
    () => ThreadStageScreenComponent,
    (component) => (ThreadStageScreenComponent = component),
    () => import('$lib/features/threads/components/ThreadStageScreen.svelte'),
  );
  const ensureCortexTimelineLoaded = createLazyComponentLoader(
    'timeline',
    () => CortexTimelineComponent,
    (component) => (CortexTimelineComponent = component),
    () => import('$lib/features/cortex/components/TimelineOverlay.svelte'),
  );
  const ensureCortexUserMenuLoaded = createLazyComponentLoader(
    'user-menu',
    () => CortexUserMenuComponent,
    (component) => (CortexUserMenuComponent = component),
    () => import('$lib/features/cortex/components/menus/UserMenu.svelte'),
  );
  const ensureCortexWorkspaceMenuLoaded = createLazyComponentLoader(
    'workspace-menu',
    () => CortexWorkspaceMenuComponent,
    (component) => (CortexWorkspaceMenuComponent = component),
    () => import('$lib/features/cortex/components/menus/WorkspaceMenu.svelte'),
  );
  const ensureCortexWorkspacePinMenuLoaded = createLazyComponentLoader(
    'pin-menu',
    () => CortexWorkspacePinMenuComponent,
    (component) => (CortexWorkspacePinMenuComponent = component),
    () => import('$lib/features/cortex/components/menus/WorkspacePinMenu.svelte'),
  );
  const ensureGeneratedAppRendererLoaded = createLazyComponentLoader(
    'generated-app',
    () => GeneratedAppRendererComponent,
    (component) => (GeneratedAppRendererComponent = component),
    () => import('$lib/features/workspace-apps/components/GeneratedAppOverlay.svelte'),
  );
  const ensureWorkspaceComposerLoaded = createLazyComponentLoader(
    'workspace-composer',
    () => WorkspaceComposerComponent,
    (component) => (WorkspaceComposerComponent = component),
    () => import('$lib/features/composer/components/WorkspaceComposer.svelte'),
  );

  $effect(() => {
    if (!shouldRenderWorkspaceScene) return;
    if (cortex.view === 'list') {
      ensureCortexListViewLoaded();
    } else {
      ensureWorkspaceSceneLoaded();
    }
  });

  $effect(() => {
    if (shouldRenderWorkspaceScene || chatDockExpanded || activeWorkspaceApp) {
      ensureWorkspaceRealtime();
    } else if (cortex.panelOpen) {
      ensureWorkspaceAppsRealtime();
    }
  });

  $effect(() => {
    if (cortexSurfaceReady) ensureCortexNotificationsMenuLoaded();
  });

  $effect(() => {
    if (cortexSurfaceReady && !cortex.panelOpen && !chatDockExpanded && !workspaceOverlay.activeWorkspaceAppId) {
      ensureWorkspaceComposerLoaded();
      ensureCortexArchiveBinMenuLoaded();
    }
  });

  $effect(() => {
    if (cortexSurfaceReady && !cortex.panelOpen && !chatDockExpanded && !workspaceOverlay.activeWorkspaceAppId) return;
    resetArchiveDragState();
  });

  $effect(() => {
    if (cortex.panelOpen) ensureThreadStageScreenLoaded();
  });

  $effect(() => {
    if (cortexSurfaceReady && (chatDockExpanded || chatDockForeground)) {
      ensureCortexChatDockLoaded();
    }
  });

  $effect(() => {
    if (!cortex.panelOpen && activeWorkspaceApp) ensureGeneratedAppRendererLoaded();
  });

  $effect(() => {
    if (workspaceOverlay.timelineOpen) ensureCortexTimelineLoaded();
  });

  $effect(() => {
    if (workspaceOverlay.opsOpen) ensureCortexOpsLoaded();
  });

  $effect(() => {
    if (workspaceOverlay.userMenuAnchor) ensureCortexUserMenuLoaded();
  });

  $effect(() => {
    if (workspaceOverlay.workspaceMenuAnchor) ensureCortexWorkspaceMenuLoaded();
  });

  $effect(() => {
    if (workspaceOverlay.pinMenuAnchor) ensureCortexWorkspacePinMenuLoaded();
  });

  const workspaceFieldLayoutOptions = $derived.by(
    (): WorkspaceFieldLayoutOptions => createWorkspaceFieldLayoutOptions(cortex.ideas, directThreadActive),
  );

  const threadPeripherySignals = $derived.by(() => buildThreadPeripherySignals({
    directThreadActive,
    selectedIdea: cortex.selectedIdea,
    ideas: cortex.ideas,
    connections: cortex.connections,
  }));

  function handleKeydown(e: KeyboardEvent) {
    handleWorkspaceKeydown(
      e,
      {
        chatDockExpanded,
        canvasOpen: cortex.canvasOpen,
        activeWorkspaceAppId: workspaceOverlay.activeWorkspaceAppId,
        opsOpen: workspaceOverlay.opsOpen,
        timelineOpen: workspaceOverlay.timelineOpen,
        panelOpen: cortex.panelOpen,
      },
      {
        compactChat: compactWorkspaceChat,
        closeChat: () => (chatDockExpanded = false),
        openChat: () => {
          workspaceOverlay.closeWorkspaceApp();
          chatDockExpanded = true;
        },
        closeCanvas: () => (cortex.canvasOpen = false),
        closeWorkspaceApp: () => workspaceOverlay.closeWorkspaceApp(),
        closeOps: () => workspaceOverlay.closeOps(),
        closeTimeline: () => workspaceOverlay.closeTimeline(),
        closeThread: () => void cortex.selectIdea(null),
        toggleTimeline: () => workspaceOverlay.toggleTimeline(),
        toggleOps: () => workspaceOverlay.toggleOps(),
        setConstellationMode: (active) => (cortex.constellationMode = active),
      },
    );
  }

  function handleKeyup(e: KeyboardEvent) {
    handleWorkspaceKeyup(e, {
      setConstellationMode: (active) => (cortex.constellationMode = active),
    });
  }

  async function maybeSelectIdeaFromUrl() {
    const requestedIdeaId = $page.url.searchParams.get('idea');
    if (!requestedIdeaId) {
      directThreadUrlPending = false;
      return;
    }
    if (requestedIdeaId === lastRequestedIdeaId) {
      if (cortex.selectedIdea?.id === requestedIdeaId) directThreadUrlPending = false;
      return;
    }
    lastRequestedIdeaId = requestedIdeaId;
    if (!cortex.ideas.some((idea) => idea.id === requestedIdeaId)) {
      await cortex.load();
    }
    if (cortex.ideas.some((idea) => idea.id === requestedIdeaId)) {
      await cortex.selectIdea(requestedIdeaId);
    }
    clearRuntimeReadyOnboardingUrl();
    directThreadUrlPending = false;
  }

  function ensureWorkspacePinsWs() {
    if (workspacePinsWsReady) return;
    workspacePinsWsReady = true;
    workspacePins.setupWs();
  }

  let workspaceAppsRealtimeReady = false;
  let presenceRealtimeReady = false;

  function ensureWorkspaceAppsRealtime() {
    if (workspaceAppsRealtimeReady) return;
    workspaceAppsRealtimeReady = true;
    workspaceApps.setup();
  }

  function ensurePresenceRealtime() {
    if (presenceRealtimeReady) return;
    presenceRealtimeReady = true;
    presence.setup();
  }

  function ensureWorkspaceRealtime() {
    ensureWorkspaceAppsRealtime();
    ensurePresenceRealtime();
  }

  $effect(() => {
    if (!cortex.loading) {
      maybeSelectIdeaFromUrl();
    }
  });

  onMount(() => {
    const requestedIdeaId = $page.url.searchParams.get('idea');
    initialDirectThreadIdeaId = requestedIdeaId;
    directThreadUrlPending = Boolean(requestedIdeaId);
    cortex.setupWs();
    if (!requestedIdeaId) {
      ensureWorkspacePinsWs();
      ensureWorkspaceRealtime();
    }
    void (async () => {
      if (requestedIdeaId) {
        lastRequestedIdeaId = requestedIdeaId;
        await cortex.loadDirectThread(requestedIdeaId);
        clearRuntimeReadyOnboardingUrl();
        directThreadUrlPending = false;
      } else {
        await loadWorkspaceSceneSidecars();
        await maybeSelectIdeaFromUrl();
      }
      schedulePostStartupHydration(requestedIdeaId);
    })();
    document.addEventListener('keydown', handleKeydown);
    document.addEventListener('keyup', handleKeyup);
    document.addEventListener('pointerup', handleArchivePointerRelease, { passive: true });
    document.addEventListener('pointercancel', handleArchivePointerRelease, { passive: true });
    window.addEventListener('blur', handleArchivePointerRelease);
  });

  onDestroy(() => {
    if (isRuntimeReadyIntroIdea(cortex.selectedIdea)) {
      void cortex.selectIdea(null);
    }
    threadStage.cleanup();
    lazyComponents.clear();
    cortex.teardownWs();
    workspacePins.teardownWs();
    workspaceApps.teardown();
    chat.teardown();
    notifications.teardown();
    presence.teardown();
    document.removeEventListener('keydown', handleKeydown);
    document.removeEventListener('keyup', handleKeyup);
    document.removeEventListener('pointerup', handleArchivePointerRelease);
    document.removeEventListener('pointercancel', handleArchivePointerRelease);
    window.removeEventListener('blur', handleArchivePointerRelease);
  });
</script>

<div
  class="cortex-page"
  class:panel-open={cortex.panelOpen}
  class:canvas-open={cortex.canvasOpen}
  style={threadWorkspaceStyle}
>
  <div class="cortex-workspace" bind:this={workspaceEl}>
    <CortexLocalPreviewControls
      workspaceContext={workspaceOverlay.composerContext}
      activeWorkspaceAppId={workspaceOverlay.activeWorkspaceAppId}
      onActiveWorkspaceAppIdChange={(appId) => {
        if (appId) {
          workspaceOverlay.openWorkspaceApp(appId);
        } else {
          workspaceOverlay.closeWorkspaceApp();
        }
      }}
    />

    <ConstellationWorkspaceBackdrop
      className={[
        'cortex-workspace-backdrop',
        directThreadActive ? 'is-direct-thread' : '',
      ].filter(Boolean).join(' ')}
    >
          {#snippet canvas()}
            <div class="cortex-main">
              {#if shouldRenderWorkspaceScene && cortex.view === 'list'}
                {#if cortexSurfaceReady && CortexListViewComponent}
                  <CortexListViewComponent />
                {/if}
              {:else if shouldRenderWorkspaceScene && cortex.view !== 'list'}
                {#if WorkspaceSceneComponent}
                  <WorkspaceSceneComponent
                    apps={workspaceApps.visibleApps}
                    pins={workspacePins.visiblePins}
                    activeAppId={workspaceOverlay.activeWorkspaceAppId}
                    onthreadopen={handleThreadOpen}
                    onappopen={handleWorkspaceAppOpen}
                    onappmove={handleMoveWorkspaceAppToPosition}
                    onapparchive={handleArchiveWorkspaceAppFromBin}
                    onpinmenu={handleWorkspacePinMenu}
                    onpinmove={handleMoveWorkspacePinToPosition}
                    onpindelete={handleDeleteWorkspacePinFromBin}
                    onworkspacecontext={handleWorkspaceContext}
                    onworkspacecontextmenu={handleWorkspaceContextMenu}
                    onownastreclick={handleOwnAstreClick}
                    onarchivedragstate={handleArchiveDragState}
                  />
                {/if}
              {/if}

              {#if cortexWorkspaceLoading}
                <div class="loading-overlay" aria-live="polite">
                  <div class="loading-pill">Loading cortex...</div>
                </div>
              {/if}

              {#if workspaceOverlay.timelineOpen && CortexTimelineComponent}
                <CortexTimelineComponent visible={workspaceOverlay.timelineOpen} />
              {/if}

              {#if cortexSurfaceReady && !cortex.panelOpen && !chatDockExpanded && !workspaceOverlay.activeWorkspaceAppId && CortexArchiveBinMenuComponent}
                <div class="workspace-archive-bin-shell" class:dragging={workspaceOverlay.archiveDragActive}>
                  <CortexArchiveBinMenuComponent
                    dragging={workspaceOverlay.archiveDragActive}
                    dropActive={workspaceOverlay.archiveDropActive}
                    onrestore={handleRestoreArchivedThread}
                    onrestoreapp={handleRestoreArchivedApp}
                  />
                </div>
              {/if}
            </div>
          {/snippet}

          {#snippet toolbar()}
            {#if cortexSurfaceReady}
              <div class="workspace-top-tools">
                <ConstellationSegmentedToggle
                  className="workspace-theme-toggle"
                  options={THEME_OPTIONS}
                  activeKey={theme.id}
                  ariaLabel="Workspace theme"
                  onActiveKeyChange={handleThemeChange}
                />

                {#if CortexNotificationsMenuComponent}
                  <CortexNotificationsMenuComponent onSelect={handleNotificationSelect} />
                {/if}
              </div>
            {/if}
          {/snippet}

          {#snippet composer()}
            {#if cortexSurfaceReady && !cortex.panelOpen && !chatDockExpanded && !workspaceOverlay.activeWorkspaceAppId}
              <div
                class="workspace-composer-shell"
                in:fly={{ y: 20, duration: 240 }}
                out:fly={{ y: 14, duration: 180 }}
              >
                {#if WorkspaceComposerComponent}
                  <WorkspaceComposerComponent context={workspaceOverlay.composerContext} onthreadintent={handleWorkspaceThreadIntent} />
                {/if}
              </div>
            {/if}
          {/snippet}

    </ConstellationWorkspaceBackdrop>

    {#if cortex.panelOpen && ThreadStageScreenComponent}
      <ThreadStageScreenComponent
        entering={threadStage.entering}
        ready={threadStage.ready}
        accentColor={threadAmbientTone.color}
        accentRgb={threadAmbientTone.rgb}
        origin={threadStage.origin}
        peripherySignals={threadPeripherySignals}
        browserOpen={threadStage.previewOpen}
        onBrowserOpenChange={(nextOpen: boolean) => (threadStage.previewOpen = nextOpen)}
        dockWidth={threadStage.dockWidth}
        dockMinWidth={360}
        dockMaxWidth={980}
        onDockWidthChange={(nextWidth: number) => (threadStage.dockWidth = nextWidth)}
        ondismiss={handleThreadStageDismiss}
      />
    {/if}

    {#if cortexSurfaceReady}
      <WorkspaceChatDock
        bind:expanded={chatDockExpanded}
        panelOpen={cortex.panelOpen}
        threadStageReady={threadStage.ready}
        topLevelMode={chatTopLevelMode}
        selectedThreadRootId={chatSelectedThreadRootId}
        selectedConversationId={chatSelectedConversationId}
        selectedPreviewMemberId={chatSelectedPreviewMemberId}
        previewMembers={chatPreviewMembers}
        SeamComponent={CortexChatDockSeamComponent}
        onForegroundChange={(foreground: boolean) => (chatDockForeground = foreground)}
        onTopLevelModeChange={handleChatTopLevelModeChange}
        onOpenRoomThread={handleChatOpenRoomThread}
        onCloseRoomThread={handleChatCloseRoomThread}
        onOpenConversation={handleChatOpenConversation}
        onCloseConversation={handleChatCloseConversation}
      />
    {/if}

    {#if !cortex.panelOpen && activeWorkspaceApp && GeneratedAppRendererComponent}
      <button
        type="button"
        class="workspace-app-dismiss-surface"
        tabindex="-1"
        aria-label="Close generated app"
        onclick={() => workspaceOverlay.closeWorkspaceApp()}
      ></button>

      <div
        class="workspace-app-overlay"
        in:fly={{ y: 42, duration: 240 }}
        out:fly={{ y: 28, duration: 160 }}
      >
        <GeneratedAppRendererComponent app={activeWorkspaceApp} onclose={() => workspaceOverlay.closeWorkspaceApp()} />
      </div>
    {/if}

    {#if workspaceOverlay.userMenuAnchor && CortexUserMenuComponent}
      <CortexUserMenuComponent anchor={workspaceOverlay.userMenuAnchor} onclose={() => workspaceOverlay.closeUserMenu()} />
    {/if}

    {#if workspaceOverlay.workspaceMenuAnchor && CortexWorkspaceMenuComponent}
      <CortexWorkspaceMenuComponent
        anchor={workspaceOverlay.workspaceMenuAnchor}
        onnewpin={handleCreateWorkspacePin}
        onclose={() => workspaceOverlay.closeWorkspaceMenu()}
      />
    {/if}

    {#if workspaceOverlay.pinMenuAnchor && CortexWorkspacePinMenuComponent}
      <CortexWorkspacePinMenuComponent
        anchor={workspaceOverlay.pinMenuAnchor}
        saving={workspaceOverlay.pinMenuSaving}
        deleting={workspaceOverlay.pinMenuDeleting}
        onrename={handleRenameWorkspacePin}
        ondelete={handleDeleteWorkspacePin}
        onclose={() => workspaceOverlay.closePinMenu()}
      />
    {/if}

  </div>

  <!-- Ops Console -->
  {#if workspaceOverlay.opsOpen && CortexOpsComponent}
    <CortexOpsComponent visible={workspaceOverlay.opsOpen} onclose={() => workspaceOverlay.closeOps()} />
  {/if}

  <!-- Hint when no ideas -->
  {#if !cortex.loading && cortex.ideas.length === 0}
    <div class="empty-hint">
      <p>Start from the composer to create your first thread.</p>
    </div>
  {/if}
</div>

<style>
  /* Override layout's max-width — cortex needs full viewport */
  :global(.main-content:has(.cortex-page)) {
    max-width: none !important;
    padding: 0 !important;
    overflow: hidden !important;
  }

  .cortex-page {
    --thread-origin-x: 50%;
    --thread-origin-y: 56%;
    --workspace-chrome-control-height: 46px;
    --workspace-system-chrome-backdrop-filter: blur(20px) saturate(1.08);
    --workspace-bottom-surface-idle-height: clamp(142px, 20svh, 230px);
    --workspace-bottom-surface-inset: clamp(8px, 1.4vh, 14px);
    --workspace-chat-dismiss-background: rgba(4, 7, 12, 0.18);
    --workspace-chat-expanded-border: var(--constellation-surface-floating-border);
    --workspace-chat-expanded-background: var(--constellation-surface-floating-background);
    --workspace-chat-expanded-shadow: var(--constellation-surface-floating-shadow);
    --workspace-chat-chrome-border: var(--constellation-surface-panel-separator);
    --workspace-chat-kicker-text: var(--constellation-color-text-primary);
    --workspace-chat-mode-text: var(--constellation-color-text-muted);
    --workspace-chat-button-border: var(--constellation-control-button-secondary-border);
    --workspace-chat-button-background: var(--constellation-control-button-secondary-background);
    --workspace-chat-button-text: var(--constellation-control-button-secondary-text);
    --workspace-chat-button-hover-border: var(--constellation-control-button-secondary-border);
    --workspace-chat-button-hover-background: var(--constellation-control-button-secondary-hover-background);
    --workspace-chat-button-hover-text: var(--constellation-control-button-secondary-hover-text);
    --workspace-chat-mini-background-idle: transparent;
    --workspace-chat-mini-background-hover: var(--constellation-surface-floating-background);
    --workspace-chat-mini-border-idle: transparent;
    --workspace-chat-mini-border-hover: var(--constellation-surface-floating-border);
    --workspace-chat-mini-shadow-idle: none;
    --workspace-chat-mini-shadow-hover: var(--constellation-surface-floating-shadow);
    --workspace-chat-mini-tab-text: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-tab-hover-text: var(--constellation-color-text-secondary);
    --workspace-chat-mini-tab-active-text: var(--constellation-color-text-primary);
    --workspace-chat-mini-tab-background: var(--constellation-control-button-secondary-background);
    --workspace-chat-mini-tab-active-background: var(--constellation-control-toggle-active-background);
    --workspace-chat-mini-tab-border: var(--constellation-control-button-secondary-border);
    --workspace-chat-mini-tab-active-border: var(--constellation-control-surface-border);
    --workspace-chat-mini-input-background-idle: transparent;
    --workspace-chat-mini-input-background-hover: var(--constellation-control-field-background);
    --workspace-chat-mini-input-border-idle: transparent;
    --workspace-chat-mini-input-border-hover: var(--constellation-control-field-border);
    --workspace-chat-mini-input-focus-border: var(--constellation-control-focus-ring);
    --workspace-chat-mini-resize-grip: var(--constellation-color-text-muted);
    --workspace-chat-mini-resize-grip-active: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-text: var(--constellation-color-text-primary);
    --workspace-chat-mini-muted: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-subtle: var(--constellation-color-text-muted);
    --workspace-chat-mini-author: var(--constellation-color-text-secondary);
    --workspace-chat-mini-thread: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-text-shadow: none;
    --cortex-panel-open-overlay-background:
      radial-gradient(
        circle at var(--thread-origin-x) var(--thread-origin-y),
        rgba(4, 7, 12, 0.08) 0%,
        rgba(4, 7, 12, 0.2) 46%,
        rgba(4, 7, 12, 0.32) 100%
      );
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }

  :global(:root[data-color-scheme='light']) .cortex-page {
    --workspace-system-chrome-backdrop-filter: none;
    --workspace-chat-dismiss-background: rgba(230, 238, 244, 0.32);
    --workspace-chat-expanded-border: var(--constellation-surface-floating-border);
    --workspace-chat-expanded-background: var(--constellation-surface-floating-background);
    --workspace-chat-expanded-shadow: var(--constellation-surface-floating-shadow);
    --workspace-chat-chrome-border: var(--constellation-surface-panel-separator);
    --workspace-chat-kicker-text: var(--constellation-color-text-primary);
    --workspace-chat-mode-text: var(--constellation-color-text-muted);
    --workspace-chat-button-border: var(--constellation-control-button-secondary-border);
    --workspace-chat-button-background: var(--constellation-control-button-secondary-background);
    --workspace-chat-button-text: var(--constellation-control-button-secondary-text);
    --workspace-chat-button-hover-border: var(--constellation-control-button-secondary-border);
    --workspace-chat-button-hover-background: var(--constellation-control-button-secondary-hover-background);
    --workspace-chat-button-hover-text: var(--constellation-control-button-secondary-hover-text);
    --workspace-chat-mini-background-idle: transparent;
    --workspace-chat-mini-background-hover: var(--constellation-surface-floating-background);
    --workspace-chat-mini-border-idle: transparent;
    --workspace-chat-mini-border-hover: var(--constellation-surface-floating-border);
    --workspace-chat-mini-shadow-idle: none;
    --workspace-chat-mini-shadow-hover: var(--constellation-surface-floating-shadow);
    --workspace-chat-mini-tab-text: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-tab-hover-text: var(--constellation-color-text-secondary);
    --workspace-chat-mini-tab-active-text: var(--constellation-color-text-primary);
    --workspace-chat-mini-tab-background: var(--constellation-control-button-secondary-background);
    --workspace-chat-mini-tab-active-background: var(--constellation-control-toggle-active-background);
    --workspace-chat-mini-tab-border: var(--constellation-control-button-secondary-border);
    --workspace-chat-mini-tab-active-border: var(--constellation-control-surface-border);
    --workspace-chat-mini-input-background-idle: transparent;
    --workspace-chat-mini-input-background-hover: var(--constellation-control-field-background);
    --workspace-chat-mini-input-border-idle: transparent;
    --workspace-chat-mini-input-border-hover: var(--constellation-control-field-border);
    --workspace-chat-mini-input-focus-border: var(--constellation-control-focus-ring);
    --workspace-chat-mini-resize-grip: var(--constellation-color-text-muted);
    --workspace-chat-mini-resize-grip-active: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-text: var(--constellation-color-text-primary);
    --workspace-chat-mini-muted: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-subtle: var(--constellation-color-text-muted);
    --workspace-chat-mini-author: var(--constellation-color-text-secondary);
    --workspace-chat-mini-thread: var(--constellation-color-text-tertiary);
    --workspace-chat-mini-text-shadow: none;
    --cortex-panel-open-overlay-background:
      radial-gradient(
        circle at var(--thread-origin-x) var(--thread-origin-y),
        rgba(255, 253, 247, 0.05) 0%,
        rgba(236, 245, 247, 0.12) 48%,
        rgba(226, 236, 240, 0.22) 100%
      );
  }

  .cortex-workspace {
    flex: 1;
    position: relative;
    min-height: 0;
    overflow: hidden;
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }

  :global(.constellation-workspace-backdrop.cortex-workspace-backdrop) {
    --constellation-workspace-backdrop-composer-bottom: var(--workspace-bottom-surface-inset);
    --constellation-workspace-backdrop-composer-width: clamp(320px, 34vw, 520px);
    height: 100%;
    min-height: 100%;
  }

  :global(.constellation-workspace-backdrop.cortex-workspace-backdrop.is-direct-thread) {
    --constellation-workspace-theme-deep-field-opacity: 0;
  }

  .workspace-composer-shell {
    width: 100%;
  }

  .workspace-archive-bin-shell {
    position: absolute;
    right: 22px;
    bottom: 16px;
    z-index: 6;
    pointer-events: auto;
  }

  .workspace-archive-bin-shell.dragging {
    z-index: 0;
  }

  .workspace-app-dismiss-surface {
    position: absolute;
    inset: 0;
    z-index: 18;
    border: 0;
    padding: 0;
    margin: 0;
    background: transparent;
    cursor: default;
  }

  .workspace-app-dismiss-surface:focus,
  .workspace-app-dismiss-surface:focus-visible {
    outline: none;
  }

  .workspace-top-tools {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    gap: 12px;
    pointer-events: auto;
  }

  .cortex-page :global(.workspace-theme-toggle) {
    --constellation-control-button-secondary-text: var(--constellation-system-chrome-text);
    --constellation-control-toggle-active-background: var(--constellation-system-chrome-active-background);
    --constellation-control-toggle-active-text: var(--constellation-system-chrome-active-text);
    height: var(--workspace-chrome-control-height);
    box-sizing: border-box;
    padding: 4px;
    border: 1px solid var(--constellation-system-chrome-border);
    background: var(--constellation-system-chrome-background);
    box-shadow: var(--constellation-system-chrome-shadow);
    backdrop-filter: var(--workspace-system-chrome-backdrop-filter);
    -webkit-backdrop-filter: var(--workspace-system-chrome-backdrop-filter);
  }

  .cortex-page :global(.workspace-theme-toggle .constellation-segmented-toggle-thumb) {
    background: var(--constellation-system-chrome-active-background);
    box-shadow: var(--constellation-system-chrome-active-shadow);
  }

  :global(.workspace-theme-toggle .constellation-segmented-toggle-option.has-icon) {
    height: calc(var(--workspace-chrome-control-height) - 10px);
    min-height: 0;
    min-width: 40px;
    padding: 0 11px;
  }

  .workspace-top-tools :global(.constellation-icon-button-md) {
    width: var(--workspace-chrome-control-height);
    height: var(--workspace-chrome-control-height);
    border-radius: 14px;
    border-color: var(--constellation-system-chrome-border);
    background: var(--constellation-system-chrome-background);
    color: var(--constellation-system-chrome-text);
    box-shadow: var(--constellation-system-chrome-shadow);
    backdrop-filter: var(--workspace-system-chrome-backdrop-filter);
    -webkit-backdrop-filter: var(--workspace-system-chrome-backdrop-filter);
  }

  .workspace-top-tools :global(.constellation-icon-button-md:hover:not(:disabled)) {
    border-color: var(--constellation-system-chrome-active-border);
    background: var(--constellation-system-chrome-active-background);
    color: var(--constellation-system-chrome-text-hover);
  }

  .workspace-top-tools :global(.constellation-icon-button-md[aria-pressed='true']) {
    border-color: var(--constellation-system-chrome-active-border);
    background: var(--constellation-system-chrome-active-background);
    color: var(--constellation-system-chrome-active-text);
    box-shadow: var(--constellation-system-chrome-active-shadow);
  }

  .workspace-top-tools :global(.constellation-icon-button-icon) {
    width: 15px;
    height: 15px;
  }

  .workspace-app-overlay {
    position: absolute;
    top: 88px;
    left: 50%;
    bottom: 22px;
    z-index: 29;
    display: flex;
    width: min(1040px, calc(100% - 132px));
    min-width: 0;
    min-height: 0;
    translate: -50% 0;
    justify-content: center;
    pointer-events: auto;
  }

  .workspace-app-overlay > :global(*) {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    overflow: auto;
  }

  .cortex-main {
    position: relative;
    z-index: 1;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    padding: 0;
    transform: scale(1);
    transform-origin: var(--thread-origin-x) var(--thread-origin-y);
    will-change: opacity, transform;
    transition:
      opacity 0.38s cubic-bezier(0.22, 1, 0.36, 1),
      transform 0.48s cubic-bezier(0.22, 1, 0.36, 1);
  }

  .cortex-main::after {
    content: '';
    position: absolute;
    inset: 0;
    z-index: 3;
    pointer-events: none;
    opacity: 0;
    background: var(--cortex-panel-open-overlay-background);
    transition: opacity 0.38s cubic-bezier(0.22, 1, 0.36, 1);
  }

  .cortex-main > :global(.cortex-container) {
    z-index: 1;
  }

  /* When canvas is open: compress galaxy, thread narrows */
  .canvas-open .cortex-main {
    flex: 1;
    min-width: 120px;
  }

  .panel-open .cortex-main {
    opacity: var(--constellation-thread-stage-backdrop-opacity, 0.78);
    transform: scale(var(--constellation-thread-stage-backdrop-scale, 0.985));
  }

  .panel-open .cortex-main::after {
    opacity: 1;
  }

  .panel-open .cortex-main :global(*) {
    animation-play-state: paused !important;
  }

  .loading-overlay {
    position: absolute;
    inset: 0;
    z-index: 4;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }

  .loading-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 38px;
    padding: 0 18px;
    border-radius: 999px;
    border: 1px solid var(--constellation-control-surface-border);
    background: var(--constellation-control-surface-background);
    color: var(--constellation-color-text-muted);
    box-shadow: var(--constellation-control-surface-shadow);
    font-size: 14px;
    letter-spacing: 0.04em;
    backdrop-filter: blur(10px) saturate(1.08);
    -webkit-backdrop-filter: blur(10px) saturate(1.08);
  }

  .empty-hint {
    position: absolute;
    bottom: 112px;
    left: 50%;
    transform: translateX(-50%);
    text-align: center;
    color: color-mix(in srgb, var(--constellation-color-amber) 42%, transparent);
    font-size: 13px;
    font-weight: 300;
    text-transform: uppercase;
    letter-spacing: 1px;
    animation: hint-pulse 4s ease-in-out infinite;
  }

  @keyframes hint-pulse {
    0%, 100% { opacity: 0.5; }
    50% { opacity: 1; }
  }

  @media (max-width: 900px) {
    .cortex-page {
      --workspace-bottom-surface-idle-height: clamp(132px, 20svh, 190px);
      --workspace-bottom-surface-inset: 8px;
    }

    :global(.constellation-workspace-backdrop.cortex-workspace-backdrop) {
      --constellation-workspace-backdrop-composer-bottom: var(--workspace-bottom-surface-inset);
      --constellation-workspace-backdrop-composer-width: clamp(300px, 40vw, 440px);
    }

    .empty-hint {
      bottom: 94px;
    }

    .workspace-app-overlay {
      top: 76px;
      left: 50%;
      bottom: 16px;
      width: calc(100% - 20px);
    }
  }

  @media (max-width: 700px) {
    .cortex-page {
      --workspace-bottom-surface-idle-height: min(150px, 22svh);
      --workspace-bottom-surface-inset: 8px;
    }

    :global(.constellation-workspace-backdrop.cortex-workspace-backdrop) {
      --constellation-workspace-backdrop-composer-width: min(calc(100% - 28px), 360px);
    }
  }

</style>
