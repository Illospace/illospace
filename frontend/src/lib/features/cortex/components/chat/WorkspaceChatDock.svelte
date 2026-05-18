<script lang="ts">
  import { browser } from '$app/environment';
  import { onDestroy, onMount, tick } from 'svelte';
  import { fly } from 'svelte/transition';
  import type { Component } from 'svelte';
  import { ConstellationIcon, ConstellationIconButton } from '$lib/components/constellation';
  import type { ChatDockPreviewMember } from '$lib/components/chat/ChatDock.svelte';
  import { scrollConversationToBottom } from '$lib/components/chat/conversationScroll';
  import type { CortexChatDockTopLevelMode } from '$lib/features/cortex/components/chat/ChatDockSeam.svelte';
  import { clamp } from '$lib/utils/math';
  import { dragDataIsShareable, setCopyDropEffect } from '$lib/utils/shareDrop';
  import './WorkspaceChatDock.css';

  type ChatDockResizeStart = {
    startY: number;
    startHeight: number;
    minHeight: number;
    maxHeight: number;
  };

  type CortexChatDockSeamComponent = Component<{
    context?: 'workspace' | 'thread';
    topLevelMode?: CortexChatDockTopLevelMode;
    selectedThreadRootId?: string | null;
    selectedConversationId?: string | null;
    selectedPreviewMemberId?: string | null;
    previewMembers?: ChatDockPreviewMember[];
    onTopLevelModeChange?: (mode: CortexChatDockTopLevelMode) => void;
    onOpenRoomThread?: (threadRootId: string) => void;
    onCloseRoomThread?: () => void;
    onOpenConversation?: (conversationId: string) => void;
    onCloseConversation?: () => void;
  }>;

  const CHAT_DOCK_HEIGHT_STORAGE_KEY = 'illo:cortex:chat-dock-height';
  const CHAT_DOCK_MIN_HEIGHT = 118;
  const CHAT_DOCK_MAX_HEIGHT = 420;
  const CHAT_DOCK_TABLET_MAX_HEIGHT = 340;
  const CHAT_DOCK_MOBILE_MAX_HEIGHT = 260;
  const CHAT_DOCK_TOP_SAFE_GAP = 84;
  const CHAT_DOCK_RESIZE_KEYBOARD_STEP = 16;

  let {
    expanded = $bindable(false),
    panelOpen = false,
    threadStageReady = false,
    topLevelMode = 'room',
    selectedThreadRootId = null,
    selectedConversationId = null,
    selectedPreviewMemberId = null,
    previewMembers = [],
    SeamComponent = null,
    onForegroundChange,
    onTopLevelModeChange,
    onOpenRoomThread,
    onCloseRoomThread,
    onOpenConversation,
    onCloseConversation,
  }: {
    expanded?: boolean;
    panelOpen?: boolean;
    threadStageReady?: boolean;
    topLevelMode?: CortexChatDockTopLevelMode;
    selectedThreadRootId?: string | null;
    selectedConversationId?: string | null;
    selectedPreviewMemberId?: string | null;
    previewMembers?: ChatDockPreviewMember[];
    SeamComponent?: CortexChatDockSeamComponent | null;
    onForegroundChange?: (foreground: boolean) => void;
    onTopLevelModeChange?: (mode: CortexChatDockTopLevelMode) => void;
    onOpenRoomThread?: (threadRootId: string) => void;
    onCloseRoomThread?: () => void;
    onOpenConversation?: (conversationId: string) => void;
    onCloseConversation?: () => void;
  } = $props();

  let chatDockEl: HTMLDivElement | undefined = $state();
  let pointerActive = $state(false);
  let focusActive = $state(false);
  let dragActive = $state(false);
  let userHeight = $state<number | null>(null);
  let resizing = $state(false);
  let resizeStart = $state<ChatDockResizeStart | null>(null);
  const foreground = $derived(expanded || pointerActive || focusActive || dragActive);
  const chatDockStyle = $derived(
    userHeight == null ? '' : `--workspace-bottom-surface-user-height:${userHeight}px;`,
  );
  const topLevelModeLabel = $derived.by(() => {
    if (topLevelMode === 'room') return 'Team room';
    if (topLevelMode === 'unread') return 'Unread';
    return 'Direct messages';
  });

  $effect(() => {
    onForegroundChange?.(foreground);
  });

  $effect(() => {
    if (!foreground || expanded) return;
    keepMiniChatTailVisible();
  });

  function toggleChatDockSize() {
    if (!expanded) endChatDockResize();
    expanded = !expanded;
  }

  function compactWorkspaceChat() {
    endChatDockResize();
    expanded = false;
    pointerActive = false;
    focusActive = false;
  }

  function getChatDockHeightBounds() {
    const viewportLimit = browser
      ? (chatDockEl?.getBoundingClientRect().bottom ?? window.innerHeight) - CHAT_DOCK_TOP_SAFE_GAP
      : CHAT_DOCK_MAX_HEIGHT;
    const responsiveMax =
      browser && window.matchMedia('(max-width: 700px)').matches
        ? CHAT_DOCK_MOBILE_MAX_HEIGHT
        : browser && window.matchMedia('(max-width: 900px)').matches
          ? CHAT_DOCK_TABLET_MAX_HEIGHT
          : CHAT_DOCK_MAX_HEIGHT;
    const maxHeight = Math.max(CHAT_DOCK_MIN_HEIGHT, Math.min(responsiveMax, viewportLimit));
    return {
      minHeight: CHAT_DOCK_MIN_HEIGHT,
      maxHeight,
    };
  }

  function persistChatDockHeight() {
    if (!browser) return;
    try {
      if (userHeight == null) {
        localStorage.removeItem(CHAT_DOCK_HEIGHT_STORAGE_KEY);
        return;
      }
      localStorage.setItem(CHAT_DOCK_HEIGHT_STORAGE_KEY, String(Math.round(userHeight)));
    } catch {
      // Best-effort preference persistence.
    }
  }

  function setChatDockUserHeight(nextHeight: number, persist = false) {
    if (!Number.isFinite(nextHeight)) return;
    const { minHeight, maxHeight } = getChatDockHeightBounds();
    userHeight = Math.round(clamp(nextHeight, minHeight, maxHeight));
    if (persist) persistChatDockHeight();
  }

  function resetChatDockUserHeight() {
    userHeight = null;
    persistChatDockHeight();
  }

  function loadChatDockUserHeight() {
    if (!browser) return;
    try {
      const rawHeight = Number.parseFloat(localStorage.getItem(CHAT_DOCK_HEIGHT_STORAGE_KEY) ?? '');
      if (Number.isFinite(rawHeight)) {
        setChatDockUserHeight(rawHeight);
      }
    } catch {
      // Best-effort preference restore.
    }
  }

  function handleChatDockResizePointerDown(event: PointerEvent) {
    if (expanded || !chatDockEl || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = chatDockEl.getBoundingClientRect();
    const { minHeight, maxHeight } = getChatDockHeightBounds();
    resizeStart = {
      startY: event.clientY,
      startHeight: rect.height,
      minHeight,
      maxHeight,
    };
    resizing = true;
    pointerActive = true;
    document.documentElement.classList.add('is-chat-dock-resizing');
    document.addEventListener('pointermove', handleChatDockResizePointerMove);
    document.addEventListener('pointerup', handleChatDockResizePointerEnd);
    document.addEventListener('pointercancel', handleChatDockResizePointerEnd);
  }

  function handleChatDockResizePointerMove(event: PointerEvent) {
    if (!resizeStart) return;
    const nextHeight = resizeStart.startHeight + (resizeStart.startY - event.clientY);
    userHeight = Math.round(
      clamp(nextHeight, resizeStart.minHeight, resizeStart.maxHeight),
    );
  }

  function handleChatDockResizePointerEnd() {
    if (!resizeStart && !resizing) return;
    resizeStart = null;
    resizing = false;
    document.removeEventListener('pointermove', handleChatDockResizePointerMove);
    document.removeEventListener('pointerup', handleChatDockResizePointerEnd);
    document.removeEventListener('pointercancel', handleChatDockResizePointerEnd);
    document.documentElement.classList.remove('is-chat-dock-resizing');
    persistChatDockHeight();
  }

  function handleChatDockResizeDoubleClick(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    resetChatDockUserHeight();
  }

  function handleChatDockResizeKeydown(event: KeyboardEvent) {
    if (expanded) return;
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      const currentHeight = userHeight ?? chatDockEl?.getBoundingClientRect().height ?? CHAT_DOCK_MIN_HEIGHT;
      const delta = event.key === 'ArrowUp' ? CHAT_DOCK_RESIZE_KEYBOARD_STEP : -CHAT_DOCK_RESIZE_KEYBOARD_STEP;
      setChatDockUserHeight(currentHeight + delta, true);
    } else if (event.key === 'Home') {
      event.preventDefault();
      setChatDockUserHeight(CHAT_DOCK_MIN_HEIGHT, true);
    } else if (event.key === 'End') {
      event.preventDefault();
      setChatDockUserHeight(getChatDockHeightBounds().maxHeight, true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      resetChatDockUserHeight();
    }
  }

  function handleChatDockGlobalPointerMove(event: PointerEvent) {
    if (resizing) {
      pointerActive = true;
      return;
    }

    if (!chatDockEl || expanded) {
      pointerActive = false;
      return;
    }

    const rect = chatDockEl.getBoundingClientRect();
    const pointerInChatRect =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    const hitTarget = document.elementFromPoint(event.clientX, event.clientY);
    const chatIsTopmost = hitTarget instanceof Node && chatDockEl.contains(hitTarget);
    const threadDismissIsTopmost =
      hitTarget instanceof Element && Boolean(hitTarget.closest('.thread-stage-edge-dismiss'));
    const panelActive = panelOpen && threadStageReady;
    const nextActive = panelActive
      ? pointerInChatRect && (pointerActive || !isPointInsideThreadStagePanel(event.clientX, event.clientY))
      : pointerInChatRect && (chatIsTopmost || threadDismissIsTopmost);

    if (pointerActive !== nextActive) {
      pointerActive = nextActive;
    }
  }

  function isPointInsideThreadStagePanel(clientX: number, clientY: number) {
    if (!browser) return false;
    const panels = document.querySelectorAll<HTMLElement>('.thread-stage-panel, .thread-panel');
    for (const panel of panels) {
      if (chatDockEl?.contains(panel)) continue;
      const rect = panel.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      if (
        clientX >= rect.left &&
        clientX <= rect.right &&
        clientY >= rect.top &&
        clientY <= rect.bottom
      ) {
        return true;
      }
    }
    return false;
  }

  function handleChatDockFocusIn() {
    focusActive = true;
    keepMiniChatTailVisible();
  }

  function handleChatDockFocusOut(event: FocusEvent) {
    const nextTarget = event.relatedTarget;
    if (chatDockEl && nextTarget instanceof Node && chatDockEl.contains(nextTarget)) return;
    focusActive = false;
  }

  function handleChatDockDragEnter(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer)) return;
    event.preventDefault();
    setCopyDropEffect(event.dataTransfer);
    dragActive = true;
    keepMiniChatTailVisible();
  }

  function handleChatDockDragOver(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer)) return;
    event.preventDefault();
    setCopyDropEffect(event.dataTransfer);
    dragActive = true;
  }

  function handleChatDockDragLeave(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer) || !chatDockEl) return;
    event.preventDefault();

    const rect = chatDockEl.getBoundingClientRect();
    const stillInside =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    if (!stillInside) dragActive = false;
  }

  function handleChatDockDrop(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer)) return;
    dragActive = false;
  }

  function resetChatDockPointerState() {
    pointerActive = false;
    focusActive = false;
    dragActive = false;
  }

  function endChatDockResize() {
    handleChatDockResizePointerEnd();
  }

  function scrollMiniChatStreamsToBottom() {
    if (!chatDockEl || expanded) return;
    const streams = chatDockEl.querySelectorAll<HTMLElement>('.chat-pane-stream, .chat-dm-stream');
    for (const stream of streams) {
      scrollConversationToBottom(stream);
    }
  }

  function keepMiniChatTailVisible() {
    if (!browser || !chatDockEl || expanded) return;
    tick().then(() => {
      scrollMiniChatStreamsToBottom();
      window.setTimeout(scrollMiniChatStreamsToBottom, 190);
    });
  }

  onMount(() => {
    loadChatDockUserHeight();
    document.addEventListener('pointermove', handleChatDockGlobalPointerMove, { passive: true });
    window.addEventListener('blur', resetChatDockPointerState);
    window.addEventListener('blur', handleChatDockResizePointerEnd);
  });

  onDestroy(() => {
    document.removeEventListener('pointermove', handleChatDockGlobalPointerMove);
    window.removeEventListener('blur', resetChatDockPointerState);
    window.removeEventListener('blur', handleChatDockResizePointerEnd);
    endChatDockResize();
  });
</script>

{#if expanded}
  <button
    type="button"
    class="workspace-chat-expanded-dismiss-surface"
    tabindex="-1"
    aria-label="Compact chat"
    onclick={compactWorkspaceChat}
  ></button>
{/if}

<div
  bind:this={chatDockEl}
  class="workspace-chat-dock"
  class:is-expanded={expanded}
  class:is-corner={!expanded}
  class:is-foreground={foreground}
  class:is-resizing={resizing}
  class:is-drag-active={dragActive}
  style={chatDockStyle}
  role="region"
  aria-label="Workspace chat"
  onfocusin={handleChatDockFocusIn}
  onfocusout={handleChatDockFocusOut}
  ondragenter={handleChatDockDragEnter}
  ondragover={handleChatDockDragOver}
  ondragleave={handleChatDockDragLeave}
  ondrop={handleChatDockDrop}
  in:fly={{ y: 32, duration: 260 }}
  out:fly={{ y: 20, duration: 180 }}
>
  {#if !expanded}
    <button
      type="button"
      class="workspace-chat-dock__resize-handle"
      aria-label="Resize chat height"
      title="Drag to resize chat height"
      onpointerdown={handleChatDockResizePointerDown}
      ondblclick={handleChatDockResizeDoubleClick}
      onkeydown={handleChatDockResizeKeydown}
    >
      <span></span>
    </button>
  {/if}

  <div class="workspace-chat-dock__chrome">
    <div class="workspace-chat-dock__identity">
      <span class="workspace-chat-dock__kicker">Cortex chat</span>
      <span class="workspace-chat-dock__mode">
        {topLevelModeLabel}
      </span>
    </div>

    <ConstellationIconButton
      label={expanded ? 'Compact chat' : 'Expand chat'}
      title={expanded ? 'Compact chat' : 'Expand chat'}
      variant="secondary"
      size="md"
      pressed={expanded}
      onclick={toggleChatDockSize}
    >
      <ConstellationIcon
        name={expanded ? 'chevron-left' : 'side-panel'}
        size={16}
        stroke={1.9}
      />
    </ConstellationIconButton>
  </div>

  <div class="workspace-chat-dock__body">
    {#if SeamComponent}
      <SeamComponent
        context="workspace"
        topLevelMode={topLevelMode}
        selectedThreadRootId={selectedThreadRootId}
        selectedConversationId={selectedConversationId}
        selectedPreviewMemberId={selectedPreviewMemberId}
        previewMembers={previewMembers}
        onTopLevelModeChange={onTopLevelModeChange}
        onOpenRoomThread={onOpenRoomThread}
        onCloseRoomThread={onCloseRoomThread}
        onOpenConversation={onOpenConversation}
        onCloseConversation={onCloseConversation}
      />
    {/if}
  </div>
</div>
