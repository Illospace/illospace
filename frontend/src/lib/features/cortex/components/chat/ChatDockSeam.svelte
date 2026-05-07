<script lang="ts">
  import { ChatDock } from '$lib/components/chat';
  import type { ChatDockPreviewMember } from '$lib/components/chat/ChatDock.svelte';
  import { chat } from '$lib/stores/chat.svelte';

  export type CortexChatDockTopLevelMode = 'room' | 'dms';
  let {
    surface,
    context = 'workspace',
    topLevelMode = 'room',
    selectedThreadRootId = null,
    selectedConversationId = null,
    selectedPreviewMemberId = null,
    previewMembers = [],
    onTopLevelModeChange,
    onOpenRoomThread,
    onCloseRoomThread,
    onOpenConversation,
    onCloseConversation,
  }: {
    surface?: 'workspace' | 'thread';
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
  } = $props();

  const resolvedSurface = $derived(surface ?? context);
  let previousIncomingMode = $state<CortexChatDockTopLevelMode | undefined>(undefined);
  let previousIncomingConversationId = $state<string | null | undefined>(undefined);
  let previousSelectedThreadRootId = $state<string | null | undefined>(undefined);
  let previousEmittedMode = $state<CortexChatDockTopLevelMode | null>(null);
  let previousEmittedConversationId = $state<string | null | undefined>(undefined);
  let previousEmittedThreadRootId = $state<string | null | undefined>(undefined);

  $effect(() => {
    const modeChanged = previousIncomingMode !== topLevelMode;
    const conversationChanged = previousIncomingConversationId !== selectedConversationId;
    if (!modeChanged && !conversationChanged) return;

    previousIncomingMode = topLevelMode;
    previousIncomingConversationId = selectedConversationId;

    if (topLevelMode === 'room') {
      if (chat.mode !== 'room') {
        void chat.selectRoom();
      }
      return;
    }

    if (selectedConversationId) {
      if (chat.mode !== 'dms' || chat.activeConversationId !== selectedConversationId) {
        void chat.selectDm(selectedConversationId);
      }
    } else {
      if (chat.mode !== 'dms') {
        void chat.setMode('dms');
      }
    }
  });

  $effect(() => {
    if (topLevelMode !== 'room') {
      if (chat.activeThreadRootId != null) {
        chat.closeThread();
      }
      previousSelectedThreadRootId = selectedThreadRootId;
      return;
    }

    const didParentChangeThreadSelection =
      previousSelectedThreadRootId !== selectedThreadRootId;

    if (!didParentChangeThreadSelection) {
      return;
    }

    if (selectedThreadRootId) {
      const threadRootNumber = Number(selectedThreadRootId);
      previousSelectedThreadRootId = selectedThreadRootId;
      if (
        Number.isFinite(threadRootNumber) &&
        (
          chat.mode !== 'room' ||
          chat.roomSubview !== 'thread' ||
          chat.activeThreadRootId !== threadRootNumber
        )
      ) {
        void chat.openThread(threadRootNumber);
      }
      return;
    }

    previousSelectedThreadRootId = selectedThreadRootId;
    if (chat.activeThreadRootId != null) {
      chat.closeThread();
    }
  });

  $effect(() => {
    if (previousEmittedMode === chat.mode) return;
    previousEmittedMode = chat.mode;
    onTopLevelModeChange?.(chat.mode);
  });

  $effect(() => {
    if (chat.mode === 'dms' && chat.activeConversationId) {
      if (previousEmittedConversationId !== chat.activeConversationId) {
        previousEmittedConversationId = chat.activeConversationId;
        onOpenConversation?.(chat.activeConversationId);
      }
      return;
    }

    if (previousEmittedConversationId !== null) {
      previousEmittedConversationId = null;
      onCloseConversation?.();
    }
  });

  $effect(() => {
    if (chat.activeThreadRootId != null) {
      const nextThreadRootId = String(chat.activeThreadRootId);
      if (previousEmittedThreadRootId !== nextThreadRootId) {
        previousEmittedThreadRootId = nextThreadRootId;
        onOpenRoomThread?.(nextThreadRootId);
      }
      return;
    }

    if (previousEmittedThreadRootId !== null) {
      previousEmittedThreadRootId = null;
      onCloseRoomThread?.();
    }
  });
</script>

<ChatDock surface={resolvedSurface} {previewMembers} {selectedPreviewMemberId} />
