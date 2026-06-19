<script lang="ts">
  import { page } from '$app/stores';
  import { ConstellationComposerOrb, ConstellationIcon } from '$lib/components/constellation';
  import { auth } from '$lib/stores/auth.svelte';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { theme } from '$lib/stores/theme.svelte';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import {
    deriveCodeReviewFilesFromRuns,
    findActiveFastRun,
    hasLiveFastReply as streamHasLiveFastReply,
    isActiveRun,
    isFastRun,
    type CodeReviewFile,
  } from '$lib/utils/cortexRunPresentation';
  import {
    buildProjectContextMessageAttachment,
    extractIdeaProjectContext,
    type ProjectContextPickerState,
  } from '$lib/utils/projectContext';
  import { ATTACHMENT_INPUT_ACCEPT } from '$lib/utils/attachmentPreview';
  import {
    getWorkspaceComposerDropFiles,
    getWorkspaceComposerInputFiles,
    getWorkspaceComposerPasteFiles,
    getWorkspaceComposerUploadFailureMessage,
    preventWorkspaceComposerDefaultDrag,
    resetWorkspaceComposerFileInput,
    uploadWorkspaceComposerFiles,
  } from '$lib/features/composer/controllers/attachmentController';
  import {
    generateTitle,
    listIdeaProjectContext,
    uploadFile,
  } from '$lib/features/threads/api/threadApi';
  import {
    activateThreadSidePanelTab,
    activeThreadSidePanelTab,
    addBrowserThreadSidePanelTab,
    buildThreadSidePanelAddMenuItems,
    closeThreadSidePanelTab,
    createDefaultThreadSidePanelTabs,
    isThreadSidePanelSingletonKind,
    openAppThreadSidePanelTab,
    openFilePreviewThreadSidePanelTab,
    openSingletonThreadSidePanelTab,
    type ThreadSidePanelTabState,
    type ThreadStageRightDockAddMenuItem,
    type ThreadStageRightDockSingletonKind,
    type ThreadStageRightDockTab,
  } from '$lib/features/threads/controllers/threadSidePanelController';
  import { threadStreamController } from '$lib/features/threads/controllers/threadStreamController';
  import { decideThreadArtifactDeepLink } from '$lib/features/threads/domain/threadArtifactDeepLink';
  import { threadUrl } from '$lib/features/threads/domain/threadLinks';
  import {
    findSlashCommandToken,
    replaceSlashCommandToken,
    type SlashCommandToken,
  } from '$lib/utils/slashCommand';
  import { hasSkillMention } from '$lib/utils/skillMention';
  import {
    CONVERSATION_SCROLL_BOTTOM_THRESHOLD,
    conversationIsNearBottom,
    scrollConversationToBottom,
    shouldShowConversationScrollCue,
  } from '$lib/components/chat/conversationScroll';
  import { onDestroy, onMount, tick } from 'svelte';

  import BrowserThoughtPanel from '$lib/features/browser-sessions/components/BrowserThoughtPanel.svelte';
  import ThreadCyclesPane from '$lib/features/cycles/components/ThreadCyclesPane.svelte';
  import ProjectContextPicker from '$lib/features/composer/components/ProjectContextPicker.svelte';
  import WorkspaceVoiceRecording from '$lib/features/composer/components/WorkspaceVoiceRecording.svelte';
  import SkillMentionOverlay from '$lib/features/composer/components/SkillMentionOverlay.svelte';
  import SlashAutocomplete from '$lib/features/composer/components/SlashAutocomplete.svelte';
  import { WorkspaceVoiceDictationController } from '$lib/features/composer/controllers/workspaceVoiceDictation.svelte.ts';
  import { resizeComposerTextareaToContent } from '$lib/features/composer/domain/composerTextareaSizing';
  import ThreadAttachmentPreviewPane from '$lib/features/threads/components/ThreadAttachmentPreviewPane.svelte';
  import ThreadCodeReviewPane from '$lib/features/threads/components/ThreadCodeReviewPane.svelte';
  import ProjectDraftStatePanel from '$lib/features/threads/components/ProjectDraftStatePanel.svelte';
  import ThreadProjectFilePreviewPane from '$lib/features/threads/components/ThreadProjectFilePreviewPane.svelte';
  import ThreadDiscussionPane from '$lib/features/threads/components/ThreadDiscussionPane.svelte';
  import ThreadStageShell, { type ThreadPeripherySignal } from '$lib/features/threads/components/ThreadStageShell.svelte';
  import ThreadUtilityContent from '$lib/features/threads/components/ThreadUtilityContent.svelte';
  import WorkspaceComposerAdapter from '$lib/features/composer/components/WorkspaceComposerAdapter.svelte';
  import ThreadAppsPane from '$lib/features/workspace-apps/components/ThreadAppsPane.svelte';
  import VaultPage from '../../../../routes/vault/+page.svelte';
  import ThreadTranscript from './ThreadTranscript.svelte';
  import ThreadStageRightDock from './ThreadStageRightDock.svelte';
  import {
    applyRunSetting,
    buildRunSettingsGroups,
    STEERING_INTENT_OPTIONS,
    type ActiveRunMessageIntent,
  } from '$lib/features/composer/domain/runSettings';
  import {
    accentTone,
    buildThreadTranscriptItems,
    elapsedLabel,
    normalizeHexColor,
    resolveIdeaAccent,
    timeAgo,
    visibleThreadStreamItems,
  } from '$lib/features/threads/domain/threadStreamAdapter';
  import type {
    CortexThreadStageFileAttachment,
    CortexThreadStageHeaderStatusState,
    CortexThreadStageImageAttachment,
    CortexThreadStageTranscriptItem,
  } from '$lib/features/threads/domain/threadTranscriptAdapter';

  let {
    entering = false,
    ready = false,
    accentColor = '#57CFA0',
    accentRgb,
    origin = { x: '50%', y: '56%' },
    peripherySignals = [],
    browserOpen = false,
    dockWidth = 560,
    dockMinWidth = 360,
    dockMaxWidth = 980,
    onBrowserOpenChange,
    onDockWidthChange,
    ondismiss,
  }: {
    entering?: boolean;
    ready?: boolean;
    accentColor?: string;
    accentRgb?: string;
    origin?: { x: number | string; y: number | string } | null;
    peripherySignals?: ThreadPeripherySignal[];
    browserOpen?: boolean;
    dockWidth?: number;
    dockMinWidth?: number;
    dockMaxWidth?: number;
    onBrowserOpenChange?: (nextOpen: boolean) => void;
    onDockWidthChange?: (nextWidth: number) => void;
    ondismiss?: () => void;
  } = $props();

  let inputValue = $state('');
  let sending = $state(false);
  let slashRef: SlashAutocomplete | undefined = $state();
  let slashToken: SlashCommandToken | null = $state(null);
  let latestRun = $state<any>(null);
  let runInfo = $state<any>(null);
  let activeSidePanelTabId = $state<string | null>('activity');
  let sidePanelTabs = $state<ThreadStageRightDockTab[]>(createDefaultThreadSidePanelTabs());
  let nextBrowserTabIndex = $state(1);
  let dockPreviewAttachment = $state<CortexThreadStageImageAttachment | CortexThreadStageFileAttachment | null>(null);
  let teamMembers = $state<any[]>([]);
  let teamMembersLoading = false;
  let mentionDropdownVisible = $state(false);
  let mentionMatches = $state<any[]>([]);
  let mentionSelectedIndex = $state(0);
  let pendingAttachments = $state<any[]>([]);
  let fileInputEl: HTMLInputElement | undefined = $state();
  let textareaEl: HTMLTextAreaElement | undefined = $state();
  let textareaScrollTop = $state(0);
  let transcriptEl: HTMLDivElement | undefined = $state();
  let threadDragOver = $state(false);
  let userScrolledUp = $state(false);
  let programmaticScroll = false;
  let showTranscriptScrollCue = $state(false);
  let transcriptScrollFrame: number | null = null;
  let projectContextError = $state('');
  let ideaProjectContextAttachments = $state<any[]>([]);
  let ideaProjectContextLoadedForIdeaId: string | null = null;
  let ideaProjectContextLoadingForIdeaId: string | null = null;
  let pendingProjectContextState = $state<ProjectContextPickerState>({
    snapshot: null,
    valid: true,
    error: null,
    resourceCount: 0,
  });
  let activeRunMessageIntent = $state<ActiveRunMessageIntent>('steer');
  let activeRunIntentTargetId = $state<string | null>(null);
  let threadStagePanelEl: HTMLElement | undefined = $state();
  let threadStageContentWidth = $state(0);
  let threadStageGutterPx = $state(24);
  let titleGenerating = $state(false);
  let threadArchiving = $state(false);
  let threadLinkCopying = $state(false);
  let lastAutoOpenedThreadAppId = $state<string | null>(null);
  let requestedThreadAppLoadRequestedFor = $state<string | null>(null);

  const THREAD_STAGE_MIN_THREAD_WIDTH = 380;
  const THREAD_STAGE_DEFAULT_GUTTER = 24;
  const THREAD_TITLE_SOURCE_ITEM_LIMIT = 8;
  const THREAD_TITLE_SOURCE_ITEM_CHARS = 420;
  const STATUS_LABELS: Record<string, string> = {
    idle: 'Idle',
    working: 'Working',
    done: 'Unread',
  };

  let idea = $derived(cortex.selectedIdea);

  const statusLabel = $derived(
    STATUS_LABELS[idea?.status ?? 'idle'] ?? (idea?.status ? idea.status.replaceAll('_', ' ') : 'Idle'),
  );
  const headerStatusState = $derived.by((): CortexThreadStageHeaderStatusState => {
    if (runInfo || idea?.status === 'working') return 'working';
    if (idea?.status === 'done') return 'unread';
    return 'idle';
  });
  const dockLayoutMaxWidth = $derived.by(() => {
    if (!browserOpen || threadStageContentWidth <= 0) return dockMaxWidth;

    const availableDockWidth = Math.floor(
      threadStageContentWidth - THREAD_STAGE_MIN_THREAD_WIDTH - threadStageGutterPx,
    );

    return Math.max(dockMinWidth, Math.min(dockMaxWidth, availableDockWidth));
  });
  const resolvedDockWidth = $derived(clampNumber(dockWidth, dockMinWidth, dockLayoutMaxWidth));
  const panelStyle = $derived(
    [
      `--thread-stage-dock-width:${resolvedDockWidth}px`,
      `--thread-stage-thread-min:${THREAD_STAGE_MIN_THREAD_WIDTH}px`,
    ].join(';'),
  );
  const activeSidePanelTab = $derived(activeThreadSidePanelTab(sidePanelTabs, activeSidePanelTabId));
  const projectDraftRunId = $derived.by(() => {
    const run = runInfo ?? latestRun;
    const id = run?.run_id ?? run?.id ?? null;
    return id === '' ? null : id;
  });
  const activeFilePreviewTab = $derived(activeSidePanelTab?.kind === 'file-preview' ? activeSidePanelTab : null);
  const activeFilePreviewPath = $derived(activeFilePreviewTab?.filePath ?? '');
  const activeFilePreviewRunId = $derived(activeFilePreviewTab?.runId ?? null);
  const threadArtifactApps = $derived.by(() => workspaceApps.threadApps(idea?.id ?? null));
  const requestedThreadAppId = $derived(
    $page.url.searchParams.get('app') || $page.url.searchParams.get('artifact_app'),
  );
  const selectedThreadApp = $derived(
    activeSidePanelTab?.kind === 'app' && activeSidePanelTab.appId
      ? threadArtifactApps.find((app) => app.id === activeSidePanelTab.appId) ?? null
      : null,
  );
  const sidePanelAddMenuItems = $derived.by(() =>
    buildThreadSidePanelAddMenuItems(sidePanelTabs, threadArtifactApps),
  );
  const activeVaultSecretPrompt = $derived(
    String(cortex.vaultSecretPrompt?.idea_id ?? '') === String(idea?.id ?? '') ? cortex.vaultSecretPrompt : null,
  );
  const activeVaultAgentGrantPrompt = $derived(
    String(cortex.vaultAgentGrantPrompt?.idea_id ?? '') === String(idea?.id ?? '') ? cortex.vaultAgentGrantPrompt : null,
  );
  const codeReviewFiles = $derived.by(() =>
    deriveCodeReviewFilesFromRuns(
      visibleStreamItems.filter((item: any) => item?.type === 'run'),
    ),
  );
  const codeReviewSignature = $derived.by(() => codeReviewFiles.map((file) => file.path).join('|'));
  const voiceDictation = new WorkspaceVoiceDictationController({
    getDraft: () => inputValue,
    setDraft: (next) => {
      inputValue = next;
      void tick().then(autoGrowTextarea);
    },
    submit: send,
    onError: (message) => ui.toast(message, 'error'),
    onSettled: () => tick().then(autoGrowTextarea),
    focusDraft: () => requestAnimationFrame(() => textareaEl?.focus()),
  });
  const isVoiceRecording = $derived(voiceDictation.isRecording);
  const voiceControlDisabled = $derived(sending || voiceDictation.controlDisabled);

  function ideaDisplayTitle(source: { display_title?: string | null; title?: string | null }): string {
    return source.display_title?.trim() || source.title?.trim() || 'Untitled thread';
  }

  function clampNumber(value: number, min: number, max: number) {
    return Math.min(max, Math.max(min, value));
  }

  function parseCssPixelValue(value: string, fallback: number) {
    const parsed = Number.parseFloat(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function updateThreadStageLayoutMetrics(element: HTMLElement) {
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    const paddingInline =
      parseCssPixelValue(style.paddingLeft, 0) + parseCssPixelValue(style.paddingRight, 0);

    threadStageContentWidth = Math.max(0, rect.width - paddingInline);
    threadStageGutterPx = parseCssPixelValue(
      style.getPropertyValue('--thread-stage-gutter'),
      THREAD_STAGE_DEFAULT_GUTTER,
    );
  }

  const headerConfig = $derived.by(() => {
    const selectedIdea = idea;
    if (!selectedIdea) return null;

    const summarySource = runInfo ?? latestRun;
    const summaryStatus = summarySource?.status
      ? summarySource.status.replaceAll('_', ' ')
      : latestRun?.status
        ? latestRun.status.replaceAll('_', ' ')
        : 'No recent run';

    const summaryEvent = summarySource?.event || latestRun?.event || '';
    const summaryTime = runInfo?.started_at
      ? `${elapsedLabel(runInfo.started_at)} live`
      : latestRun?.timestamp
        ? timeAgo(latestRun.timestamp)
        : latestRun?.started_at
          ? timeAgo(latestRun.started_at)
          : '';

    return {
      title: ideaDisplayTitle(selectedIdea),
      statusLabel,
      statusState: headerStatusState,
      linkActionLabel: threadLinkCopying ? 'Thread link copied' : 'Copy thread link',
      linkActionLoading: threadLinkCopying,
      onLinkAction: () => void copyThreadLink(),
      titleActionLabel: titleGenerating ? 'Generating a new thread title' : 'Generate a new thread title',
      titleActionLoading: titleGenerating,
      onTitleAction: () => void regenerateThreadTitle(),
      archiveActionLabel: threadArchiving ? 'Archiving thread' : 'Archive thread',
      archiveActionLoading: threadArchiving,
      onArchiveAction: () => void archiveThread(),
      panelOpen: browserOpen,
      onTogglePanel: () => {
        if (!browserOpen) void workspaceApps.load({ silent: true });
        onBrowserOpenChange?.(!browserOpen);
      },
      runLabel: summarySource ? 'Latest run' : 'Run',
      runStatus: summaryStatus,
      runEvent: summaryEvent,
      runTime: summaryTime,
      panelLabel: 'Side panel',
    };
  });

  const visibleStreamItems = $derived.by(() =>
    visibleThreadStreamItems(cortex.stream),
  );

  function runSortTime(source: any): number {
    const raw = source?.started_at || source?.timestamp || source?.created_at;
    if (!raw) return 0;
    const parsed = new Date(raw).getTime();
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function hasLiveFastReply(): boolean {
    return streamHasLiveFastReply(visibleStreamItems);
  }

  function activeFastRun(): any | null {
    return findActiveFastRun(runInfo, visibleStreamItems);
  }

  function isQueuedAfterRun(source: any): boolean {
    return Boolean(source?.metadata?.queued_after_run || source?.metadata?.queued_after_run_id);
  }

  function composerPlaceholder(): string {
    if (!activeFastRun()) return 'Ask Illo anything...';
    return activeRunMessageIntent === 'queue'
      ? 'Queue a message for after this reply...'
      : 'Send steering to the running reply...';
  }

  function composerKicker(): string {
    return '';
  }

  function compactTitleSourceText(value: unknown, maxChars = THREAD_TITLE_SOURCE_ITEM_CHARS): string {
    const text = String(value ?? '').replace(/\s+/g, ' ').trim();
    if (!text) return '';
    return text.length > maxChars ? `${text.slice(0, Math.max(0, maxChars - 3))}...` : text;
  }

  function threadTitleSourceLine(item: any): string | null {
    if (item?.type === 'message') {
      const text = compactTitleSourceText(item.content);
      if (!text) return null;
      const role = item.role === 'assistant' || item.role === 'illo' ? 'Illo' : item.user_name || 'User';
      return `${role}: ${text}`;
    }

    if (item?.type === 'visual_block') {
      const title = compactTitleSourceText(item.title, 120);
      const content = compactTitleSourceText(item.content);
      if (!title && !content) return null;
      return `Visual: ${[title, content].filter(Boolean).join(' - ')}`;
    }

    return null;
  }

  function threadTitleSourceText(selectedIdea: typeof idea): string {
    const recentLines = visibleStreamItems
      .slice()
      .reverse()
      .map(threadTitleSourceLine)
      .filter((line): line is string => Boolean(line))
      .slice(0, THREAD_TITLE_SOURCE_ITEM_LIMIT);

    const fallbackLines = [
      selectedIdea?.title ? `Original title: ${compactTitleSourceText(selectedIdea.title, 240)}` : '',
      selectedIdea?.description ? `Description: ${compactTitleSourceText(selectedIdea.description)}` : '',
    ].filter(Boolean);
    const contextLines = recentLines.length ? [...recentLines, ...fallbackLines] : fallbackLines;

    return contextLines.length ? ['Current thread, newest items first:', ...contextLines].join('\n') : '';
  }

  async function regenerateThreadTitle() {
    const selectedIdea = idea;
    if (!selectedIdea?.id || titleGenerating) return;

    const ideaId = selectedIdea.id;
    const sourceText = threadTitleSourceText(selectedIdea);
    if (!sourceText.trim()) {
      ui.toast('There is not enough thread context to title yet.', 'info');
      return;
    }

    titleGenerating = true;
    try {
      const result = await generateTitle(sourceText);
      const nextTitle = compactTitleSourceText(result?.title, 80);
      if (!nextTitle) throw new Error('Title generation returned no title');
      await cortex.updateIdeaDisplayTitle(ideaId, nextTitle);
      ui.toast('Thread title refreshed', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || err?.message || 'Failed to refresh thread title', 'error');
    } finally {
      titleGenerating = false;
    }
  }

  async function copyThreadLink() {
    const selectedIdea = idea;
    if (!selectedIdea?.id || threadLinkCopying) return;
    const value = selectedIdea.thread_url || threadUrl(selectedIdea.id);
    threadLinkCopying = true;
    try {
      await navigator.clipboard.writeText(value);
      ui.toast('Thread link copied', 'success');
    } catch {
      ui.toast(value, 'info');
    } finally {
      window.setTimeout(() => {
        threadLinkCopying = false;
      }, 900);
    }
  }

  async function archiveThread() {
    const selectedIdea = idea;
    if (!selectedIdea?.id || threadArchiving) return;

    threadArchiving = true;
    try {
      await cortex.deleteIdea(selectedIdea.id);
    } finally {
      threadArchiving = false;
    }
  }

  const replyAccent = $derived.by(() => {
    return normalizeHexColor(auth.user?.color) ?? resolveIdeaAccent(idea);
  });

  const replyComposerTone = $derived.by(() => accentTone(replyAccent));
  const existingProjectContext = $derived(extractIdeaProjectContext(idea));
  const latestIdeaProjectContextAttachment = $derived(ideaProjectContextAttachments[0] ?? null);
  const visibleProjectContext = $derived(latestIdeaProjectContextAttachment?.snapshot ?? existingProjectContext);

  async function loadIdeaProjectContext(ideaId: string) {
    if (ideaProjectContextLoadedForIdeaId === ideaId || ideaProjectContextLoadingForIdeaId === ideaId) return;
    ideaProjectContextLoadingForIdeaId = ideaId;
    try {
      const attachments = await listIdeaProjectContext(ideaId);
      if (idea?.id === ideaId) ideaProjectContextAttachments = attachments;
    } catch {
      if (idea?.id === ideaId) ideaProjectContextAttachments = [];
    } finally {
      if (ideaProjectContextLoadingForIdeaId === ideaId) {
        ideaProjectContextLoadedForIdeaId = ideaId;
        ideaProjectContextLoadingForIdeaId = null;
      }
    }
  }

  async function ensureIdeaProjectContextLoaded() {
    if (!idea?.id) return;
    await loadIdeaProjectContext(idea.id);
  }

  function handlePendingProjectContextState(state: ProjectContextPickerState) {
    pendingProjectContextState = state;
    if (state.valid) projectContextError = '';
  }

  const transcriptItems = $derived.by((): CortexThreadStageTranscriptItem[] =>
    buildThreadTranscriptItems({
      idea,
      stream: cortex.stream,
      themeMode: theme.mode === 'light' ? 'light' : 'dark',
      runInfo,
      latestRun,
      currentUser: auth.user,
      onApproveRun: (runId) => void threadStreamController.approveRun(runId),
      onDenyRun: (runId) => void threadStreamController.denyRun(runId),
    }),
  );

  function isNearBottom(threshold = 100): boolean {
    return conversationIsNearBottom(transcriptEl, threshold);
  }

  function handleScrollEvent() {
    if (programmaticScroll) return;
    userScrolledUp = !isNearBottom(CONVERSATION_SCROLL_BOTTOM_THRESHOLD);
    syncTranscriptScrollCue();
  }

  function scrollToBottom(force = false) {
    if (!transcriptEl) return;
    if (!force && userScrolledUp) return;
    programmaticScroll = true;
    scrollConversationToBottom(transcriptEl);
    requestAnimationFrame(() => {
      scrollConversationToBottom(transcriptEl);
      programmaticScroll = false;
      userScrolledUp = false;
      syncTranscriptScrollCue();
    });
  }

  function keepTranscriptPinnedToBottom() {
    if (!transcriptEl || userScrolledUp) {
      syncTranscriptScrollCue();
      return;
    }

    if (transcriptScrollFrame !== null) {
      cancelAnimationFrame(transcriptScrollFrame);
    }

    transcriptScrollFrame = requestAnimationFrame(() => {
      transcriptScrollFrame = null;
      scrollToBottom(true);
    });
  }

  function syncTranscriptScrollCue() {
    if (!transcriptEl) {
      showTranscriptScrollCue = false;
      return;
    }

    showTranscriptScrollCue = shouldShowConversationScrollCue(transcriptEl);
  }

  async function send() {
    const text = inputValue.trim();
    if ((!text && pendingAttachments.length === 0) || sending) return;
    if (!pendingProjectContextState.valid) {
      projectContextError = pendingProjectContextState.error ?? 'Fix the project context before sending.';
      return;
    }

    sending = true;
    inputValue = '';
    void tick().then(autoGrowTextarea);
    const attachments = [...pendingAttachments];
    const projectContext = pendingProjectContextState.snapshot;
    if (projectContext) {
      attachments.push(buildProjectContextMessageAttachment(projectContext));
    }
    const fastSteerTarget = activeFastRun();
    const queueAfterTarget = fastSteerTarget && activeRunMessageIntent === 'queue';
    pendingAttachments = [];
    try {
      await threadStreamController.sendReply(text || '(attachment)', attachments, {
        executionProfile: cortex.executionProfile,
        skipRun: Boolean(fastSteerTarget && !queueAfterTarget),
        metadata: fastSteerTarget
          ? queueAfterTarget
            ? {
                queued_after_run: true,
                queued_after_run_id: fastSteerTarget.id,
                message_intent: 'queue',
              }
            : {
                live_guidance: true,
                fast_steer: true,
                target_run_id: fastSteerTarget.id,
              }
          : undefined,
      });
      await tick();
      scrollToBottom(true);
    } catch {
      pendingAttachments = attachments;
      inputValue = text;
      void tick().then(autoGrowTextarea);
    }
    sending = false;
  }

  function setRunSetting(key: string, value: string) {
    applyRunSetting(key, value, {
      setExecutionProfile: (nextValue) => cortex.setExecutionProfile(nextValue),
      setIntelligenceTier: (nextValue) => cortex.setIntelligenceTier(nextValue),
      setEffortLevel: (nextValue) => cortex.setEffortLevel(nextValue),
    });
  }

  function setActiveRunMessageIntent(value: string) {
    activeRunMessageIntent = value === 'queue' ? 'queue' : 'steer';
  }

  function activeRunSendLabel(): string {
    if (!activeFastRun()) return 'Send';
    return activeRunMessageIntent === 'queue' ? 'Queue' : 'Send steering';
  }

  $effect(() => {
    const run = activeFastRun();
    const runId = run ? String(run.run_id ?? run.id ?? '') : null;
    if (!runId) {
      activeRunIntentTargetId = null;
      activeRunMessageIntent = 'steer';
      return;
    }
    if (activeRunIntentTargetId && activeRunIntentTargetId !== runId) {
      activeRunMessageIntent = 'steer';
    }
    activeRunIntentTargetId = runId;
  });

  function autoGrowTextarea() {
    if (!textareaEl) return;
    textareaScrollTop = resizeComposerTextareaToContent(textareaEl, {
      value: inputValue,
      minHeight: 34,
      maxHeight: 120,
      emptyHeight: 34,
    });
  }

  function ensureTeamMembersForMentions() {
    if (teamMembers.length > 0 || teamMembersLoading) return;
    teamMembersLoading = true;
    cortex.loadTeamMembers()
      .then((members) => {
        teamMembers = members;
        checkMentionTrigger();
      })
      .catch(() => {
        teamMembers = [];
      })
      .finally(() => {
        teamMembersLoading = false;
      });
  }

  function checkMentionTrigger() {
    if (!textareaEl) return;
    const value = inputValue;
    const cursorPos = textareaEl.selectionStart;
    const before = value.slice(0, cursorPos);
    const atMatch = before.match(/(^|\s)@(\w*)$/);
    if (atMatch) {
      if (teamMembers.length === 0) {
        ensureTeamMembersForMentions();
        mentionMatches = [];
        mentionDropdownVisible = false;
        return;
      }
      const query = atMatch[2].toLowerCase();
      const members = teamMembers.map((member) => ({
        ...member,
        hint: 'Notifies with gentle halo',
      }));
      const all = members;
      mentionMatches = query ? all.filter((member) => member.name.toLowerCase().includes(query)) : all;
      mentionSelectedIndex = 0;
      mentionDropdownVisible = mentionMatches.length > 0;
      return;
    }
    mentionDropdownVisible = false;
  }

  function insertMention(name: string) {
    if (!textareaEl) return;
    const value = inputValue;
    const cursorPos = textareaEl.selectionStart;
    const before = value.slice(0, cursorPos);
    const atPos = before.lastIndexOf('@');
    if (atPos >= 0) {
      inputValue = `${before.slice(0, atPos)}@${name} ${value.slice(cursorPos)}`;
      const newPos = atPos + name.length + 2;
      tick().then(() => {
        textareaEl?.setSelectionRange(newPos, newPos);
        textareaEl?.focus();
      });
    }
    mentionDropdownVisible = false;
  }

  function syncSlashAutocomplete() {
    const token = textareaEl
      ? findSlashCommandToken(inputValue, textareaEl.selectionStart ?? inputValue.length)
      : null;
    slashToken = token;
    if (token) slashRef?.filter(token.query);
    else slashRef?.clear();
    return token;
  }

  function applySlashCommand(cmd: string) {
    const token = slashToken ?? syncSlashAutocomplete();
    if (!token) {
      inputValue = cmd;
      requestAnimationFrame(() => {
        textareaEl?.focus();
        autoGrowTextarea();
        checkMentionTrigger();
      });
      return;
    }

    const next = replaceSlashCommandToken(inputValue, token, cmd);
    inputValue = next.value;
    slashToken = null;
    slashRef?.clear();
    mentionDropdownVisible = false;
    requestAnimationFrame(() => {
      textareaEl?.focus();
      textareaEl?.setSelectionRange(next.cursor, next.cursor);
      autoGrowTextarea();
      checkMentionTrigger();
    });
  }

  function handleCursorChange() {
    const token = syncSlashAutocomplete();
    if (token) {
      mentionDropdownVisible = false;
    } else {
      checkMentionTrigger();
    }
  }

  function handleKeydown(event: KeyboardEvent) {
    if (slashRef?.handleKey(event)) return;

    if (mentionDropdownVisible) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        mentionSelectedIndex = Math.min(mentionSelectedIndex + 1, mentionMatches.length - 1);
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        mentionSelectedIndex = Math.max(mentionSelectedIndex - 1, 0);
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        if (mentionMatches[mentionSelectedIndex]) {
          insertMention(mentionMatches[mentionSelectedIndex].name);
        }
        return;
      }
      if (event.key === 'Escape') {
        mentionDropdownVisible = false;
        return;
      }
    }

    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (isVoiceRecording) {
        void voiceDictation.send();
        return;
      }
      void send();
    }
  }

  function handleTextInput() {
    const token = syncSlashAutocomplete();
    if (token) mentionDropdownVisible = false;
    else checkMentionTrigger();
    autoGrowTextarea();
  }

  function handleTextareaScroll() {
    textareaScrollTop = textareaEl?.scrollTop ?? 0;
  }

  async function uploadFiles(files: File[]) {
    const { uploaded, failures } = await uploadWorkspaceComposerFiles(files, uploadFile);
    if (uploaded.length) pendingAttachments = [...pendingAttachments, ...uploaded];
    for (const failure of failures) {
      ui.toast(getWorkspaceComposerUploadFailureMessage(failure), 'error');
    }
  }

  async function handlePaste(event: ClipboardEvent) {
    const files = getWorkspaceComposerPasteFiles(event);
    if (files.length === 0) return;
    event.preventDefault();
    await uploadFiles(files);
  }

  async function handleFileSelect(event: Event) {
    const files = getWorkspaceComposerInputFiles(event);
    if (!files.length) return;
    await uploadFiles(files);
    resetWorkspaceComposerFileInput(event);
  }

  function removeAttachment(index: number) {
    pendingAttachments = pendingAttachments.filter((_, attachmentIndex) => attachmentIndex !== index);
  }

  async function handleDrop(event: DragEvent) {
    preventWorkspaceComposerDefaultDrag(event);
    threadDragOver = false;
    const files = getWorkspaceComposerDropFiles(event);
    if (!files.length) return;
    await uploadFiles(files);
  }

  function handleDragOver(event: DragEvent) {
    preventWorkspaceComposerDefaultDrag(event);
    threadDragOver = true;
  }

  function handleDragLeave(event: DragEvent) {
    preventWorkspaceComposerDefaultDrag(event);
    threadDragOver = false;
  }

  function handleDocClick(event: MouseEvent) {
    const target = event.target as HTMLElement;
    if (mentionDropdownVisible && !target.closest('.mention-dropdown') && !target.closest('.thread-bridge-textarea')) {
      mentionDropdownVisible = false;
    }
  }

  function sidePanelState(): ThreadSidePanelTabState {
    return { tabs: sidePanelTabs, activeTabId: activeSidePanelTabId, nextBrowserTabIndex };
  }

  function applySidePanelState(state: ThreadSidePanelTabState) {
    sidePanelTabs = state.tabs;
    activeSidePanelTabId = state.activeTabId;
    nextBrowserTabIndex = state.nextBrowserTabIndex;
    onBrowserOpenChange?.(true);
  }

  function openSidePanelTab(tabId: string | null) {
    activeSidePanelTabId = activateThreadSidePanelTab(sidePanelTabs, tabId);
    onBrowserOpenChange?.(true);
    if (activeThreadSidePanelTab(sidePanelTabs, activeSidePanelTabId)?.kind === 'app') {
      void workspaceApps.load({ silent: true });
    }
  }

  function addBrowserTab() {
    applySidePanelState(addBrowserThreadSidePanelTab(sidePanelState()));
  }

  function openSingletonTab(kind: ThreadStageRightDockSingletonKind) {
    applySidePanelState(openSingletonThreadSidePanelTab(sidePanelState(), kind));
  }

  function openPreviewTab(attachment: CortexThreadStageImageAttachment | CortexThreadStageFileAttachment) {
    dockPreviewAttachment = attachment;
    openSingletonTab('preview');
  }

  function openCodeReviewFilePreview(file: CodeReviewFile) {
    applySidePanelState(openFilePreviewThreadSidePanelTab(sidePanelState(), file.path, file.runId ?? null));
  }

  function openAppTab(appId: string | null | undefined) {
    const app = threadArtifactApps.find((candidate) => candidate.id === appId) ?? null;
    applySidePanelState(openAppThreadSidePanelTab(sidePanelState(), appId, app));
  }

  function handleThreadAppSelect(appId: string | null) {
    if (appId) {
      openAppTab(appId);
      return;
    }

    if (activeSidePanelTab?.kind === 'app') {
      closeSidePanelTab(activeSidePanelTab.id);
    }
  }

  $effect(() => {
    const appId = requestedThreadAppId;
    if (requestedThreadAppLoadRequestedFor && requestedThreadAppLoadRequestedFor !== appId) {
      requestedThreadAppLoadRequestedFor = null;
    }
    const app = workspaceApps.appById(appId);
    const decision = decideThreadArtifactDeepLink({
      requestedAppId: appId,
      lastAutoOpenedAppId: lastAutoOpenedThreadAppId,
      appExists: Boolean(app),
      appBelongsToCurrentThread: workspaceApps.appBelongsToThread(app, idea?.id ?? null),
      currentThreadLoaded: Boolean(idea?.id),
      loadRequestedForAppId: requestedThreadAppLoadRequestedFor,
      appsLoading: workspaceApps.loading,
    });

    if (decision.action === 'request-refresh') {
      requestedThreadAppLoadRequestedFor = decision.appId;
      void workspaceApps.load({ silent: true, force: true });
      return;
    }
    if (decision.action !== 'open') return;

    requestedThreadAppLoadRequestedFor = null;
    lastAutoOpenedThreadAppId = decision.appId;
    openAppTab(decision.appId);
  });

  $effect(() => {
    const appId = workspaceApps.lastChangedAppId;
    if (!appId || appId === lastAutoOpenedThreadAppId) return;
    if (workspaceApps.lastChangeAction !== 'create' && workspaceApps.lastChangeAction !== 'update') return;
    const app = workspaceApps.appById(appId);
    if (!workspaceApps.appBelongsToThread(app, idea?.id ?? null)) return;
    lastAutoOpenedThreadAppId = appId;
    openAppTab(appId);
  });

  function closeSidePanelTab(tabId: string) {
    const closingTab = sidePanelTabs.find((tab) => tab.id === tabId);
    const nextState = closeThreadSidePanelTab(sidePanelTabs, activeSidePanelTabId, tabId);
    sidePanelTabs = nextState.tabs;
    activeSidePanelTabId = nextState.activeTabId;
    if (closingTab?.kind === 'preview') {
      dockPreviewAttachment = null;
    }
    onBrowserOpenChange?.(true);
  }

  function handleSidePanelAddMenuItem(item: ThreadStageRightDockAddMenuItem) {
    if (item.kind === 'browser') {
      addBrowserTab();
      return;
    }
    if (item.kind === 'preview') {
      if (dockPreviewAttachment) openPreviewTab(dockPreviewAttachment);
      return;
    }
    if (item.kind === 'app') {
      openAppTab(item.appId);
      return;
    }
    if (isThreadSidePanelSingletonKind(item.kind)) {
      openSingletonTab(item.kind);
    }
  }

  let pendingInitialScrollIdeaId = $state<string | null>(null);
  let lastSelectedIdeaId = $state<string | null>(null);

  $effect(() => {
    const streamRuns = visibleStreamItems
      .filter((item: any) => item.type === 'run')
      .sort((left: any, right: any) => runSortTime(right) - runSortTime(left));

    const activeRuns = streamRuns.filter((item: any) => isActiveRun(item));
    runInfo = activeRuns.find((item: any) => !isQueuedAfterRun(item)) ?? activeRuns[0] ?? null;
    latestRun = streamRuns[0] ?? null;
  });

  $effect(() => {
    const currentIdeaId = idea?.id ?? null;
    if (currentIdeaId !== lastSelectedIdeaId) {
      pendingInitialScrollIdeaId = currentIdeaId;
      lastSelectedIdeaId = currentIdeaId;
      pendingAttachments = [];
      inputValue = '';
      void tick().then(autoGrowTextarea);
      projectContextError = '';
      ideaProjectContextAttachments = [];
      ideaProjectContextLoadedForIdeaId = null;
      ideaProjectContextLoadingForIdeaId = null;
      activeSidePanelTabId = 'activity';
      sidePanelTabs = createDefaultThreadSidePanelTabs();
      nextBrowserTabIndex = 1;
      dockPreviewAttachment = null;
      onBrowserOpenChange?.(false);
      if (currentIdeaId) void loadIdeaProjectContext(currentIdeaId);
    }
  });

  $effect(() => {
    if (!idea) return;
    if (cortex.streamLoading) return;
    if (pendingInitialScrollIdeaId !== idea.id) return;
    pendingInitialScrollIdeaId = null;
    tick().then(() =>
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          scrollToBottom(true);
          syncTranscriptScrollCue();
        }),
      ),
    );
  });

  $effect(() => {
    const streamLength = cortex.stream.length;
    if (streamLength) {
      tick().then(() =>
        requestAnimationFrame(() => {
          scrollToBottom();
          syncTranscriptScrollCue();
        }),
      );
    }
  });

  $effect(() => {
    const element = transcriptEl;
    if (!element || typeof ResizeObserver === 'undefined') return;

    const observer = new ResizeObserver(keepTranscriptPinnedToBottom);
    observer.observe(element);

    return () => {
      observer.disconnect();
      if (transcriptScrollFrame !== null) {
        cancelAnimationFrame(transcriptScrollFrame);
        transcriptScrollFrame = null;
      }
    };
  });

  $effect(() => {
    const element = threadStagePanelEl;
    if (!element || typeof window === 'undefined') return;

    const update = () => updateThreadStageLayoutMetrics(element);
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);

    update();
    observer?.observe(element);
    window.addEventListener('resize', update);

    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', update);
    };
  });

  onMount(async () => {
    document.addEventListener('click', handleDocClick);
    await voiceDictation.loadSettings();
  });

  onDestroy(() => {
    voiceDictation.destroy();
    document.removeEventListener('click', handleDocClick);
    if (transcriptScrollFrame !== null) {
      cancelAnimationFrame(transcriptScrollFrame);
      transcriptScrollFrame = null;
    }
  });
</script>

{#if cortex.panelOpen && idea}
  {#snippet replyDock()}
    <div class="thread-bridge-reply" class:drag-over={threadDragOver}>
      {#if mentionDropdownVisible}
        <div class="mention-dropdown">
          <div class="mention-dropdown-label">Mention someone</div>
          {#each mentionMatches as match, index}
            <button
              type="button"
              class="mention-option"
              class:selected={index === mentionSelectedIndex}
              onclick={() => insertMention(match.name)}
            >
              <span
                class="mention-avatar"
                style="--mention-avatar-color: {match.color || '#6366f1'};"
              >{match.isIllo ? 'I' : (match.name || '').slice(0, 2).toUpperCase()}</span>
              <span class="mention-copy">
                <strong>{match.name}</strong>
                <span>{match.hint}</span>
              </span>
            </button>
          {/each}
        </div>
      {/if}

      {#snippet editor()}
        <div class="thread-bridge-editor">
          <SlashAutocomplete
            bind:this={slashRef}
            visible={Boolean(slashToken)}
            anchor={textareaEl}
            oninput={applySlashCommand}
          />
          {#if hasSkillMention(inputValue)}
            <SkillMentionOverlay value={inputValue} scrollTop={textareaScrollTop} />
          {/if}

          <input
            type="file"
            accept={ATTACHMENT_INPUT_ACCEPT}
            multiple
            style="display:none"
            bind:this={fileInputEl}
            onchange={handleFileSelect}
          />

          <textarea
            class="thread-bridge-textarea"
            class:has-skill-mentions={hasSkillMention(inputValue)}
            bind:this={textareaEl}
            bind:value={inputValue}
            onkeydown={handleKeydown}
            oninput={handleTextInput}
            onkeyup={handleCursorChange}
            onclick={handleCursorChange}
            onscroll={handleTextareaScroll}
            onpaste={handlePaste}
            placeholder={composerPlaceholder()}
            rows="1"
            disabled={sending}
          ></textarea>
        </div>
      {/snippet}

      <WorkspaceComposerAdapter
        mode="thread"
        tone={replyComposerTone}
        kicker={composerKicker()}
        placeholder={composerPlaceholder()}
        attachments={pendingAttachments}
        canSubmit={(isVoiceRecording || inputValue.trim().length > 0 || pendingAttachments.length > 0) && pendingProjectContextState.valid}
        footerStatusActive={isVoiceRecording}
        allowSubmitWhileWorking={Boolean(activeFastRun())}
        actionState={isVoiceRecording ? 'idle' : (runInfo || idea?.status === 'working' ? 'working' : 'idle')}
        disabled={sending}
        className="cortex-thread-composer-adapter"
        sendLabel={activeRunSendLabel()}
        settingsGroups={buildRunSettingsGroups({
          mode: cortex.executionProfile,
          intelligence: cortex.intelligenceTier,
          effort: cortex.effortLevel,
        })}
        onSettingsChange={setRunSetting}
        settingsAriaLabel="Mode, Intelligence, and Effort"
        secondaryIntentOptions={activeFastRun() ? STEERING_INTENT_OPTIONS : undefined}
        secondaryIntentValue={activeFastRun() ? activeRunMessageIntent : undefined}
        secondaryIntentAriaLabel="Message intent"
        onSecondaryIntentChange={setActiveRunMessageIntent}
        onSubmit={() => (isVoiceRecording ? void voiceDictation.send() : void send())}
        onStop={() => void threadStreamController.cancelAll()}
        onAttach={() => fileInputEl?.click()}
        onRemoveAttachment={removeAttachment}
        onDrop={handleDrop}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        editor={editor}
      >
        {#snippet extraLeadingControls()}
          <ProjectContextPicker
            mode="thread"
            currentSnapshot={visibleProjectContext}
            contextKey={idea?.id ?? ''}
            onStateChange={handlePendingProjectContextState}
            onOpenChange={(open) => { if (open) void ensureIdeaProjectContextLoaded(); }}
          />
          {#if projectContextError}
            <span class="project-context-inline-error">{projectContextError}</span>
          {/if}
        {/snippet}
        {#snippet footerStatus()}
          {#if isVoiceRecording}
            <WorkspaceVoiceRecording elapsedMs={voiceDictation.elapsedMs} levels={voiceDictation.audioLevels} />
          {/if}
        {/snippet}
        {#snippet extraTrailingControls()}
          <ConstellationComposerOrb
            label={voiceDictation.controlLabel}
            title={voiceDictation.controlTitle}
            disabled={voiceControlDisabled}
            variant="bare"
            onclick={() => {
              if (!sending) voiceDictation.toggle();
            }}
          >
            <ConstellationIcon name={isVoiceRecording ? 'stop' : 'mic'} size={18} stroke={2} />
          </ConstellationComposerOrb>
        {/snippet}
      </WorkspaceComposerAdapter>
    </div>
  {/snippet}

  {#snippet browserPane()}
    <BrowserThoughtPanel onPreviewAttachment={openPreviewTab} />
  {/snippet}

  {#snippet utilityPane()}
    <div class="thread-utility-surface">
      <div class="thread-utility-surface-body">
        <ThreadUtilityContent
          {idea}
          activeTab={activeSidePanelTab?.kind === 'handoff-summary' ? 'handoff-summary' : 'activity'}
        />
      </div>
    </div>
  {/snippet}

  {#snippet projectPane()}
    <div class="thread-utility-surface">
      <div class="thread-utility-surface-body">
        <ProjectDraftStatePanel {idea} runId={projectDraftRunId} />
      </div>
    </div>
  {/snippet}

  {#snippet previewPane()}
    <ThreadAttachmentPreviewPane attachment={dockPreviewAttachment} />
  {/snippet}

  {#snippet discussionPane()}
    <ThreadDiscussionPane ideaId={idea?.id ?? null} />
  {/snippet}

  {#snippet appsPane()}
    <ThreadAppsPane
      apps={threadArtifactApps}
      selectedAppId={selectedThreadApp?.id ?? null}
      onSelectApp={handleThreadAppSelect}
    />
  {/snippet}

  {#snippet codeReviewPane()}
    <ThreadCodeReviewPane
      files={codeReviewFiles}
      latestRunStatus={latestRun?.status ?? null}
      onPreviewFile={openCodeReviewFilePreview}
    />
  {/snippet}

  {#snippet filePreviewPane()}
    <ThreadProjectFilePreviewPane
      {idea}
      runId={activeFilePreviewRunId ?? projectDraftRunId}
      filePath={activeFilePreviewPath}
    />
  {/snippet}

  {#snippet vaultPane()}
    <div class="thread-vault-surface">
      <VaultPage
        embedded
        initialCreatePrefill={activeVaultSecretPrompt
          ? {
              id: activeVaultSecretPrompt.id,
              keyName: activeVaultSecretPrompt.key_name,
              description: activeVaultSecretPrompt.description,
              category: activeVaultSecretPrompt.category,
            }
          : null}
        initialAgentGrantPrompt={activeVaultAgentGrantPrompt
          ? {
              id: activeVaultAgentGrantPrompt.id,
              grantId: activeVaultAgentGrantPrompt.grant_id,
              keyName: activeVaultAgentGrantPrompt.key_name,
              reason: activeVaultAgentGrantPrompt.reason,
            }
          : null}
        onInitialCreateSaved={(promptId) => {
          if (promptId) cortex.clearVaultSecretPrompt(promptId);
        }}
        onInitialAgentGrantHandled={(promptId) => {
          if (promptId) cortex.clearVaultAgentGrantPrompt(promptId);
        }}
      />
    </div>
  {/snippet}

  {#snippet cyclesPane()}
    <ThreadCyclesPane
      focusCycleId={cortex.cyclePanelSignal?.ideaId === idea?.id ? cortex.cyclePanelSignal.cycleId : null}
      refreshSerial={cortex.cyclePanelSignal?.ideaId === idea?.id ? cortex.cyclePanelSignal.serial : null}
    />
  {/snippet}

  <ThreadStageShell
    {entering}
    {ready}
    {accentColor}
    {accentRgb}
    {origin}
    {peripherySignals}
    {ondismiss}
  >
    <section
      class="thread-stage-panel"
      class:solo={!browserOpen}
      bind:this={threadStagePanelEl}
      style={panelStyle}
      data-cortex-surface="thread-shell"
    >
      <div class="thread-stage-layout" class:with-dock={browserOpen}>
        <div class="thread-stage-thread">
          <ThreadTranscript
            header={headerConfig}
            transcriptItems={transcriptItems}
            loading={cortex.streamLoading}
            showScrollCue={showTranscriptScrollCue}
            replyDock={replyDock}
            onTranscriptScroll={handleScrollEvent}
            onScrollToBottom={() => scrollToBottom(true)}
            onPreviewAttachment={openPreviewTab}
            onTranscriptReady={(element) => {
              transcriptEl = element;
              requestAnimationFrame(() => syncTranscriptScrollCue());
            }}
          />
        </div>

        {#if browserOpen}
          <div class="thread-stage-dock">
            <ThreadStageRightDock
              activeTabId={activeSidePanelTabId}
              tabs={sidePanelTabs}
              addMenuItems={sidePanelAddMenuItems}
              className="is-stage-integrated"
              width={resolvedDockWidth}
              minWidth={dockMinWidth}
              maxWidth={dockLayoutMaxWidth}
              resizable={true}
              onWidthChange={onDockWidthChange}
              onTabChange={openSidePanelTab}
              onTabClose={closeSidePanelTab}
              onAddMenuItem={handleSidePanelAddMenuItem}
              browserPane={browserPane}
              previewPane={previewPane}
              discussionPane={discussionPane}
              utilityPane={utilityPane}
              projectPane={projectPane}
              appsPane={appsPane}
              vaultPane={vaultPane}
              cyclesPane={cyclesPane}
              filePreviewPane={filePreviewPane}
              codeReviewPane={codeReviewPane}
            />
          </div>
        {/if}
      </div>
    </section>
  </ThreadStageShell>
{/if}

<style>
  .thread-stage-panel {
    --thread-stage-dock-width: 432px;
    --thread-stage-thread-min: 380px;
    --thread-stage-thread-max: clamp(860px, 74vw, 1560px);
    --thread-stage-readable-max: 860px;
    --thread-stage-gutter: clamp(16px, 1.7vw, 24px);
    --thread-stage-panel-backdrop-filter: none;
    --thread-stage-panel-before-filter: blur(48px);
    --thread-stage-panel-before-opacity: 0.16;
    --thread-stage-panel-radius: 24px;
    --thread-stage-panel-padding-block: clamp(14px, 1.7vw, 22px);
    --thread-stage-panel-padding-inline: clamp(16px, 2vw, 24px);
    --thread-stage-docked-header-height: 46px;
    --thread-stage-stacked-dock-height: clamp(220px, 34svh, 360px);
    --thread-bridge-mention-dropdown-border: rgba(124, 138, 158, 0.14);
    --thread-bridge-mention-dropdown-background:
      linear-gradient(180deg, rgba(10, 14, 22, 0.98), rgba(8, 11, 18, 1));
    --thread-bridge-mention-dropdown-shadow: 0 22px 44px rgba(0, 0, 0, 0.28);
    --thread-bridge-mention-option-border: rgba(124, 138, 158, 0.12);
    --thread-bridge-mention-option-background: rgba(255, 255, 255, 0.03);
    --thread-bridge-mention-option-selected-border: rgba(94, 207, 160, 0.22);
    --thread-bridge-mention-option-selected-background: rgba(94, 207, 160, 0.08);
    --thread-bridge-mention-avatar-text:
      color-mix(in srgb, var(--mention-avatar-color, #6366f1) 34%, #defbee 66%);
    --thread-bridge-mention-avatar-background:
      color-mix(in srgb, var(--mention-avatar-color, #6366f1) 16%, transparent);
    --thread-bridge-mention-avatar-border: 0 solid transparent;
    --thread-bridge-mention-avatar-shadow: none;
    --thread-bridge-mention-name-text: rgba(242, 247, 255, 0.92);
    --thread-bridge-mention-meta-text: rgba(218, 228, 241, 0.6);
    position: relative;
    height: 100%;
    min-height: 0;
    container: thread-stage-panel / inline-size;
    border: 1px solid var(--constellation-thread-reading-core-border);
    border-radius: var(--thread-stage-panel-radius);
    overflow: hidden;
    isolation: isolate;
    box-sizing: border-box;
    padding:
      var(--thread-stage-panel-padding-block)
      var(--thread-stage-panel-padding-inline);
    background: var(--constellation-thread-reading-core-background);
    box-shadow: var(--constellation-thread-reading-core-shadow);
    backdrop-filter: var(--thread-stage-panel-backdrop-filter);
    -webkit-backdrop-filter: var(--thread-stage-panel-backdrop-filter);
  }

  :global(:root[data-color-scheme='light']) .thread-stage-panel {
    --thread-stage-panel-backdrop-filter: none;
    --thread-stage-panel-before-filter: blur(36px);
    --thread-stage-panel-before-opacity: 0.06;
    --thread-bridge-mention-dropdown-border: rgba(126, 92, 52, 0.1);
    --thread-bridge-mention-dropdown-background: var(--constellation-surface-floating-background);
    --thread-bridge-mention-dropdown-shadow: var(--constellation-surface-floating-shadow);
    --thread-bridge-mention-option-border: rgba(126, 92, 52, 0.08);
    --thread-bridge-mention-option-background: rgba(248, 250, 248, 0.78);
    --thread-bridge-mention-option-selected-border: rgba(20, 120, 93, 0.22);
    --thread-bridge-mention-option-selected-background: rgba(20, 120, 93, 0.09);
    --thread-bridge-mention-avatar-text:
      color-mix(in srgb, var(--mention-avatar-color, #315fd6) 18%, rgba(32, 43, 54, 0.9));
    --thread-bridge-mention-avatar-background:
      color-mix(in srgb, var(--mention-avatar-color, #315fd6) 9%, rgba(255, 253, 247, 0.97));
    --thread-bridge-mention-avatar-border:
      1px solid color-mix(in srgb, var(--mention-avatar-color, #315fd6) 24%, rgba(126, 92, 52, 0.1));
    --thread-bridge-mention-avatar-shadow: 0 6px 14px rgba(54, 70, 82, 0.06);
    --thread-bridge-mention-name-text: rgba(18, 27, 36, 0.92);
    --thread-bridge-mention-meta-text: rgba(82, 98, 111, 0.68);
  }

  .thread-utility-surface {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-width: 0;
    min-height: 0;
    height: 100%;
    gap: 14px;
  }

  .thread-utility-surface-body {
    flex: 1 1 auto;
    width: 100%;
    min-width: 0;
    min-height: 0;
    display: flex;
  }

  .thread-utility-surface-body > :global(*) {
    flex: 1 1 auto;
    min-height: 0;
  }

  .thread-vault-surface {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
  }

  .thread-vault-surface :global(.vault-constellation-frame.is-embedded) {
    min-height: 0;
    overflow: visible;
  }

  .thread-vault-surface :global(.vault-constellation-frame.is-embedded .constellation-page-frame-scene-glow),
  .thread-vault-surface :global(.vault-constellation-frame.is-embedded .constellation-page-frame-scene-warmth) {
    display: none;
  }

  .thread-vault-surface :global(.vault-constellation-frame.is-embedded .constellation-page-frame-shell) {
    width: 100%;
    gap: 12px;
  }

  .thread-vault-surface :global(.vault-constellation-frame.is-embedded .constellation-page-frame-header) {
    padding: 2px 2px 12px;
  }

  .thread-vault-surface :global(.vault-constellation-frame.is-embedded .constellation-page-frame-header-head) {
    flex-direction: column;
    gap: 12px;
  }

  .thread-vault-surface :global(.vault-constellation-frame.is-embedded .constellation-page-frame-header-actions) {
    width: 100%;
    justify-content: flex-start;
    gap: 8px;
  }

  .thread-vault-surface :global(.vault-constellation-frame.is-embedded .constellation-page-frame-header-actions > *) {
    flex: 1 1 auto;
  }

  .thread-vault-surface :global(.vault-page.is-embedded) {
    gap: 12px;
  }

  .thread-vault-surface :global(.vault-page.is-embedded .inventory-tools) {
    padding: 12px;
    gap: 10px;
  }

  .thread-vault-surface :global(.vault-page.is-embedded .vault-list) {
    padding: 0 10px 10px;
  }

  .thread-vault-surface :global(.vault-page.is-embedded .vault-row) {
    grid-template-columns: 1fr;
  }

  .thread-vault-surface :global(.vault-page.is-embedded .vault-row-side) {
    justify-items: start;
  }

  .thread-vault-surface :global(.vault-page.is-embedded .metadata-list) {
    grid-template-columns: 1fr;
  }

  .thread-stage-panel::before,
  .thread-stage-panel::after {
    content: none;
    position: absolute;
    pointer-events: none;
    z-index: 0;
    transform: translateZ(0);
    border-radius: inherit;
  }

  .thread-stage-panel::before {
    inset: -18% -12% -14%;
    background:
      var(--constellation-thread-stage-panel-shadow-well),
      radial-gradient(
        40% 62% at 30% 22%,
        rgba(var(--thread-accent-rgb, 141, 183, 255), 0.075) 0%,
        rgba(var(--thread-accent-rgb, 141, 183, 255), 0.024) 36%,
        transparent 74%
      ),
      radial-gradient(
        42% 66% at 72% 78%,
        rgba(var(--thread-accent-rgb, 87, 207, 160), 0.048) 0%,
        rgba(var(--thread-accent-rgb, 87, 207, 160), 0.016) 42%,
        transparent 80%
      );
    filter: var(--thread-stage-panel-before-filter);
    opacity: var(--thread-stage-panel-before-opacity);
    mask-image: linear-gradient(
      90deg,
      transparent 0%,
      rgba(0, 0, 0, 0.26) 7%,
      rgba(0, 0, 0, 0.78) 15%,
      #000 24%,
      #000 76%,
      rgba(0, 0, 0, 0.78) 85%,
      rgba(0, 0, 0, 0.26) 93%,
      transparent 100%
    );
    -webkit-mask-image: linear-gradient(
      90deg,
      transparent 0%,
      rgba(0, 0, 0, 0.26) 7%,
      rgba(0, 0, 0, 0.78) 15%,
      #000 24%,
      #000 76%,
      rgba(0, 0, 0, 0.78) 85%,
      rgba(0, 0, 0, 0.26) 93%,
      transparent 100%
    );
  }

  .thread-stage-panel::after {
    inset: 0;
    border: 1px solid var(--constellation-thread-reading-core-border);
    background: transparent;
    opacity: 1;
    box-shadow: var(--constellation-thread-reading-core-shadow);
  }

  .thread-stage-layout {
    position: relative;
    z-index: 1;
    width: 100%;
    height: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    align-items: stretch;
    min-height: 0;
    overflow: hidden;
    border-radius: calc(var(--thread-stage-panel-radius) - 8px);
    box-sizing: border-box;
  }

  .thread-stage-panel.solo {
    width: min(100%, var(--thread-stage-thread-max));
    margin-inline: auto;
  }

  .thread-stage-thread,
  .thread-stage-dock {
    min-width: 0;
    min-height: 0;
    pointer-events: auto;
  }

  .thread-stage-thread,
  .thread-stage-dock {
    display: flex;
  }

  .thread-stage-thread {
    width: 100%;
  }

  .thread-stage-thread > :global(*) {
    flex: 1 1 auto;
    min-width: 0;
    width: 100%;
  }

  .thread-stage-thread :global(.thread-transcript) {
    --thread-column-max: var(--thread-stage-readable-max);
  }

  .thread-bridge-reply {
    position: relative;
  }

  .project-context-inline-error {
    font-size: 11px;
    line-height: 1.35;
  }

  .project-context-inline-error {
    color: #d4808f;
  }

  .thread-bridge-editor {
    position: relative;
    min-height: 34px;
    max-height: 120px;
    overflow: visible;
    --skill-mention-padding: 0;
    --skill-mention-font-size: inherit;
    --skill-mention-line-height: 1.55;
  }

  .thread-bridge-textarea {
    width: 100%;
    min-height: 34px;
    max-height: 120px;
    overflow-y: auto;
    resize: none;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--constellation-composer-textarea, rgba(242, 247, 255, 0.92));
    font: inherit;
    line-height: 1.55;
  }

  .thread-bridge-textarea.has-skill-mentions {
    color: transparent;
    caret-color: var(--constellation-composer-textarea, rgba(242, 247, 255, 0.92));
  }

  .thread-bridge-textarea.has-skill-mentions::selection {
    color: var(--constellation-composer-textarea, rgba(242, 247, 255, 0.92));
    background: var(--constellation-skill-mention-selection);
  }

  .thread-bridge-textarea:focus {
    outline: none;
  }

  .mention-dropdown {
    position: absolute;
    left: 0;
    right: 0;
    bottom: calc(100% + 10px);
    z-index: 8;
    display: grid;
    gap: 8px;
    padding: 12px;
    border-radius: 16px;
    border: 1px solid var(--thread-bridge-mention-dropdown-border);
    background: var(--thread-bridge-mention-dropdown-background);
    box-shadow: var(--thread-bridge-mention-dropdown-shadow);
  }

  .mention-dropdown-label {
    color: rgba(214, 194, 142, 0.76);
    font-size: 11px;
    line-height: 1;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .mention-option {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 10px;
    border: 1px solid var(--thread-bridge-mention-option-border);
    border-radius: 12px;
    background: var(--thread-bridge-mention-option-background);
    color: inherit;
    text-align: left;
    cursor: pointer;
  }

  .mention-option.selected {
    border-color: var(--thread-bridge-mention-option-selected-border);
    background: var(--thread-bridge-mention-option-selected-background);
  }

  .mention-avatar {
    width: 26px;
    height: 26px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 999px;
    background: var(--thread-bridge-mention-avatar-background);
    border: var(--thread-bridge-mention-avatar-border);
    box-shadow: var(--thread-bridge-mention-avatar-shadow);
    color: var(--thread-bridge-mention-avatar-text);
    font-size: 11px;
    font-weight: 700;
  }

  .mention-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .mention-copy strong {
    color: var(--thread-bridge-mention-name-text);
    font-size: 13px;
  }

  .mention-copy span {
    color: var(--thread-bridge-mention-meta-text);
    font-size: 12px;
  }

  @media (min-width: 1180px) {
    .thread-stage-layout.with-dock {
      grid-template-columns:
        minmax(var(--thread-stage-thread-min), 1fr)
        minmax(
          0,
          min(
            var(--thread-stage-dock-width),
            max(0px, calc(100% - var(--thread-stage-thread-min) - var(--thread-stage-gutter)))
          )
        );
      column-gap: var(--thread-stage-gutter);
    }

    .thread-stage-dock {
      padding: 0;
      box-sizing: border-box;
      justify-self: stretch;
      align-self: stretch;
    }

    .thread-stage-layout.with-dock .thread-stage-thread :global(.thread-panel-header) {
      min-height: var(--thread-stage-docked-header-height);
      padding: 0 2px 0 0;
    }

    .thread-stage-layout.with-dock .thread-stage-thread :global(.thread-header-title-row) {
      min-height: var(--thread-stage-docked-header-height);
    }
  }

  @container thread-stage-panel (max-width: 1040px) {
    .thread-stage-layout.with-dock {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr) minmax(220px, var(--thread-stage-stacked-dock-height));
      row-gap: 10px;
      column-gap: 0;
    }

    .thread-stage-dock {
      position: relative;
      inset: auto;
      width: 100%;
      min-height: 0;
      padding-top: 10px;
      border-top: 1px solid var(--constellation-utility-panel-header-border);
      box-sizing: border-box;
    }
  }

  @media (max-width: 1179px) {
    .thread-stage-panel {
      --thread-stage-thread-max: clamp(680px, 82vw, 920px);
      --thread-stage-panel-radius: 22px;
      --thread-stage-panel-padding-block: clamp(12px, 2vw, 18px);
      --thread-stage-panel-padding-inline: clamp(12px, 2.4vw, 20px);
    }

    .thread-stage-layout.with-dock {
      grid-template-rows: minmax(0, 1fr) minmax(220px, var(--thread-stage-stacked-dock-height));
      row-gap: 10px;
    }

    .thread-stage-dock {
      position: relative;
      inset: auto;
      width: 100%;
      min-height: 0;
      padding-top: 10px;
      border-top: 1px solid var(--constellation-utility-panel-header-border);
      box-sizing: border-box;
    }
  }

  @media (max-width: 980px) {
    .thread-stage-panel {
      --thread-stage-thread-max: min(100%, 760px);
      --thread-stage-panel-radius: 20px;
      --thread-stage-panel-padding-block: 12px;
      --thread-stage-panel-padding-inline: 12px;
      --thread-stage-stacked-dock-height: clamp(200px, 32svh, 320px);
    }
  }
</style>
