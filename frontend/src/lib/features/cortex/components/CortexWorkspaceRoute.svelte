<script lang="ts">
  import { browser } from '$app/environment';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount, onDestroy, type Component } from 'svelte';
  import { fly } from 'svelte/transition';
  import WorkspaceStageShell from '$lib/components/layout/WorkspaceStageShell.svelte';
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
    resolveThreadOriginAccent,
    type WorkspaceFieldLayoutOptions,
  } from '$lib/features/cortex/controllers';
  import type { Idea } from '$lib/types/cortex';
  import { chat } from '$lib/stores/chat.svelte';
  import { notifications } from '$lib/stores/notifications.svelte';
  import { presence } from '$lib/stores/presence.svelte';
  import { THEME_OPTIONS, theme, type ThemeId } from '$lib/stores/theme.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import {
    ConstellationIcon,
    ConstellationIconButton,
    ConstellationSegmentedToggle,
    ConstellationWorkspaceBackdrop,
  } from '$lib/design-system/constellation';
  import type { CortexWorkspacePoint } from '$lib/features/workspace-scene/domain/workspacePoint';
  import { isLocalPreviewMemberId } from '$lib/utils/cortexLocalPreview';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import { workspacePins } from '$lib/stores/workspacePins.svelte';
  import CortexLocalPreviewControls from '$lib/features/cortex/components/LocalPreviewControls.svelte';
  import WorkspacePageModal from '$lib/features/cortex/components/WorkspacePageModal.svelte';
  import WorkspaceChatDock from '$lib/features/cortex/components/chat/WorkspaceChatDock.svelte';
  import type { CortexChatDockTopLevelMode } from '$lib/features/cortex/components/chat/ChatDockSeam.svelte';
  import { api, type ChatConversationSummary } from '$lib/api/client';
  import {
    WORKSPACE_PAGE_MODAL_PARAM,
    WORKSPACE_PAGE_MODAL_SECTIONS,
    buildCortexHrefWithoutWorkspacePage,
    isWorkspacePageModalId,
    type WorkspacePageModalId,
  } from '$lib/features/cortex/domain/workspacePageModal';
  import { decideThreadRouteSelection } from '$lib/features/cortex/domain/threadRouteOpening';
  import {
    buildCortexHrefWithoutThread,
    CORTEX_THREAD_PARAM,
    isThreadRoutePathname,
    threadIdFromUrl,
    threadRoute,
  } from '$lib/features/threads/domain/threadLinks';

  const RUNTIME_READY_ONBOARDING_PARAM = 'runtime-ready';
  const WORKSPACE_APP_QUERY_PARAM = 'app';
  const WORKSPACE_ARRIVAL_DURATION_MS = 1320;
  const RUNTIME_READY_INTRO_DELAY_MS = 1560;
  type RuntimeReadyComposerDraft = {
    id: string;
    text: string;
    origin?: string | null;
    originRef?: string | null;
    displayTitle?: string | null;
    runMetadata?: Record<string, any> | null;
    delayMs?: number;
    typeIntervalMs?: number;
    submitDelayMs?: number;
  };
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
  let workspaceArrivalActive = $state(true);
  let workspaceArrivalSettled = $state(false);
  let runtimeReadyIntroHandled = $state(false);
  let runtimeReadyIntroStarting = $state(false);
  let runtimeReadyIntroTimer: ReturnType<typeof setTimeout> | null = null;
  let runtimeReadyComposerDraft = $state<RuntimeReadyComposerDraft | null>(null);
  let workspacePageComponent = $state<Component | null>(null);
  let workspacePageComponentId = $state<WorkspacePageModalId | null>(null);
  let workspacePageLoading = $state(false);
  let workspacePageLoadToken = 0;
  let lastRequestedIdeaId = $state<string | null>(null);
  let lastSyncedThreadRoute = $state<string | null>(null);
  function requestedThreadIdeaIdFromPage() {
    return threadIdFromUrl($page.url);
  }

  function requestedWorkspaceAppIdFromPage() {
    return ($page.url.searchParams.get(WORKSPACE_APP_QUERY_PARAM) || '').trim() || null;
  }

  function isThreadStageUrl() {
    return isThreadRoutePathname($page.url.pathname) || Boolean($page.url.searchParams.get(CORTEX_THREAD_PARAM));
  }

  let initialDirectThreadIdeaId = $state<string | null>(requestedThreadIdeaIdFromPage());
  let directThreadUrlPending = $state(Boolean(requestedThreadIdeaIdFromPage()));
  let lastAutoOpenedAppId = $state<string | null>(null);
  let lastRouteOpenedWorkspaceAppId = $state<string | null>(null);
  let threadStagePrewarmQueued = false;
  let CortexArchiveBinMenuComponent = $state<typeof import('$lib/features/cortex/components/ArchiveBinMenu.svelte').default | null>(null);
  let WorkspaceSceneComponent = $state<typeof import('$lib/features/workspace-scene/components/WorkspaceScene.svelte').default | null>(null);
  let CortexChatDockSeamComponent = $state<typeof import('$lib/features/cortex/components/chat/ChatDockSeam.svelte').default | null>(null);
  let CortexListViewComponent = $state<typeof import('$lib/features/cortex/components/ListView.svelte').default | null>(null);
  let CortexNotificationsMenuComponent = $state<typeof import('$lib/features/notifications/components/NotificationsMenu.svelte').default | null>(null);
  let ThreadStageScreenComponent = $state<typeof import('$lib/features/threads/components/ThreadStageScreen.svelte').default | null>(null);
  let CortexUserMenuComponent = $state<typeof import('$lib/features/cortex/components/menus/UserMenu.svelte').default | null>(null);
  let CortexWorkspaceMenuComponent = $state<typeof import('$lib/features/cortex/components/menus/WorkspaceMenu.svelte').default | null>(null);
  let CortexWorkspacePinMenuComponent = $state<typeof import('$lib/features/cortex/components/menus/WorkspacePinMenu.svelte').default | null>(null);
  let GeneratedAppRendererComponent = $state<typeof import('$lib/features/workspace-apps/components/GeneratedAppOverlay.svelte').default | null>(null);
  let WorkspaceComposerComponent = $state<typeof import('$lib/features/composer/components/WorkspaceComposer.svelte').default | null>(null);
  const activeWorkspaceApp = $derived(workspaceApps.appById(workspaceOverlay.activeWorkspaceAppId));
  const activeWorkspacePageModalId = $derived.by(() => {
    const value = $page.url.searchParams.get(WORKSPACE_PAGE_MODAL_PARAM);
    return isWorkspacePageModalId(value) ? value : null;
  });
  const activeWorkspacePageModal = $derived(
    activeWorkspacePageModalId ? WORKSPACE_PAGE_MODAL_SECTIONS[activeWorkspacePageModalId] : null,
  );
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
    return (
      idea?.origin === 'onboarding'
      && (
        idea.agent_details?.onboarding?.intro === true
        || idea.origin_ref?.startsWith('runtime-ready-intro:')
      )
    );
  }

  function runtimeReadyIntroIsDeferred() {
    return isRuntimeReadyOnboardingUrl() && !requestedThreadIdeaIdFromPage();
  }

  function runtimeReadyIntroOpenExisting() {
    return $page.url.searchParams.get('open_existing') === '1';
  }

  function clearRuntimeReadyOnboardingUrl(options: { requireIntroIdea?: boolean } = {}) {
    const { requireIntroIdea = true } = options;
    if (!browser || !isRuntimeReadyOnboardingUrl()) return;
    if (requireIntroIdea && !isRuntimeReadyIntroIdea(cortex.selectedIdea)) return;
    const url = new URL(window.location.href);
    url.searchParams.delete(CORTEX_THREAD_PARAM);
    url.searchParams.delete('onboarding');
    url.searchParams.delete('open_existing');
    const nextPath = `${url.pathname}${url.search}${url.hash}`;
    window.history.replaceState(window.history.state, '', nextPath);
  }

  async function startDeferredRuntimeReadyIntro() {
    if (runtimeReadyIntroHandled || runtimeReadyIntroStarting) return;
    runtimeReadyIntroHandled = true;
    runtimeReadyIntroStarting = true;
    try {
      const intro = await api.runtimeReadyIntroDraft();
      if (!intro?.should_play) {
        if (intro?.idea_id && runtimeReadyIntroOpenExisting()) {
          threadStage.setCenteredOrigin('50%', '54%');
          await cortex.loadDirectThread(intro.idea_id);
        }
        clearRuntimeReadyOnboardingUrl({ requireIntroIdea: false });
        return;
      }
      runtimeReadyComposerDraft = {
        id: `${intro.origin_ref}:${Date.now()}`,
        text: intro.prompt,
        origin: intro.origin,
        originRef: intro.origin_ref,
        displayTitle: intro.display_title,
        runMetadata: intro.run_metadata,
        delayMs: 360,
        typeIntervalMs: 18,
        submitDelayMs: 620,
      };
    } catch (err: any) {
      ui.toast(err?.detail || err?.message || 'Illo intro did not start.', 'error');
      clearRuntimeReadyOnboardingUrl({ requireIntroIdea: false });
    } finally {
      runtimeReadyIntroStarting = false;
    }
  }

  function handleRuntimeReadyAutoDraftComplete() {
    runtimeReadyComposerDraft = null;
    clearRuntimeReadyOnboardingUrl({ requireIntroIdea: false });
  }

  async function loadWorkspacePageComponentById(id: WorkspacePageModalId): Promise<Component> {
    switch (id) {
      case 'cycles':
        return (await import('../../../../routes/cycles/+page.svelte')).default;
      case 'skills':
        return (await import('../../../../routes/skills/+page.svelte')).default;
      case 'team':
        return (await import('../../../../routes/team/+page.svelte')).default;
      case 'vault':
        return (await import('../../../../routes/vault/+page.svelte')).default;
      case 'system':
        return (await import('../../../../routes/system/+page.svelte')).default;
    }
  }

  async function ensureWorkspacePageComponent(id: WorkspacePageModalId) {
    if (workspacePageComponentId === id && workspacePageComponent) return;
    const token = ++workspacePageLoadToken;
    workspacePageLoading = true;
    workspacePageComponent = null;
    workspacePageComponentId = id;
    try {
      const component = await loadWorkspacePageComponentById(id);
      if (token !== workspacePageLoadToken) return;
      workspacePageComponent = component;
    } catch (err: any) {
      if (token !== workspacePageLoadToken) return;
      workspacePageComponentId = null;
      ui.toast(err?.detail || err?.message || 'Workspace page did not open.', 'error');
      await closeWorkspacePageModal();
    } finally {
      if (token === workspacePageLoadToken) workspacePageLoading = false;
    }
  }

  async function closeWorkspacePageModal() {
    if (!browser) return;
    await goto(buildCortexHrefWithoutWorkspacePage($page.url.searchParams), {
      keepFocus: true,
      noScroll: true,
    });
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

  $effect(() => {
    if (isThreadStageUrl()) return;
    const requestedAppId = requestedWorkspaceAppIdFromPage();
    if (!requestedAppId) {
      lastRouteOpenedWorkspaceAppId = null;
      return;
    }
    const app = workspaceApps.appById(requestedAppId);
    if (!app) {
      if (!workspaceApps.loaded) void workspaceApps.load({ silent: true });
      return;
    }
    if (requestedAppId === lastRouteOpenedWorkspaceAppId && workspaceOverlay.activeWorkspaceAppId === requestedAppId) return;
    lastRouteOpenedWorkspaceAppId = requestedAppId;
    void openWorkspaceAppFromRoute(requestedAppId);
  });

  $effect(() => {
    const modalId = activeWorkspacePageModalId;
    if (!modalId) {
      workspacePageLoadToken += 1;
      workspacePageComponent = null;
      workspacePageComponentId = null;
      workspacePageLoading = false;
      return;
    }

    workspaceOverlay.closeWorkspaceAndPinMenus();
    workspaceOverlay.closeWorkspaceApp();
    void ensureWorkspacePageComponent(modalId);
  });

  async function handleThreadOpen(origin: { x: number; y: number; id: string }) {
    workspaceOverlay.closeWorkspaceApp();
    workspaceOverlay.closeWorkspaceAndPinMenus();
    threadStage.setOriginFromClient(workspaceEl, origin.x, origin.y);
    await openDirectThreadAndSyncUrl(origin.id);
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

  async function openWorkspaceAppFromRoute(appId: string) {
    workspaceOverlay.openWorkspaceAppOverlay(appId);
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
    const shouldRefreshWorkspaceSceneSidecars = directThreadActive || !workspaceSceneSidecarsReady;
    cortex.selectIdea(null);
    initialDirectThreadIdeaId = null;
    lastSyncedThreadRoute = null;
    if (browser && isThreadStageUrl()) {
      void goto('/cortex', {
        replaceState: true,
        keepFocus: true,
        noScroll: true,
      });
    }
    ensureWorkspaceRealtime();
    void cortex.loadTeamMembers();
    if (shouldRefreshWorkspaceSceneSidecars) {
      void loadWorkspaceSceneSidecars();
    }
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
    if (nextMode !== 'dms') {
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

  function syncChatSelectionFromConversation(conversation: ChatConversationSummary | null) {
    chatSelectedPreviewMemberId = null;
    chatSelectedThreadRootId = null;

    if (conversation?.type === 'dm') {
      chatTopLevelMode = 'dms';
      chatSelectedConversationId = conversation.id;
      return;
    }

    chatTopLevelMode = 'room';
    chatSelectedConversationId = null;
  }

  async function openChatToMostRecentConversation() {
    workspaceOverlay.closeWorkspaceApp();
    chatDockExpanded = true;
    try {
      syncChatSelectionFromConversation(await chat.openMostRecentConversation());
    } catch {
      // The chat store records and surfaces bootstrap/load failures.
    }
  }

  function compactWorkspaceChat() {
    chatDockExpanded = false;
    chatDockForeground = false;
  }

  async function toggleWorkspaceChat() {
    if (chatDockExpanded) {
      compactWorkspaceChat();
      return;
    }

    await openChatToMostRecentConversation();
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
    threadStage.syncPanelOpen(cortex.panelOpen && Boolean(ThreadStageScreenComponent));
  });

  const threadWorkspaceStyle = $derived.by(() => threadStage.workspaceStyle);

  const threadOriginAccent = $derived.by(() =>
    resolveThreadOriginAccent({
      selectedIdea: cortex.selectedIdea,
      currentUserId: auth.user?.id,
      currentUserColor: auth.user?.color,
      teamMembers: cortex.teamMembers,
    }),
  );
  const threadAmbientTone = $derived.by(() =>
    buildThreadAmbientTone({
      status: cortex.selectedIdea?.status ?? 'idle',
      originAccent: threadOriginAccent,
    }),
  );
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
  const workspaceVisualReady = $derived(
    shouldRenderWorkspaceScene
      ? (cortex.view === 'list' ? Boolean(CortexListViewComponent) : Boolean(WorkspaceSceneComponent))
      : cortexSurfaceReady,
  );
  const workspaceArrivalReady = $derived(cortexSurfaceReady && workspaceVisualReady);

  function sameCortexHref(targetHref: string) {
    return `${$page.url.pathname}${$page.url.search}` === targetHref;
  }

  async function replaceCortexHref(targetHref: string) {
    if (sameCortexHref(targetHref)) return;
    await goto(targetHref, { keepFocus: true, noScroll: true, replaceState: true });
  }

  function cleanupUnresolvedThreadUrl(requestedIdeaId: string | null | undefined) {
    if (!browser || !requestedIdeaId || requestedThreadIdeaIdFromPage() !== requestedIdeaId) return;
    lastSyncedThreadRoute = null;
    initialDirectThreadIdeaId = null;
    void replaceCortexHref(buildCortexHrefWithoutThread($page.url.searchParams));
  }

  function syncThreadUrlToStage() {
    if (!browser || directThreadUrlPending) return;

    const urlIdeaId = requestedThreadIdeaIdFromPage();
    const selectedIdeaId = cortex.panelOpen ? cortex.selectedIdeaId : null;
    if (selectedIdeaId) {
      if (urlIdeaId && urlIdeaId !== selectedIdeaId) return;
      lastSyncedThreadRoute = threadRoute(selectedIdeaId);
      return;
    }

    if (urlIdeaId && lastSyncedThreadRoute) {
      lastSyncedThreadRoute = null;
      initialDirectThreadIdeaId = null;
      void replaceCortexHref(buildCortexHrefWithoutThread($page.url.searchParams));
    }
  }

  $effect(() => {
    syncThreadUrlToStage();
  });

  $effect(() => {
    if (!browser || workspaceArrivalSettled || !workspaceArrivalReady) return;
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      workspaceArrivalActive = false;
      workspaceArrivalSettled = true;
      return;
    }

    workspaceArrivalActive = true;
    const timeout = window.setTimeout(() => {
      workspaceArrivalActive = false;
      workspaceArrivalSettled = true;
    }, WORKSPACE_ARRIVAL_DURATION_MS);

    return () => window.clearTimeout(timeout);
  });

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
  const ensureThreadStageScreenLoaded = createLazyComponentLoader(
    'thread-stage',
    () => ThreadStageScreenComponent,
    (component) => (ThreadStageScreenComponent = component),
    () => import('$lib/features/threads/components/ThreadStageScreen.svelte'),
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
    if (!cortexSurfaceReady || ThreadStageScreenComponent || threadStagePrewarmQueued) return;
    threadStagePrewarmQueued = true;
    runWhenBrowserIdle(() => ensureThreadStageScreenLoaded(), 260, 1600);
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
    if (!chatDockExpanded && chatDockForeground) chatDockForeground = false;
  });

  $effect(() => {
    if (!cortex.panelOpen && activeWorkspaceApp) ensureGeneratedAppRendererLoaded();
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
    currentUserId: auth.user?.id,
    currentUserColor: auth.user?.color,
    teamMembers: cortex.teamMembers,
  }));

  function handleKeydown(e: KeyboardEvent) {
    if (activeWorkspacePageModalId && e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      void closeWorkspacePageModal();
      return;
    }

    handleWorkspaceKeydown(
      e,
      {
        chatDockExpanded,
        canvasOpen: cortex.canvasOpen,
        activeWorkspaceAppId: workspaceOverlay.activeWorkspaceAppId,
        panelOpen: cortex.panelOpen,
      },
      {
        compactChat: compactWorkspaceChat,
        closeChat: () => (chatDockExpanded = false),
        openChat: () => {
          void openChatToMostRecentConversation();
        },
        closeCanvas: () => (cortex.canvasOpen = false),
        closeWorkspaceApp: () => workspaceOverlay.closeWorkspaceApp(),
        closeThread: () => void cortex.selectIdea(null),
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
    const decision = decideThreadRouteSelection({
      requestedIdeaId: requestedThreadIdeaIdFromPage(),
      selectedIdeaId: cortex.selectedIdeaId,
      panelOpen: cortex.panelOpen,
      lastRequestedIdeaId,
    });
    if (decision.action === 'idle') {
      lastRequestedIdeaId = null;
      directThreadUrlPending = false;
      return;
    }
    if (decision.action === 'already-open') {
      lastRequestedIdeaId = decision.ideaId;
      directThreadUrlPending = false;
      return;
    }
    if (decision.action === 'skip-repeat') return;

    lastRequestedIdeaId = decision.ideaId;
    await openDirectThreadAndSyncUrl(decision.ideaId);
    clearRuntimeReadyOnboardingUrl();
  }

  async function openDirectThreadAndSyncUrl(ideaId: string): Promise<boolean> {
    directThreadUrlPending = true;
    const opened = await cortex.loadDirectThread(ideaId);
    if (opened) {
      const navigationStarted = syncCanonicalThreadUrl(ideaId);
      if (!navigationStarted) directThreadUrlPending = false;
      return true;
    }

    directThreadUrlPending = false;
    cleanupUnresolvedThreadUrl(ideaId);
    return false;
  }

  function syncCanonicalThreadUrl(ideaId: string | null | undefined): boolean {
    if (!browser || !ideaId) return false;
    const nextRoute = threadRoute(ideaId);
    const current = `${$page.url.pathname}${$page.url.search}${$page.url.hash}`;
    lastSyncedThreadRoute = nextRoute;
    if (current === nextRoute) return false;
    const replaceState = $page.url.pathname.startsWith('/threads/') || Boolean($page.url.searchParams.get('idea'));
    directThreadUrlPending = true;
    void goto(nextRoute, {
      replaceState,
      keepFocus: true,
      noScroll: true,
    }).catch(() => {
      if (lastSyncedThreadRoute === nextRoute) {
        lastSyncedThreadRoute = null;
        directThreadUrlPending = false;
      }
    });
    return true;
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

  $effect(() => {
    if (
      !browser
      || requestedThreadIdeaIdFromPage()
      || directThreadUrlPending
      || !cortex.panelOpen
      || !lastSyncedThreadRoute
    ) {
      return;
    }
    lastSyncedThreadRoute = null;
    initialDirectThreadIdeaId = null;
    void cortex.selectIdea(null);
    ensureWorkspaceRealtime();
  });

  $effect(() => {
    if (!cortex.panelOpen || !cortex.selectedIdea?.id) return;
    syncCanonicalThreadUrl(cortex.selectedIdea.id);
  });

  $effect(() => {
    if (
      !browser
      || runtimeReadyIntroHandled
      || runtimeReadyIntroStarting
      || !runtimeReadyIntroIsDeferred()
      || cortexWorkspaceLoading
      || !workspaceArrivalReady
      || !WorkspaceComposerComponent
      || cortex.panelOpen
      || runtimeReadyComposerDraft
    ) {
      return;
    }

    const timeout = window.setTimeout(() => {
      runtimeReadyIntroTimer = null;
      void startDeferredRuntimeReadyIntro();
    }, RUNTIME_READY_INTRO_DELAY_MS);
    runtimeReadyIntroTimer = timeout;

    return () => {
      if (runtimeReadyIntroTimer === timeout) runtimeReadyIntroTimer = null;
      window.clearTimeout(timeout);
    };
  });

  onMount(() => {
    const requestedIdeaId = requestedThreadIdeaIdFromPage();
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
        await openDirectThreadAndSyncUrl(requestedIdeaId);
        clearRuntimeReadyOnboardingUrl();
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
    if (runtimeReadyIntroTimer) {
      clearTimeout(runtimeReadyIntroTimer);
      runtimeReadyIntroTimer = null;
    }
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
  class:workspace-page-open={Boolean(activeWorkspacePageModal)}
  class:is-arriving={workspaceArrivalActive}
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
      composerClassName="workspace-composer-slot"
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

            </div>
          {/snippet}

          {#snippet composer()}
            {#if cortexSurfaceReady && !cortex.panelOpen && !chatDockExpanded && !workspaceOverlay.activeWorkspaceAppId}
              <div
                class="workspace-composer-shell"
                in:fly={{ y: 20, duration: 240 }}
                out:fly={{ y: 14, duration: 180 }}
              >
                {#if WorkspaceComposerComponent}
                  <WorkspaceComposerComponent
                    context={workspaceOverlay.composerContext}
                    autoDraft={runtimeReadyComposerDraft}
                    onthreadintent={handleWorkspaceThreadIntent}
                    onAutoDraftComplete={handleRuntimeReadyAutoDraftComplete}
                  />
                {/if}
              </div>
            {/if}
          {/snippet}

          {#snippet overlays()}
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
          {/snippet}

    </ConstellationWorkspaceBackdrop>

    {#if cortexSurfaceReady}
      <div class="workspace-top-tools-layer">
        <div class="workspace-top-tools">
          <ConstellationSegmentedToggle
            className="workspace-theme-toggle"
            options={THEME_OPTIONS}
            activeKey={theme.id}
            ariaLabel="Workspace theme"
            onActiveKeyChange={handleThemeChange}
          />

          <div class="workspace-chat-trigger-shell">
            <ConstellationIconButton
              label={chatDockExpanded ? 'Close chat' : 'Open chat'}
              title={chatDockExpanded ? 'Close chat' : 'Open chat'}
              size="md"
              variant="secondary"
              className="workspace-chat-trigger"
              pressed={chatDockExpanded}
              onclick={toggleWorkspaceChat}
            >
              <ConstellationIcon name="chat" size={16} stroke={1.85} />
            </ConstellationIconButton>

            {#if notifications.summary.chat_unread_total > 0}
              <span class="workspace-chat-trigger-badge">
                {notifications.summary.chat_unread_total > 9 ? '9+' : notifications.summary.chat_unread_total}
              </span>
            {/if}
          </div>

          {#if CortexNotificationsMenuComponent}
            <CortexNotificationsMenuComponent onSelect={handleNotificationSelect} />
          {/if}
        </div>
      </div>
    {/if}

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

    {#if cortexSurfaceReady && chatDockExpanded}
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
        onExpandChat={openChatToMostRecentConversation}
        onTopLevelModeChange={handleChatTopLevelModeChange}
        onOpenRoomThread={handleChatOpenRoomThread}
        onCloseRoomThread={handleChatCloseRoomThread}
        onOpenConversation={handleChatOpenConversation}
        onCloseConversation={handleChatCloseConversation}
      />
    {/if}

    {#if !cortex.panelOpen && activeWorkspaceApp && GeneratedAppRendererComponent}
      <WorkspaceStageShell
        className="workspace-app-stage"
        frameClassName="workspace-app-stage-frame"
        zIndex={29}
        dismissLabel="Close generated app"
        ondismiss={() => workspaceOverlay.closeWorkspaceApp()}
      >
        <div
          class="workspace-app-overlay"
          in:fly={{ y: 42, duration: 240 }}
          out:fly={{ y: 28, duration: 160 }}
        >
          <GeneratedAppRendererComponent
            app={activeWorkspaceApp}
            surface="stage"
            onclose={() => workspaceOverlay.closeWorkspaceApp()}
          />
        </div>
      </WorkspaceStageShell>
    {/if}

    {#if activeWorkspacePageModal}
      <WorkspacePageModal
        section={activeWorkspacePageModal}
        PageComponent={workspacePageComponent}
        loading={workspacePageLoading}
        onclose={closeWorkspacePageModal}
      />
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
    --workspace-top-tools-inset: 16px;
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
    --workspace-chat-button-hover-border: var(--constellation-control-button-secondary-border-hover);
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
    --workspace-chat-button-hover-border: var(--constellation-control-button-secondary-border-hover);
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
    --constellation-workspace-theme-deep-field-opacity: 0.82;
  }

  .workspace-composer-shell {
    width: 100%;
  }

  .workspace-archive-bin-shell {
    position: absolute;
    right: 22px;
    bottom: 16px;
    z-index: 1;
    pointer-events: auto;
  }

  .workspace-archive-bin-shell.dragging {
    z-index: 0;
  }

  .workspace-top-tools {
    display: inline-flex;
    align-items: center;
    justify-content: flex-end;
    gap: 12px;
    pointer-events: auto;
  }

  .workspace-chat-trigger-shell {
    position: relative;
    flex: 0 0 auto;
  }

  .workspace-chat-trigger-badge {
    position: absolute;
    top: -5px;
    right: -4px;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background: linear-gradient(180deg, rgba(94, 169, 255, 0.96), rgba(54, 114, 222, 0.94));
    color: rgba(247, 251, 255, 0.98);
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
    box-shadow: 0 10px 22px rgba(24, 72, 151, 0.32);
    pointer-events: none;
  }

  .workspace-top-tools-layer {
    position: absolute;
    top: var(--workspace-top-tools-inset);
    right: var(--workspace-top-tools-inset);
    z-index: 118;
    display: flex;
    justify-content: flex-end;
    pointer-events: none;
  }

  .cortex-page.is-arriving .workspace-top-tools-layer {
    animation: cortex-toolbar-arrive 640ms cubic-bezier(0.22, 1, 0.36, 1) 240ms both;
  }

  .cortex-page.is-arriving .workspace-archive-bin-shell {
    animation: cortex-edge-bin-arrive 640ms cubic-bezier(0.22, 1, 0.36, 1) 420ms both;
  }

  .cortex-page.is-arriving :global(.workspace-composer-slot) {
    animation: cortex-composer-arrive 680ms cubic-bezier(0.22, 1, 0.36, 1) 360ms both;
  }

  .cortex-page.is-arriving .cortex-main :global(.constellation-astre-own) {
    animation: cortex-own-astre-arrive 780ms cubic-bezier(0.16, 1, 0.3, 1) 120ms both;
  }

  .cortex-page.is-arriving .cortex-main :global(.constellation-astre:not(.constellation-astre-own)) {
    animation: cortex-field-astre-arrive 640ms cubic-bezier(0.22, 1, 0.36, 1) 260ms both;
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
    display: flex;
    width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
  }

  .workspace-app-overlay > :global(*) {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
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
  }

  .cortex-main::after {
    content: '';
    position: absolute;
    inset: 0;
    z-index: 3;
    pointer-events: none;
    opacity: 0;
    background: transparent;
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
    opacity: 1;
    transform: none;
  }

  .panel-open .cortex-main::after {
    opacity: 0;
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

  @keyframes cortex-own-astre-arrive {
    0% {
      opacity: 0;
      filter: blur(12px) saturate(1.28) brightness(1.14);
      transform: translate(-50%, -50%) scale(0.22);
    }

    62% {
      opacity: 1;
      filter: blur(0) saturate(1.16) brightness(1.08);
      transform: translate(-50%, -50%) scale(1.08);
    }

    100% {
      opacity: 1;
      filter: none;
      transform: translate(-50%, -50%) scale(var(--astre-scale));
    }
  }

  @keyframes cortex-field-astre-arrive {
    0% {
      opacity: 0;
      filter: blur(5px) saturate(1.16);
      transform: translate(-50%, -50%) scale(0.72);
    }

    100% {
      opacity: 1;
      filter: none;
      transform: translate(-50%, -50%) scale(var(--astre-scale));
    }
  }

  @keyframes cortex-toolbar-arrive {
    0% {
      opacity: 0;
      filter: blur(3px);
      transform: translate3d(34px, -2px, 0);
    }

    100% {
      opacity: 1;
      filter: none;
      transform: translate3d(0, 0, 0);
    }
  }

  @keyframes cortex-edge-bin-arrive {
    0% {
      opacity: 0;
      filter: blur(3px);
      transform: translate3d(30px, 0, 0) scale(0.985);
    }

    100% {
      opacity: 1;
      filter: none;
      transform: translate3d(0, 0, 0) scale(1);
    }
  }

  @keyframes cortex-composer-arrive {
    0% {
      opacity: 0;
      filter: blur(3px);
      transform: translateX(-50%) translateY(30px) scale(0.985);
    }

    100% {
      opacity: 1;
      filter: none;
      transform: translateX(-50%) translateY(0) scale(1);
    }
  }

  @media (max-width: 900px) {
    .cortex-page {
      --workspace-bottom-surface-idle-height: clamp(132px, 20svh, 190px);
      --workspace-bottom-surface-inset: 8px;
      --workspace-top-tools-inset: 12px;
    }

    :global(.constellation-workspace-backdrop.cortex-workspace-backdrop) {
      --constellation-workspace-backdrop-composer-bottom: var(--workspace-bottom-surface-inset);
      --constellation-workspace-backdrop-composer-width: clamp(300px, 40vw, 440px);
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

  @media (prefers-reduced-motion: reduce) {
    .cortex-page.is-arriving .workspace-top-tools-layer,
    .cortex-page.is-arriving .workspace-archive-bin-shell,
    .cortex-page.is-arriving :global(.workspace-composer-slot),
    .cortex-page.is-arriving .cortex-main :global(.constellation-astre) {
      animation: none !important;
    }
  }

</style>
