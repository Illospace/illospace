<script lang="ts">
  import { onMount, tick } from 'svelte';

  import { api, type ChatAttachmentPayload, type ChatConversationSummary, type ChatUnreadThread } from '$lib/api/client';
  import {
    mentionHandleForPerson,
    type MentionAutocompleteOption,
  } from '$lib/features/composer/domain/mentionAutocomplete';
  import {
    ConstellationIcon,
    ConstellationIconButton,
    ConstellationNotice,
    ConstellationPresenceSeed,
  } from '$lib/components/constellation';
  import type { ConstellationIconName } from '$lib/components/constellation/ConstellationIcon.svelte';
  import AttachmentPreviewDialog from '$lib/components/chat/AttachmentPreviewDialog.svelte';
  import ChatComposer from '$lib/components/chat/ChatComposer.svelte';
  import ConversationScrollCue from '$lib/components/chat/ConversationScrollCue.svelte';
  import ThreadLinkPreviewCard from '$lib/features/threads/components/ThreadLinkPreviewCard.svelte';
  import type { ChatAttachmentItem, ChatAttachmentKind } from '$lib/components/chat/chatTypes';
  import {
    CONVERSATION_SCROLL_BOTTOM_THRESHOLD,
    conversationIsNearBottom,
    scrollConversationToBottom,
    shouldShowConversationScrollCue,
  } from '$lib/components/chat/conversationScroll';
  import { auth } from '$lib/stores/auth.svelte';
  import {
    chat,
    type ChatConversationPageState,
    type ChatMessage,
    type ChatThreadPageState,
  } from '$lib/stores/chat.svelte';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import {
    ATTACHMENT_INPUT_ACCEPT,
    attachmentCanOpen,
    attachmentDetail,
    attachmentKindLabel,
    attachmentLabel,
    attachmentPreviewKind,
    attachmentUrl,
    messageLinkAttachments,
    type AttachmentPreviewKind,
  } from '$lib/utils/attachmentPreview';
  import { buildPresenceSeedStyle, normalizeHexColor, presenceToneForColor } from '$lib/utils/constellationPresence';
  import { parseServerDate } from '$lib/utils/datetime';
  import {
    dragDataIsShareable,
    droppedFilesFromDataTransfer,
    droppedTextFromDataTransfer,
    setCopyDropEffect,
  } from '$lib/utils/shareDrop';

  export type ChatDockPreviewMember = {
    id: string;
    name: string;
    email?: string | null;
    color?: string | null;
    cortex_color?: string | null;
    approved?: boolean | null;
  };

  type ChatStreamKind = 'room' | 'thread' | 'dm';

  type ChatDraftBinding = {
    key: string;
    label: string;
    textareaSelector: string;
    value: string;
    assignValue: (value: string) => void;
  };

  let {
    surface = 'workspace',
    previewMembers = [],
    selectedPreviewMemberId = null,
  }: {
    surface?: 'workspace' | 'thread';
    previewMembers?: ChatDockPreviewMember[];
    selectedPreviewMemberId?: string | null;
  } = $props();

  let roomStreamEl: HTMLDivElement | undefined = $state();
  let threadStreamEl: HTMLDivElement | undefined = $state();
  let dmStreamEl: HTMLDivElement | undefined = $state();
  let shellEl: HTMLElement | undefined = $state();
  let mainLayoutEl: HTMLDivElement | undefined = $state();
  let fileInputEl: HTMLInputElement | undefined = $state();
  let pendingAttachmentsByContext = $state<Record<string, ChatAttachmentPayload[]>>({});
  let attachmentTargetKey = $state<string | null>(null);
  let dockDragOver = $state(false);
  let dockDragTargetKey = $state<string | null>(null);
  let previewAttachment = $state<ChatAttachmentPayload | null>(null);
  let teamMembers = $state<TeamMember[]>([]);
  let roomComposerValue = $state('');
  let threadComposerValue = $state('');
  let dmComposerValue = $state('');
  let roomUserScrolledUp = $state(false);
  let threadUserScrolledUp = $state(false);
  let dmUserScrolledUp = $state(false);
  let showRoomScrollCue = $state(false);
  let showThreadScrollCue = $state(false);
  let showDmScrollCue = $state(false);
  let lastRoomScrollConversationId = $state<string | null | undefined>(undefined);
  let lastThreadScrollRootId = $state<string | null | undefined>(undefined);
  let lastDmScrollConversationId = $state<string | null | undefined>(undefined);
  let threadSplitRatio = $state(0.5);
  let resizingThreadSplit = $state(false);
  let selectedPreviewDmId = $state<string | null>(null);
  let previousSelectedPreviewMemberId = $state<string | null | undefined>(undefined);
  let lastUnreadFetchTotal = $state(0);

  type TeamMember = {
    id: string;
    name: string;
    email?: string | null;
    color?: string | null;
    cortex_color?: string | null;
    approved?: boolean | null;
    preview?: boolean;
  };

  type MessageTextSegment = {
    text: string;
    mention: boolean;
  };

  type MentionAliasEntry = {
    aliases: Set<string>;
  };

  const MENTION_RENDER_RE = /(^|[^A-Za-z0-9_])@([A-Za-z0-9._-]+)([.,:;!?]?)/g;
  const MENTION_PART_SPLIT_RE = /[^a-z0-9]+/;
  const MENTION_EMAIL_SPLIT_RE = /[._+-]+/;
  const MIN_MENTION_PREFIX_LENGTH = 2;

  const EMPTY_CONVERSATION_PAGE: ChatConversationPageState = {
    conversationId: '',
    messages: [],
    hasMore: false,
    nextBeforeSeq: null,
    loaded: false,
    loading: false,
    loadingOlder: false,
    sending: false,
    error: null,
  };

  const EMPTY_THREAD_PAGE: ChatThreadPageState = {
    rootMessageId: 0,
    conversationId: null,
    rootMessage: null,
    replies: [],
    hasMore: false,
    nextBeforeSeq: null,
    loaded: false,
    loading: false,
    loadingOlder: false,
    sending: false,
    error: null,
  };

  onMount(() => {
    void chat.setup().then(() => chat.openDock());
    void loadTeamMembers();

    return () => {
      chat.closeDock();
    };
  });

  const shellClass = $derived(
    ['chat-dock-shell', surface === 'thread' ? 'is-thread-surface' : 'is-workspace-surface']
      .filter(Boolean)
      .join(' '),
  );
  const roomConversation = $derived(chat.room);
  const roomPage = $derived(
    roomConversation ? (chat.conversationPageFor(roomConversation.id) ?? EMPTY_CONVERSATION_PAGE) : EMPTY_CONVERSATION_PAGE,
  );
  const roomMessages = $derived(roomPage.messages);
  const unreadThreads = $derived(chat.unreadThreads);
  const unreadMessageTotal = $derived(chat.unreadSummary.total);
  const unreadThreadCount = $derived(unreadThreads.length);
  const showUnreadTab = $derived(unreadMessageTotal > 0 && unreadThreadCount > 0);
  const unreadThreadCountText = $derived(
    `${unreadThreadCount} thread${unreadThreadCount === 1 ? '' : 's'}`,
  );
  const unreadThreadHeading = $derived(`${unreadThreadCount} unread ${unreadThreadCountText}`);
  const activePreviewDmMember = $derived.by<TeamMember | null>(() => {
    if (chat.mode !== 'dms' || !selectedPreviewDmId) return null;
    const previewMember = previewMembers.find(
      (member) => String(member.id) === String(selectedPreviewDmId),
    );
    if (!previewMember || previewMember.approved === false) return null;
    return {
      id: String(previewMember.id),
      name: previewMember.name,
      email: previewMember.email,
      color: previewMember.color ?? previewMember.cortex_color ?? null,
      cortex_color: previewMember.cortex_color ?? previewMember.color ?? null,
      approved: previewMember.approved,
      preview: true,
    };
  });
  const activeDmConversation = $derived(
    chat.mode === 'dms' && !activePreviewDmMember ? chat.activeConversation : null,
  );
  const activeDmPage = $derived(
    activeDmConversation ? (chat.conversationPageFor(activeDmConversation.id) ?? EMPTY_CONVERSATION_PAGE) : EMPTY_CONVERSATION_PAGE,
  );
  const activeDmMessages = $derived(activeDmPage.messages);
  const activeDmCounterpart = $derived(activeDmConversation?.counterpart ?? null);
  const activeDmDisplayName = $derived(
    activePreviewDmMember ? memberLabel(activePreviewDmMember) : conversationTitle(activeDmConversation),
  );
  const activeDmDisplayEmail = $derived(
    activePreviewDmMember?.email || activeDmCounterpart?.email || (activePreviewDmMember ? 'Preview teammate' : 'Direct message'),
  );
  const activeDmDisplayColor = $derived(
    activePreviewDmMember ? memberColor(activePreviewDmMember) : conversationColor(activeDmConversation),
  );
  const threadPage = $derived(chat.activeThreadPage ?? EMPTY_THREAD_PAGE);
  const activeThreadRoot = $derived(threadPage.rootMessage);
  const roomDraftKey = $derived('room');
  const threadDraftKey = $derived(chat.activeThreadRootId != null ? `thread:${chat.activeThreadRootId}` : null);
  const dmDraftKey = $derived(activeDmConversation ? `dm:${activeDmConversation.id}` : null);
  const roomDraft = $derived(chat.draftByContext[roomDraftKey] ?? '');
  const threadDraft = $derived(threadDraftKey ? (chat.draftByContext[threadDraftKey] ?? '') : '');
  const dmDraft = $derived(dmDraftKey ? (chat.draftByContext[dmDraftKey] ?? '') : '');
  const roomAttachments = $derived(pendingAttachmentsByContext[roomDraftKey] ?? []);
  const threadAttachments = $derived(
    threadDraftKey ? (pendingAttachmentsByContext[threadDraftKey] ?? []) : [],
  );
  const dmAttachments = $derived(dmDraftKey ? (pendingAttachmentsByContext[dmDraftKey] ?? []) : []);
  const activeChatDropTargetKey = $derived(
    chat.mode === 'dms' ? dmDraftKey : chat.mode === 'room' ? roomDraftKey : null,
  );
  const dockDropTargetLabel = $derived(dropTargetLabelForKey(dockDragTargetKey ?? activeChatDropTargetKey));
  const roomTailSignature = $derived(`${roomMessages.length}:${roomMessages.at(-1)?.id ?? 'empty'}`);
  const dmTailSignature = $derived(`${activeDmMessages.length}:${activeDmMessages.at(-1)?.id ?? 'empty'}`);
  const threadTailSignature = $derived(
    `${threadPage.replies.length}:${threadPage.replies.at(-1)?.id ?? activeThreadRoot?.id ?? 'empty'}`,
  );
  const threadSplitStyle = $derived(
    chat.roomSubview === 'thread' ? `--chat-thread-split:${(threadSplitRatio * 100).toFixed(1)}%;` : '',
  );
  const previewAttachmentUrl = $derived(previewAttachment ? attachmentUrl(previewAttachment) : '');
  const previewAttachmentLabel = $derived(previewAttachment ? attachmentLabel(previewAttachment) : '');
  const previewAttachmentDetail = $derived(previewAttachment ? attachmentDetail(previewAttachment) : '');
  const previewAttachmentKind = $derived(previewAttachment ? attachmentKind(previewAttachment) : 'file');
  const dmByCounterpartId = $derived.by(() => {
    const rows = new Map<string, ChatConversationSummary>();
    for (const conversation of chat.dms) {
      if (conversation.counterpart?.id) rows.set(String(conversation.counterpart.id), conversation);
    }
    return rows;
  });
  const signedUpTeamMembers = $derived.by(() => {
    const rows = new Map<string, TeamMember>();

    for (const member of teamMembers) {
      if (!member?.id || member.approved === false) continue;
      rows.set(String(member.id), {
        ...member,
        id: String(member.id),
        color: member.color ?? member.cortex_color ?? null,
        cortex_color: member.cortex_color ?? member.color ?? null,
      });
    }

    for (const member of previewMembers) {
      if (!member?.id || member.approved === false) continue;
      rows.set(String(member.id), {
        id: String(member.id),
        name: member.name,
        email: member.email,
        color: member.color ?? member.cortex_color ?? null,
        cortex_color: member.cortex_color ?? member.color ?? null,
        approved: member.approved,
        preview: true,
      });
    }

    const user = auth.user;
    if (user?.id && user.approved !== false) {
      const existing = rows.get(String(user.id));
      rows.set(String(user.id), {
        id: String(user.id),
        name: user.name || existing?.name || user.email || 'You',
        email: user.email || existing?.email || null,
        color: user.color || existing?.color || existing?.cortex_color || null,
        cortex_color: user.color || existing?.cortex_color || existing?.color || null,
        approved: user.approved,
      });
    }

    return Array.from(rows.values()).sort((left, right) => {
      const currentUserId = String(auth.user?.id ?? '');
      if (String(left.id) === currentUserId) return -1;
      if (String(right.id) === currentUserId) return 1;
      return memberLabel(left).localeCompare(memberLabel(right));
    });
  });
  const mentionAliasEntries = $derived.by<MentionAliasEntry[]>(() => {
    return signedUpTeamMembers.map((member) => ({
      aliases: mentionAliasesForMember(member),
    }));
  });
  const mentionAliasCounts = $derived.by(() => {
    const counts = new Map<string, number>([['illo', 1]]);
    for (const entry of mentionAliasEntries) {
      for (const alias of entry.aliases) {
        if (alias === 'illo') continue;
        counts.set(alias, (counts.get(alias) ?? 0) + 1);
      }
    }
    return counts;
  });
  const chatMentionOptions = $derived.by<MentionAutocompleteOption[]>(() => {
    return signedUpTeamMembers.map((member) => ({
      id: String(member.id),
      name: memberLabel(member),
      insertText: mentionHandleForPerson(member),
      color: memberColor(member),
      hint: member.email || 'Mention teammate',
      keywords: [member.email ?? '', member.name ?? '', memberLabel(member)]
        .filter(Boolean)
        .map(String),
    }));
  });
  const directTeamMembers = $derived.by(() => {
    return signedUpTeamMembers
      .filter((member) => member.approved !== false)
      .filter((member) => String(member.id) !== String(auth.user?.id ?? ''))
      .sort((left, right) => {
        return memberLabel(left).localeCompare(memberLabel(right));
      });
  });

  function chatStreamElement(kind: ChatStreamKind): HTMLDivElement | undefined {
    if (kind === 'dm') return dmStreamEl;
    if (kind === 'thread') return threadStreamEl;
    return roomStreamEl;
  }

  function activeVisibleStreamKind(): ChatStreamKind {
    if (chat.mode === 'dms') return 'dm';
    return 'room';
  }

  function chatStreamUserScrolledUp(kind: ChatStreamKind): boolean {
    if (kind === 'dm') return dmUserScrolledUp;
    if (kind === 'thread') return threadUserScrolledUp;
    return roomUserScrolledUp;
  }

  function setChatStreamUserScrolledUp(kind: ChatStreamKind, value: boolean) {
    if (kind === 'dm') {
      dmUserScrolledUp = value;
      return;
    }
    if (kind === 'thread') {
      threadUserScrolledUp = value;
      return;
    }
    roomUserScrolledUp = value;
  }

  function setChatStreamScrollCue(kind: ChatStreamKind, value: boolean) {
    if (kind === 'dm') {
      showDmScrollCue = value;
      return;
    }
    if (kind === 'thread') {
      showThreadScrollCue = value;
      return;
    }
    showRoomScrollCue = value;
  }

  function syncChatStreamScrollCue(kind: ChatStreamKind) {
    setChatStreamScrollCue(kind, shouldShowConversationScrollCue(chatStreamElement(kind)));
  }

  function handleChatStreamScroll(kind: ChatStreamKind) {
    const stream = chatStreamElement(kind);
    setChatStreamUserScrolledUp(
      kind,
      !conversationIsNearBottom(stream, CONVERSATION_SCROLL_BOTTOM_THRESHOLD),
    );
    syncChatStreamScrollCue(kind);
  }

  function scrollChatStreamToBottom(kind: ChatStreamKind, force = false) {
    const stream = chatStreamElement(kind);
    if (!stream) return;
    if (!force && chatStreamUserScrolledUp(kind)) return;
    scrollConversationToBottom(stream);
    requestAnimationFrame(() => {
      setChatStreamUserScrolledUp(kind, false);
      syncChatStreamScrollCue(kind);
    });
  }

  function isCornerDock() {
    return !!shellEl?.closest('.workspace-chat-dock.is-corner');
  }

  function keepVisibleStreamPinnedToBottom() {
    const kind = activeVisibleStreamKind();
    const stream = chatStreamElement(kind);
    if (!stream || !isCornerDock() || !conversationIsNearBottom(stream, 140)) return;

    let frame = 0;
    const repin = () => {
      scrollConversationToBottom(stream);
      frame += 1;
      if (frame < 14) requestAnimationFrame(repin);
    };

    tick().then(() => requestAnimationFrame(repin));
  }

  $effect(() => {
    const conversationId = roomConversation?.id == null ? null : String(roomConversation.id);
    if (conversationId === lastRoomScrollConversationId) return;
    lastRoomScrollConversationId = conversationId;
    roomUserScrolledUp = false;
    showRoomScrollCue = false;
  });

  $effect(() => {
    const threadRootId = chat.activeThreadRootId == null ? null : String(chat.activeThreadRootId);
    if (threadRootId === lastThreadScrollRootId) return;
    lastThreadScrollRootId = threadRootId;
    threadUserScrolledUp = false;
    showThreadScrollCue = false;
  });

  $effect(() => {
    const conversationId =
      activeDmConversation?.id == null
        ? (activePreviewDmMember?.id == null ? null : `preview:${activePreviewDmMember.id}`)
        : String(activeDmConversation.id);
    if (conversationId === lastDmScrollConversationId) return;
    lastDmScrollConversationId = conversationId;
    dmUserScrolledUp = false;
    showDmScrollCue = false;
  });

  $effect(() => {
    roomTailSignature;
    if (!roomStreamEl || roomPage.loadingOlder) return;
    tick().then(() => scrollChatStreamToBottom('room'));
  });

  $effect(() => {
    if (chat.roomSubview !== 'thread') return;
    threadTailSignature;
    if (!threadStreamEl || threadPage.loadingOlder) return;
    tick().then(() => scrollChatStreamToBottom('thread'));
  });

  $effect(() => {
    dmTailSignature;
    if (!dmStreamEl || activeDmPage.loadingOlder) return;
    tick().then(() => scrollChatStreamToBottom('dm'));
  });

  $effect(() => {
    if (previousSelectedPreviewMemberId === selectedPreviewMemberId) return;
    previousSelectedPreviewMemberId = selectedPreviewMemberId;
    if (!selectedPreviewMemberId) {
      selectedPreviewDmId = null;
      return;
    }
    selectedPreviewDmId = String(selectedPreviewMemberId);
    void chat.setMode('dms');
  });

  $effect(() => {
    if (chat.mode !== 'dms' && selectedPreviewDmId) {
      selectedPreviewDmId = null;
    }
  });

  $effect(() => {
    if (!chat.bootstrapped || unreadMessageTotal <= 0) {
      lastUnreadFetchTotal = 0;
      return;
    }
    if (
      chat.unreadThreadsLoading ||
      lastUnreadFetchTotal === unreadMessageTotal
    ) {
      return;
    }
    lastUnreadFetchTotal = unreadMessageTotal;
    void chat.refreshUnreadThreads(true);
  });

  $effect(() => {
    if (chat.mode !== 'unread' || chat.unreadThreadsLoading || showUnreadTab) return;
    void chat.selectRoom();
  });

  $effect(() => {
    if (roomDraft !== roomComposerValue) {
      roomComposerValue = roomDraft;
    }
  });

  $effect(() => {
    if (threadDraft !== threadComposerValue) {
      threadComposerValue = threadDraft;
    }
  });

  $effect(() => {
    if (dmDraft !== dmComposerValue) {
      dmComposerValue = dmDraft;
    }
  });

  function formatMessageTime(timestamp: string | null | undefined) {
    if (!timestamp) return '';
    try {
      const createdAt = parseServerDate(timestamp);
      if (!createdAt) return timestamp;
      const now = new Date();
      const diffMs = Math.max(0, now.getTime() - createdAt.getTime());
      const diffMinutes = Math.floor(diffMs / 60000);
      const diffHours = Math.floor(diffMs / 3600000);

      if (diffMs < 60000) return 'just now';
      if (diffMinutes < 60) return `${diffMinutes} minute${diffMinutes === 1 ? '' : 's'} ago`;
      if (diffHours < 24) return `${diffHours} hour${diffHours === 1 ? '' : 's'} ago`;

      return createdAt.toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
    } catch {
      return timestamp;
    }
  }

  function shouldShowMessageHeader(messages: ChatMessage[], index: number) {
    if (index === 0) return true;

    const current = messages[index];
    const previous = messages[index - 1];
    if (!current || !previous) return true;

    if (
      current.sender_user_id !== previous.sender_user_id ||
      current.sender_name !== previous.sender_name ||
      current.sender_kind !== previous.sender_kind
    ) {
      return true;
    }

    if (!current.created_at || !previous.created_at) return false;

    const currentDate = parseServerDate(current.created_at);
    const previousDate = parseServerDate(previous.created_at);
    if (!currentDate || !previousDate) return true;

    if (
      currentDate.getFullYear() !== previousDate.getFullYear() ||
      currentDate.getMonth() !== previousDate.getMonth() ||
      currentDate.getDate() !== previousDate.getDate()
    ) {
      return true;
    }

    return currentDate.getTime() - previousDate.getTime() > 15 * 60 * 1000;
  }

  function participantTone(color: string | null | undefined) {
    return presenceToneForColor(color);
  }

  function resolvedParticipantColor(userId: string | number | null | undefined, color: string | null | undefined) {
    const ownColor = normalizeHexColor(auth.user?.color);
    if (userId != null && auth.user?.id != null && String(userId) === String(auth.user.id) && ownColor) {
      return ownColor;
    }
    return normalizeHexColor(color);
  }

  function participantResolvedTone(userId: string | number | null | undefined, color: string | null | undefined) {
    const resolved = resolvedParticipantColor(userId, color);
    return participantTone(resolved);
  }

  function participantStyle(
    userId: string | number | null | undefined,
    color: string | null | undefined,
  ) {
    const resolved = resolvedParticipantColor(userId, color);
    return buildPresenceSeedStyle(resolved) || undefined;
  }

  function messageStyle(message: ChatMessage) {
    const resolved = resolvedParticipantColor(message.sender_user_id, message.sender_color);
    if (!resolved) return undefined;

    return [
      `--chat-message-author-color:color-mix(in srgb, ${resolved} 76%, var(--constellation-color-text-primary))`,
      `--chat-message-border:color-mix(in srgb, ${resolved} 20%, rgba(255, 255, 255, 0.08))`,
      `--chat-message-glow:color-mix(in srgb, ${resolved} 14%, transparent)`,
      `--chat-message-own-surface:color-mix(in srgb, ${resolved} 13%, rgba(8, 12, 20, 0.95))`,
    ].join('; ');
  }

  function isOwnMessage(message: ChatMessage) {
    return message.sender_user_id != null && message.sender_user_id === auth.user?.id;
  }

  function threadRootIdFor(message: ChatMessage) {
    return message.thread_root_message_id ?? message.id;
  }

  function attachmentKind(attachment: any): AttachmentPreviewKind {
    return attachmentPreviewKind(attachment);
  }

  function attachmentCanPreview(attachment: any) {
    return attachmentCanOpen(attachment);
  }

  function attachmentIconName(attachment: any): ConstellationIconName {
    const kind = attachmentKind(attachment);
    if (kind === 'image') return 'image';
    if (kind === 'video') return 'video';
    if (kind === 'pdf') return 'pdf';
    if (kind === 'link') return 'link';
    if (kind === 'archive') return 'archive';
    if (kind === 'text') return 'code';
    if (kind === 'file') return 'file';
    return 'document';
  }

  function openAttachmentPreview(event: MouseEvent, attachment: any) {
    if (!attachmentCanPreview(attachment)) return;
    event.preventDefault();
    previewAttachment = attachment;
  }

  function closeAttachmentPreview() {
    previewAttachment = null;
  }

  function shouldShowThreadSummary(message: ChatMessage) {
    return message.reply_count > 0;
  }

  function threadPreviewParticipants(message: ChatMessage) {
    return (message.thread_preview_participants ?? []).slice(0, 2);
  }

  function threadPreviewTitle(message: ChatMessage) {
    const names = threadPreviewParticipants(message).map((participant) => participant.name);
    if (names.length === 0) return 'Open thread';
    if (names.length === 1) return `Open thread with replies from ${names[0]}`;
    return `Open thread with replies from ${names.join(' and ')}`;
  }

  function threadReplyCountLabel(message: ChatMessage) {
    return `${message.reply_count} repl${message.reply_count === 1 ? 'y' : 'ies'}`;
  }

  function mentionParts(value?: string | null) {
    return (value ?? '').toLowerCase().split(MENTION_PART_SPLIT_RE).filter(Boolean);
  }

  function compactMentionAlias(value?: string | null) {
    return (value ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  }

  function mentionAliasesForMember(member: TeamMember) {
    const nameParts = mentionParts(member.name);
    const emailLocal = (member.email ?? '').split('@', 1)[0].trim().toLowerCase();
    const emailParts = emailLocal.split(MENTION_EMAIL_SPLIT_RE).filter(Boolean);
    const aliases = new Set<string>([
      (member.name ?? '').trim().toLowerCase(),
      compactMentionAlias(member.name),
      emailLocal,
      compactMentionAlias(emailLocal),
    ]);

    for (const part of nameParts) aliases.add(part);
    for (const part of emailParts) aliases.add(part);
    if (nameParts.length > 1) aliases.add(nameParts.map((part) => part[0]).join(''));
    if (emailParts.length > 1) aliases.add(emailParts.map((part) => part[0]).join(''));
    aliases.delete('');
    return aliases;
  }

  function isKnownMentionToken(rawToken: string) {
    const token = rawToken.trim().toLowerCase().replace(/[.,:;!?]+$/, '');
    if (!token) return false;
    if ((mentionAliasCounts.get(token) ?? 0) === 1) return true;
    if (token.length < MIN_MENTION_PREFIX_LENGTH) return false;

    let matchCount = 0;
    for (const entry of mentionAliasEntries) {
      for (const alias of entry.aliases) {
        if (alias.startsWith(token)) {
          matchCount += 1;
          break;
        }
      }
      if (matchCount > 1) return false;
    }
    return matchCount === 1;
  }

  function messageTextSegments(body: string): MessageTextSegment[] {
    const segments: MessageTextSegment[] = [];
    MENTION_RENDER_RE.lastIndex = 0;
    let cursor = 0;
    let match: RegExpExecArray | null;

    while ((match = MENTION_RENDER_RE.exec(body)) !== null) {
      const boundary = match[1] ?? '';
      let token = match[2] ?? '';
      let punctuation = match[3] ?? '';
      const tokenTrailingPunctuation = token.match(/[.,:;!?]+$/)?.[0] ?? '';
      if (tokenTrailingPunctuation) {
        token = token.slice(0, -tokenTrailingPunctuation.length);
        punctuation = `${tokenTrailingPunctuation}${punctuation}`;
      }
      const mentionStart = match.index + boundary.length;
      if (mentionStart > cursor) {
        segments.push({ text: body.slice(cursor, mentionStart), mention: false });
      }

      const mentionText = `@${token}`;
      segments.push({ text: mentionText, mention: isKnownMentionToken(token) });
      cursor = mentionStart + mentionText.length;

      if (punctuation) {
        segments.push({ text: punctuation, mention: false });
        cursor += punctuation.length;
      }
    }

    if (cursor < body.length) {
      segments.push({ text: body.slice(cursor), mention: false });
    }

    return segments.length > 0 ? segments : [{ text: body, mention: false }];
  }

  function showReactionsSoon() {
    ui.toast('Reactions are next up.', 'info');
  }

  function setAttachmentsForKey(key: string, attachments: ChatAttachmentPayload[]) {
    pendingAttachmentsByContext = {
      ...pendingAttachmentsByContext,
      [key]: attachments,
    };
  }

  function composerAttachmentKind(attachment: ChatAttachmentPayload): ChatAttachmentKind {
    if (attachment.kind === 'image' || attachment.kind === 'link' || attachment.kind === 'file') {
      return attachment.kind;
    }

    const previewKind = attachmentPreviewKind(attachment);
    if (previewKind === 'image' || previewKind === 'link') return previewKind;
    return 'file';
  }

  function composerAttachments(attachments: readonly ChatAttachmentPayload[]): ChatAttachmentItem[] {
    return attachments.map((attachment, index) => {
      const url = attachmentUrl(attachment);
      const previewUrl = typeof attachment.previewUrl === 'string' ? attachment.previewUrl : undefined;
      return {
        id: attachment.id ?? `${url || attachmentLabel(attachment)}-${index}`,
        kind: composerAttachmentKind(attachment),
        label: attachmentLabel(attachment),
        detail: attachmentDetail(attachment),
        url,
        previewUrl,
      };
    });
  }

  function queueAttachmentPicker(targetKey: string) {
    attachmentTargetKey = targetKey;
    fileInputEl?.click();
  }

  async function uploadFiles(files: File[], targetKey: string) {
    const results = await Promise.allSettled(files.map((file) => api.uploadFile(file)));
    const succeeded: ChatAttachmentPayload[] = [];

    for (const result of results) {
      if (result.status === 'fulfilled') {
        succeeded.push(result.value);
      } else {
        ui.toast(`Upload failed: ${result.reason?.message || 'unknown error'}`, 'error');
      }
    }

    if (succeeded.length > 0) {
      setAttachmentsForKey(targetKey, [...(pendingAttachmentsByContext[targetKey] ?? []), ...succeeded]);
    }

    return succeeded.length;
  }

  async function handleFileSelect(event: Event) {
    const input = event.currentTarget as HTMLInputElement;
    const files = input.files ? Array.from(input.files) : [];
    const targetKey = attachmentTargetKey ?? roomDraftKey;
    attachmentTargetKey = null;
    input.value = '';
    if (files.length === 0) return;
    await uploadFiles(files, targetKey);
  }

  async function handleComposerPaste(event: ClipboardEvent, targetKey: string) {
    const items = event.clipboardData?.items;
    if (!items) return;

    const files: File[] = [];
    for (const item of items) {
      if (item.kind !== 'file') continue;
      const file = item.getAsFile();
      if (file) files.push(file);
    }

    if (files.length === 0) return;
    event.preventDefault();
    await uploadFiles(files, targetKey);
  }

  function targetKeyFromDragEvent(event: DragEvent) {
    const target = event.target;
    const targetElement = target instanceof Element ? target : null;

    if (targetElement?.closest('.chat-thread-column') && threadDraftKey) return threadDraftKey;
    if (targetElement?.closest('.chat-dm-column') && dmDraftKey) return dmDraftKey;
    if (targetElement?.closest('.chat-room-column')) return roomDraftKey;

    return activeChatDropTargetKey;
  }

  function draftBindings(): ChatDraftBinding[] {
    const bindings: ChatDraftBinding[] = [
      {
        key: roomDraftKey,
        label: 'team room',
        textareaSelector: '.chat-room-column textarea:not(:disabled)',
        value: roomComposerValue,
        assignValue: (value) => {
          roomComposerValue = value;
        },
      },
    ];

    if (threadDraftKey) {
      bindings.push({
        key: threadDraftKey,
        label: 'thread reply',
        textareaSelector: '.chat-thread-column textarea:not(:disabled)',
        value: threadComposerValue,
        assignValue: (value) => {
          threadComposerValue = value;
        },
      });
    }

    if (dmDraftKey) {
      bindings.push({
        key: dmDraftKey,
        label: activeDmDisplayName || 'direct message',
        textareaSelector: '.chat-dm-column textarea:not(:disabled)',
        value: dmComposerValue,
        assignValue: (value) => {
          dmComposerValue = value;
        },
      });
    }

    return bindings;
  }

  function draftBindingForKey(targetKey: string | null) {
    if (!targetKey) return null;
    return draftBindings().find((binding) => binding.key === targetKey) ?? null;
  }

  function dropTargetLabelForKey(targetKey: string | null) {
    if (!targetKey) return 'chat';
    return draftBindingForKey(targetKey)?.label ?? 'team room';
  }

  function draftValueForKey(targetKey: string) {
    const binding = draftBindingForKey(targetKey);
    if (binding) return binding.value;
    return chat.draftByContext[targetKey] ?? '';
  }

  function setDraftValueForKey(targetKey: string, nextValue: string) {
    draftBindingForKey(targetKey)?.assignValue(nextValue);
    chat.setDraft(targetKey, nextValue);
  }

  function appendDroppedTextToDraft(targetKey: string, droppedText: string) {
    const text = droppedText.trim();
    if (!text) return false;

    const currentValue = draftValueForKey(targetKey).trimEnd();
    const nextValue = currentValue ? `${currentValue}\n${text}` : text;
    setDraftValueForKey(targetKey, nextValue);
    return true;
  }

  function composerSelectorForKey(targetKey: string) {
    return draftBindingForKey(targetKey)?.textareaSelector ?? '.chat-room-column textarea:not(:disabled)';
  }

  function focusComposerForKey(targetKey: string) {
    tick().then(() => {
      shellEl?.querySelector<HTMLTextAreaElement>(composerSelectorForKey(targetKey))?.focus();
    });
  }

  function clearDockDragState() {
    dockDragOver = false;
    dockDragTargetKey = null;
  }

  function handleDockDragEnter(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer)) return;
    event.preventDefault();
    setCopyDropEffect(event.dataTransfer);
    dockDragTargetKey = targetKeyFromDragEvent(event);
    dockDragOver = true;
  }

  function handleDockDragOver(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer)) return;
    event.preventDefault();
    setCopyDropEffect(event.dataTransfer);
    dockDragTargetKey = targetKeyFromDragEvent(event);
    dockDragOver = true;
  }

  function handleDockDragLeave(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer)) return;
    event.preventDefault();

    const currentTarget = event.currentTarget as HTMLElement;
    const rect = currentTarget.getBoundingClientRect();
    const stillInside =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    if (!stillInside) clearDockDragState();
  }

  async function handleDockDrop(event: DragEvent) {
    if (!dragDataIsShareable(event.dataTransfer)) return;
    event.preventDefault();

    const targetKey = targetKeyFromDragEvent(event);
    clearDockDragState();

    if (!targetKey) {
      ui.toast('Open a chat before dropping an attachment.', 'info');
      return;
    }

    const files = droppedFilesFromDataTransfer(event.dataTransfer);
    const uploadedCount = files.length > 0 ? await uploadFiles(files, targetKey) : 0;
    const textAdded = files.length === 0
      ? appendDroppedTextToDraft(targetKey, droppedTextFromDataTransfer(event.dataTransfer))
      : false;

    if (uploadedCount > 0 || textAdded) focusComposerForKey(targetKey);
  }

  function removeAttachment(targetKey: string, index: number) {
    setAttachmentsForKey(
      targetKey,
      (pendingAttachmentsByContext[targetKey] ?? []).filter((_, attachmentIndex) => attachmentIndex !== index),
    );
  }

  function clamp(value: number, min: number, max: number) {
    return Math.min(max, Math.max(min, value));
  }

  function updateThreadSplitRatio(clientX: number) {
    if (!mainLayoutEl) return;
    const rect = mainLayoutEl.getBoundingClientRect();
    if (rect.width <= 0) return;
    threadSplitRatio = clamp((clientX - rect.left) / rect.width, 0.32, 0.68);
  }

  function handleThreadSplitPointerDown(event: PointerEvent) {
    if (chat.roomSubview !== 'thread') return;
    resizingThreadSplit = true;
    updateThreadSplitRatio(event.clientX);
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }

  function handleThreadSplitPointerMove(event: PointerEvent) {
    if (!resizingThreadSplit) return;
    updateThreadSplitRatio(event.clientX);
  }

  function releaseThreadSplitPointer(event: PointerEvent) {
    resizingThreadSplit = false;
    const currentTarget = event.currentTarget as HTMLElement;
    if (currentTarget.hasPointerCapture(event.pointerId)) {
      currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function handleRoomComposerValueChange(value: string) {
    roomComposerValue = value;
    chat.setDraft(roomDraftKey, value);
    if (roomConversation?.id) {
      chat.sendTyping({ conversationId: roomConversation.id, threadRootMessageId: null });
    }
  }

  function handleThreadComposerValueChange(value: string) {
    if (!threadDraftKey || !activeThreadRoot) return;
    threadComposerValue = value;
    chat.setDraft(threadDraftKey, value);
    chat.sendTyping({
      conversationId: activeThreadRoot.conversation_id,
      threadRootMessageId: activeThreadRoot.id,
    });
  }

  function handleDmComposerValueChange(value: string) {
    if (!dmDraftKey || !activeDmConversation) return;
    dmComposerValue = value;
    chat.setDraft(dmDraftKey, value);
    chat.sendTyping({ conversationId: activeDmConversation.id, threadRootMessageId: null });
  }

  async function sendRoomMessage() {
    if (!roomConversation) return;
    const attachments = [...roomAttachments];
    const result = await chat.sendConversationMessage(roomConversation.id, roomComposerValue, {
      attachments,
    });
    if (result) {
      setAttachmentsForKey(roomDraftKey, []);
    }
  }

  async function sendThreadReply() {
    if (!activeThreadRoot || !threadDraftKey) return;
    const attachments = [...threadAttachments];
    const result = await chat.sendThreadReply(activeThreadRoot.id, threadComposerValue, {
      attachments,
    });
    if (result) {
      setAttachmentsForKey(threadDraftKey, []);
    }
  }

  async function sendDmMessage() {
    if (!activeDmConversation) return;
    const attachments = [...dmAttachments];
    const result = await chat.sendConversationMessage(activeDmConversation.id, dmComposerValue, {
      attachments,
    });
    if (result && dmDraftKey) {
      setAttachmentsForKey(dmDraftKey, []);
    }
  }

  async function openThread(message: ChatMessage) {
    await chat.openThread(threadRootIdFor(message));
  }

  function closeThreadPane() {
    chat.closeThread();
  }

  function loadOlderRoomMessages() {
    if (!roomConversation) return;
    void chat.loadOlderMessages(roomConversation.id);
  }

  function loadOlderThreadReplies() {
    if (chat.activeThreadRootId == null) return;
    void chat.loadOlderThreadReplies(chat.activeThreadRootId);
  }

  function loadOlderDmMessages() {
    if (!activeDmConversation) return;
    void chat.loadOlderMessages(activeDmConversation.id);
  }

  async function loadTeamMembers() {
    try {
      const members = await cortex.loadTeamMembers();
      teamMembers = (members || []).map((member: any) => ({
        id: String(member.id),
        name: member.name,
        email: member.email,
        color: member.color ?? member.cortex_color ?? null,
        cortex_color: member.cortex_color ?? member.color ?? null,
        approved: member.approved,
      }));
    } catch (err: any) {
      teamMembers = [];
    }
  }

  function conversationTitle(conversation: ChatConversationSummary | null | undefined) {
    return conversation?.counterpart?.name || conversation?.title || 'Direct message';
  }

  function conversationColor(conversation: ChatConversationSummary | null | undefined) {
    return conversation?.counterpart?.color ?? null;
  }

  function memberColor(member: TeamMember) {
    return member.color ?? member.cortex_color ?? null;
  }

  function memberLabel(member: TeamMember) {
    return member.name || member.email || 'Team member';
  }

  function memberConversation(member: TeamMember) {
    return dmByCounterpartId.get(String(member.id)) ?? null;
  }

  function memberUnreadCount(member: TeamMember) {
    return memberConversation(member)?.unread_count ?? 0;
  }

  function unreadAccentStyle(color: string | null | undefined) {
    const accent = normalizeHexColor(color);
    return accent ? `--chat-unread-accent:${accent};` : undefined;
  }

  function roomUnreadAccentStyle() {
    const lastMessage = roomConversation?.last_message;
    return unreadAccentStyle(
      resolvedParticipantColor(lastMessage?.sender_user_id, lastMessage?.sender_color),
    );
  }

  function unreadThreadAccentStyle(item: ChatUnreadThread) {
    const message = item.unread_messages.at(-1) ?? item.root_message;
    return unreadAccentStyle(
      resolvedParticipantColor(message?.sender_user_id, message?.sender_color ?? item.conversation.counterpart?.color),
    );
  }

  function unreadThreadTitle(item: ChatUnreadThread) {
    if (item.kind === 'dm') return conversationTitle(item.conversation);
    return item.conversation.title || 'Team';
  }

  function unreadThreadSubtitle(item: ChatUnreadThread) {
    const count = item.unread_count;
    const unit = unreadThreadCountsAsMessage(item)
      ? `message${count === 1 ? '' : 's'}`
      : `repl${count === 1 ? 'y' : 'ies'}`;
    return `${count} unread ${unit}`;
  }

  function unreadThreadCountsAsMessage(item: ChatUnreadThread) {
    if (item.kind === 'dm') return true;
    const rootMessageId = item.root_message?.id;
    return rootMessageId != null && item.unread_messages.some((message) => message.id === rootMessageId);
  }

  function unreadThreadDetail(item: ChatUnreadThread) {
    if (item.kind === 'dm') return item.conversation.counterpart?.email || 'Direct message';
    const root = item.root_message;
    if (!root) return 'Team room';
    return `${root.sender_name}'s thread`;
  }

  function unreadThreadOpenLabel(item: ChatUnreadThread) {
    return item.kind === 'dm' ? 'Open DM' : 'Open thread';
  }

  function unreadThreadRootId(item: ChatUnreadThread) {
    return item.root_message?.id
      ?? item.unread_messages[0]?.thread_root_message_id
      ?? item.unread_messages[0]?.id
      ?? null;
  }

  function unreadReplyMessages(item: ChatUnreadThread) {
    const rootMessageId = item.root_message?.id ?? null;
    if (item.kind === 'dm') return item.unread_messages.slice(0, 4);
    return item.unread_messages
      .filter((message) => message.id !== rootMessageId)
      .slice(0, 4);
  }

  function unreadHiddenMessageCount(item: ChatUnreadThread) {
    const renderedMessages = unreadReplyMessages(item).length;
    const rootMessageId = item.kind !== 'dm' ? item.root_message?.id : null;
    const renderedRootMessages = rootMessageId != null
      && item.unread_messages.some((message) => message.id === rootMessageId)
      ? 1
      : 0;
    const renderedUnreadCount = renderedMessages + renderedRootMessages;
    return Math.max(item.unread_count - renderedUnreadCount, 0);
  }

  function isMemberConversationActive(member: TeamMember) {
    if (member.preview) {
      return chat.mode === 'dms' && String(selectedPreviewDmId) === String(member.id);
    }
    const conversation = memberConversation(member);
    return !!conversation && chat.mode === 'dms' && chat.selectedDmId === conversation.id;
  }

  async function selectTeamChat() {
    selectedPreviewDmId = null;
    await chat.selectRoom();
  }

  async function selectUnreadChat() {
    selectedPreviewDmId = null;
    await chat.selectUnread();
  }

  async function openUnreadItem(item: ChatUnreadThread) {
    if (item.kind === 'dm') {
      selectedPreviewDmId = null;
      await chat.selectDm(item.conversation.id);
      return;
    }

    const rootMessageId = unreadThreadRootId(item);
    if (rootMessageId == null) return;
    selectedPreviewDmId = null;
    await chat.openThread(rootMessageId);
  }

  async function selectMemberDm(member: TeamMember) {
    if (member.preview) {
      selectedPreviewDmId = String(member.id);
      await chat.setMode('dms');
      return;
    }

    try {
      selectedPreviewDmId = null;
      const conversation = memberConversation(member);
      if (conversation) {
        await chat.selectDm(conversation.id);
        return;
      }
      await chat.ensureDm(String(member.id), { select: true });
    } catch (err: any) {
      ui.toast(err?.detail || err?.message || 'Could not start DM', 'error');
    }
  }
</script>

<section
  bind:this={shellEl}
  class={shellClass}
  class:is-drag-over={dockDragOver}
  data-chat-surface={surface}
  aria-label="Chat"
  onpointerenter={keepVisibleStreamPinnedToBottom}
  onfocusin={keepVisibleStreamPinnedToBottom}
  ondragenter={handleDockDragEnter}
  ondragover={handleDockDragOver}
  ondragleave={handleDockDragLeave}
  ondrop={handleDockDrop}
>
  <input
    type="file"
    accept={ATTACHMENT_INPUT_ACCEPT}
    multiple
    class="chat-hidden-file-input"
    bind:this={fileInputEl}
    onchange={handleFileSelect}
  />

  {#if dockDragOver}
    <div class="chat-drop-overlay" aria-hidden="true">
      <span class="chat-drop-overlay-icon">
        <ConstellationIcon name="attach" size={18} stroke={2} />
      </span>
      <span>{dockDropTargetLabel}</span>
    </div>
  {/if}

  <div class="chat-workspace-layout">
    <aside class="chat-channel-rail" aria-label="Conversations">
      <div class="chat-channel-list">
        {#if showUnreadTab}
          <button
            type="button"
            class="chat-channel-row chat-channel-row-unread"
            class:is-active={chat.mode === 'unread'}
            aria-label={`${unreadThreadCount} unread thread${unreadThreadCount === 1 ? '' : 's'}`}
            title={`${unreadThreadCount} unread thread${unreadThreadCount === 1 ? '' : 's'}`}
            onclick={selectUnreadChat}
          >
            <span class="chat-channel-copy">
              <span>Unread</span>
              <small>{unreadThreadCountText}</small>
            </span>
            <span class="chat-channel-unread">
              {unreadThreadCount}
            </span>
          </button>
        {/if}

        <button
          type="button"
          class="chat-channel-row chat-channel-row-team"
          class:is-active={chat.mode === 'room'}
          aria-label="Team"
          title="Team"
          onclick={selectTeamChat}
        >
          <span class="chat-channel-presence-stack" aria-hidden="true">
            {#each signedUpTeamMembers as member (member.id)}
              <ConstellationPresenceSeed
                label={memberLabel(member)}
                size="xs"
                role="user"
                tone={participantTone(memberColor(member))}
                style={buildPresenceSeedStyle(normalizeHexColor(memberColor(member))) || undefined}
              />
            {/each}
          </span>
          {#if roomConversation?.unread_count}
            <span class="chat-channel-unread" style={roomUnreadAccentStyle()}>
              {roomConversation.unread_count}
            </span>
          {/if}
          <span class="chat-channel-copy">
            <span>Team</span>
            <small>{signedUpTeamMembers.length} member{signedUpTeamMembers.length === 1 ? '' : 's'}</small>
          </span>
        </button>

        {#if directTeamMembers.length > 0}
          {#each directTeamMembers as member (member.id)}
            <button
              type="button"
              class="chat-channel-row"
              class:is-active={isMemberConversationActive(member)}
              aria-label={`Message ${memberLabel(member)}`}
              title={memberLabel(member)}
              onclick={() => selectMemberDm(member)}
            >
              <ConstellationPresenceSeed
                label={memberLabel(member)}
                size="sm"
                role="user"
                tone={participantTone(memberColor(member))}
                style={buildPresenceSeedStyle(normalizeHexColor(memberColor(member))) || undefined}
              />
              <span class="chat-channel-copy">
                <span>{memberLabel(member)}</span>
                <small>{member.preview ? 'Preview' : 'Direct message'}</small>
              </span>
              {#if memberUnreadCount(member)}
                <span class="chat-channel-unread" style={unreadAccentStyle(memberColor(member))}>
                  {memberUnreadCount(member)}
                </span>
              {/if}
            </button>
          {/each}
        {/if}
      </div>
    </aside>

    <div class="chat-workspace-conversation">
  {#if chat.mode === 'unread' && showUnreadTab}
    <section class="chat-unread-column">
      <header class="chat-unread-header">
        <div class="chat-unread-heading">
          <span class="chat-unread-kicker">Unread</span>
          <h3>{unreadThreadHeading}</h3>
          <p>Latest first.</p>
        </div>
        <button
          type="button"
          class="chat-unread-refresh"
          aria-label="Refresh unread threads"
          title="Refresh unread threads"
          onclick={() => void chat.refreshUnreadThreads(false)}
          disabled={chat.unreadThreadsLoading}
        >
          <ConstellationIcon name="refresh" size={15} stroke={1.9} />
        </button>
      </header>

      <div class="chat-unread-stream">
        {#each unreadThreads as item (`${item.kind}:${item.conversation.id}:${unreadThreadRootId(item) ?? item.latest_unread_at}`)}
          <article class="chat-unread-thread" style={unreadThreadAccentStyle(item)}>
            <button
              type="button"
              class="chat-unread-thread-open"
              aria-label={unreadThreadOpenLabel(item)}
              title={unreadThreadOpenLabel(item)}
              onclick={() => openUnreadItem(item)}
            >
              <span class="chat-unread-thread-mark" aria-hidden="true"></span>
              <span class="chat-unread-thread-copy">
                <span>
                  {unreadThreadTitle(item)}
                  <small>{unreadThreadDetail(item)}</small>
                </span>
                <span>{unreadThreadSubtitle(item)} · {formatMessageTime(item.latest_unread_at)}</span>
              </span>
              <span class="chat-unread-thread-action">
                <ConstellationIcon name="chevron-right" size={16} stroke={1.9} />
              </span>
            </button>

            {#if item.kind !== 'dm' && item.root_message}
              {@render unreadMessagePreview(item.root_message, 'root')}
            {/if}

            {#each unreadReplyMessages(item) as message (message.id)}
              {@render unreadMessagePreview(message, item.kind === 'dm' ? 'root' : 'reply')}
            {/each}

            {#if unreadHiddenMessageCount(item) > 0}
              <button
                type="button"
                class="chat-unread-more"
                onclick={() => openUnreadItem(item)}
              >
                Show {unreadHiddenMessageCount(item)} more
              </button>
            {/if}
          </article>
        {/each}
      </div>
    </section>
  {:else if chat.mode === 'dms'}
    {#if !activeDmConversation && !activePreviewDmMember}
      <div class="chat-empty-conversation">
        <span class="chat-empty-icon" aria-hidden="true">
          <ConstellationIcon name="team" size={18} stroke={1.9} />
        </span>
        <h3>Pick someone to message</h3>
        <p>Choose a person on the left to open a direct conversation.</p>
      </div>
    {:else}
      <section class="chat-dm-column">
        <header class="chat-conversation-header">
          <ConstellationPresenceSeed
            label={activeDmDisplayName}
            size="sm"
            role="user"
            tone={participantTone(activeDmDisplayColor)}
            style={buildPresenceSeedStyle(normalizeHexColor(activeDmDisplayColor)) || undefined}
            treatment="plain"
          />
          <div class="chat-conversation-heading">
            <h3>{activeDmDisplayName}</h3>
            <p>{activeDmDisplayEmail}</p>
          </div>
        </header>

        <div class="chat-pane-stream chat-dm-stream" bind:this={dmStreamEl} onscroll={() => handleChatStreamScroll('dm')}>
          {#if activeDmConversation && activeDmPage.hasMore}
            <button
              type="button"
              class="chat-load-older"
              onclick={loadOlderDmMessages}
              disabled={activeDmPage.loadingOlder}
            >
              {activeDmPage.loadingOlder ? 'Loading earlier messages…' : 'Load earlier messages'}
            </button>
          {/if}

          {#if activeDmPage.loading && activeDmMessages.length === 0}
            <div class="chat-state-row">Loading messages…</div>
          {:else if activeDmPage.error && activeDmMessages.length === 0}
            <ConstellationNotice
              title="DM could not load"
              description={activeDmPage.error}
              tone="warning"
            />
          {:else if activePreviewDmMember}
            <ConstellationNotice
              title="Preview conversation"
              description={`${memberLabel(activePreviewDmMember)} is a local preview teammate.`}
              tone="neutral"
            />
          {:else if activeDmMessages.length === 0}
            <ConstellationNotice
              title="No messages yet"
              description={`Send the first note to ${activeDmDisplayName}.`}
              tone="neutral"
            />
          {:else}
            {#each activeDmMessages as message, index (message.id)}
              <article
                class="chat-message"
                class:has-header={shouldShowMessageHeader(activeDmMessages, index)}
                class:is-continuation={!shouldShowMessageHeader(activeDmMessages, index)}
                class:is-own={isOwnMessage(message)}
                style={messageStyle(message)}
              >
                {#if shouldShowMessageHeader(activeDmMessages, index)}
                  <header class="chat-message-header">
                    <div class="chat-message-author">
                      <ConstellationPresenceSeed
                        label={message.sender_name}
                        size="sm"
                        role={message.sender_kind === 'agent' ? 'illo' : 'user'}
                        tone={participantResolvedTone(message.sender_user_id, message.sender_color)}
                        style={participantStyle(message.sender_user_id, message.sender_color)}
                        treatment="plain"
                      />

                      <div class="chat-message-author-copy">
                        <span>{message.sender_name}</span>
                        <p>{formatMessageTime(message.created_at)}</p>
                      </div>
                    </div>
                  </header>
                {/if}

                {@render threadMiniSummary(message)}

                {#if message.body}
                  {@render messageBody(message, 'chat-message-body')}
                {/if}

                {#if message.attachments?.length}
                  <div class="chat-attachments">
                    {#each message.attachments as attachment, attachmentIndex (`${message.id}-${attachment.url ?? attachment.filename ?? attachmentIndex}`)}
                      {@render attachmentCard(attachment)}
                    {/each}
                  </div>
                {/if}

                {@render messageLinkAttachmentList(message.body, message.attachments)}
                {@render threadPreviewCards(message)}
              </article>
            {/each}
          {/if}

          <ConversationScrollCue
            visible={showDmScrollCue}
            label="Jump to latest direct message"
            onclick={() => scrollChatStreamToBottom('dm', true)}
          />
        </div>

        {#if dmDraftKey}
          <div class="chat-room-composer">
            <ChatComposer
              tone="spectral"
              variant="room"
              placeholder={`Message ${conversationTitle(activeDmConversation)}…`}
              value={dmComposerValue}
              attachments={composerAttachments(dmAttachments)}
              mentionOptions={chatMentionOptions}
              disabled={activeDmPage.sending}
              loading={activeDmPage.sending}
              primaryActionLabel="Send"
              workingLabel="Sending"
              attachLabel="Attach file"
              onValueChange={handleDmComposerValueChange}
              onPaste={(event) => dmDraftKey && handleComposerPaste(event, dmDraftKey)}
              onAttach={() => dmDraftKey && queueAttachmentPicker(dmDraftKey)}
              onRemoveAttachment={(index) => dmDraftKey && removeAttachment(dmDraftKey, index)}
              onSubmit={() => void sendDmMessage()}
            />
          </div>
        {/if}
      </section>
    {/if}
  {:else if !roomConversation}
    <div class="chat-loading-row">Loading…</div>
  {:else}
    <div
      class="chat-main-layout"
      class:has-thread={chat.roomSubview === 'thread'}
      class:is-resizing={resizingThreadSplit}
      style={threadSplitStyle}
      bind:this={mainLayoutEl}
    >
      <section class="chat-room-column">
        <div class="chat-pane-stream" bind:this={roomStreamEl} onscroll={() => handleChatStreamScroll('room')}>
          {#if roomPage.hasMore}
            <button
              type="button"
              class="chat-load-older"
              onclick={loadOlderRoomMessages}
              disabled={roomPage.loadingOlder}
            >
              {roomPage.loadingOlder ? 'Loading earlier messages…' : 'Load earlier messages'}
            </button>
          {/if}

          {#if roomPage.loading && roomMessages.length === 0}
            <div class="chat-state-row">Loading messages…</div>
          {:else if roomPage.error && roomMessages.length === 0}
            <ConstellationNotice
              title="Chat could not load"
              description={roomPage.error}
              tone="warning"
            />
          {:else if roomMessages.length === 0}
            <ConstellationNotice
              title="The room is quiet"
              description="Drop the first team message to start the shared stream."
              tone="neutral"
            />
          {:else}
            {#each roomMessages as message, index (message.id)}
              <article
                class="chat-message"
                class:has-header={shouldShowMessageHeader(roomMessages, index)}
                class:is-continuation={!shouldShowMessageHeader(roomMessages, index)}
                class:is-own={isOwnMessage(message)}
                class:is-thread-active={chat.activeThreadRootId === message.id}
                style={messageStyle(message)}
              >
                <div class="chat-message-actions" aria-label="Message actions">
                  <ConstellationIconButton
                    className="chat-message-action"
                    label="Add reaction"
                    title="Add reaction"
                    variant="secondary"
                    size="md"
                    onclick={showReactionsSoon}
                  >
                    <ConstellationIcon name="reaction-add" size={16} stroke={1.9} />
                  </ConstellationIconButton>

                  <ConstellationIconButton
                    className="chat-message-action"
                    label="Reply in thread"
                    title="Reply in thread"
                    variant="secondary"
                    size="md"
                    onclick={() => openThread(message)}
                  >
                    <ConstellationIcon name="reply-thread" size={16} stroke={1.9} />
                  </ConstellationIconButton>
                </div>

                {#if shouldShowMessageHeader(roomMessages, index)}
                  <header class="chat-message-header">
                    <div class="chat-message-author">
                      <ConstellationPresenceSeed
                        label={message.sender_name}
                        size="sm"
                        role={message.sender_kind === 'agent' ? 'illo' : 'user'}
                        tone={participantResolvedTone(message.sender_user_id, message.sender_color)}
                        style={participantStyle(message.sender_user_id, message.sender_color)}
                        treatment="plain"
                      />

                      <div class="chat-message-author-copy">
                        <span>{message.sender_name}</span>
                        <p>{formatMessageTime(message.created_at)}</p>
                      </div>
                    </div>
                  </header>
                {/if}

                {@render threadMiniSummary(message)}

                {#if message.body}
                  {@render messageBody(message, 'chat-message-body')}
                {/if}

                {#if message.attachments?.length}
                  <div class="chat-attachments">
                    {#each message.attachments as attachment, attachmentIndex (`${message.id}-${attachment.url ?? attachment.filename ?? attachmentIndex}`)}
                      {@render attachmentCard(attachment)}
                    {/each}
                  </div>
                {/if}

                {@render messageLinkAttachmentList(message.body, message.attachments)}
                {@render threadPreviewCards(message)}

                {#if shouldShowThreadSummary(message)}
                  <footer class="chat-thread-summary">
                    <button
                      type="button"
                      class="chat-thread-summary-link"
                      class:is-active-thread={chat.activeThreadRootId === message.id && chat.roomSubview === 'thread'}
                      title={threadPreviewTitle(message)}
                      onclick={() => openThread(message)}
                    >
                      <span class="chat-thread-summary-leading">
                        {#if threadPreviewParticipants(message).length > 0}
                          <span class="chat-thread-preview-stack" aria-hidden="true">
                            {#each threadPreviewParticipants(message) as participant, previewIndex (participant.id)}
                              <span
                                class="chat-thread-preview-avatar"
                                style={`--chat-thread-preview-index:${previewIndex};`}
                              >
                                <ConstellationPresenceSeed
                                  label={participant.name}
                                  size="sm"
                                  role="user"
                                  tone={participantResolvedTone(participant.id, participant.color)}
                                  style={participantStyle(participant.id, participant.color)}
                                  treatment="plain"
                                />
                              </span>
                            {/each}
                          </span>
                        {/if}

                        <span class="chat-thread-summary-count">{threadReplyCountLabel(message)}</span>

                        <span class="chat-thread-summary-meta">
                          <span class="chat-thread-summary-default">
                            {#if message.last_reply_at}
                              {formatMessageTime(message.last_reply_at)}
                            {:else}
                              Open thread
                            {/if}
                          </span>
                          <span class="chat-thread-summary-hover">Open thread</span>
                        </span>
                      </span>

                      <span class="chat-thread-summary-arrow" aria-hidden="true">
                        <ConstellationIcon name="chevron-right" size={16} stroke={1.9} />
                      </span>
                    </button>
                  </footer>
                {/if}
              </article>
            {/each}
          {/if}

          <ConversationScrollCue
            visible={showRoomScrollCue}
            label="Jump to latest room message"
            onclick={() => scrollChatStreamToBottom('room', true)}
          />
        </div>

        <div class="chat-room-composer">
          <ChatComposer
            tone="spectral"
            variant="room"
            placeholder="Message…"
            value={roomComposerValue}
            attachments={composerAttachments(roomAttachments)}
            mentionOptions={chatMentionOptions}
            disabled={roomPage.sending}
            loading={roomPage.sending}
            primaryActionLabel="Send"
            workingLabel="Sending"
            attachLabel="Attach file"
            onValueChange={handleRoomComposerValueChange}
            onPaste={(event) => handleComposerPaste(event, roomDraftKey)}
            onAttach={() => queueAttachmentPicker(roomDraftKey)}
            onRemoveAttachment={(index) => removeAttachment(roomDraftKey, index)}
            onSubmit={() => void sendRoomMessage()}
          />
        </div>
      </section>

      {#if chat.roomSubview === 'thread' && activeThreadRoot}
        <button
          type="button"
          class="chat-thread-splitter"
          aria-label="Resize chat panels"
          title="Resize chat panels"
          onpointerdown={handleThreadSplitPointerDown}
          onpointermove={handleThreadSplitPointerMove}
          onpointerup={releaseThreadSplitPointer}
          onpointercancel={releaseThreadSplitPointer}
          onlostpointercapture={() => (resizingThreadSplit = false)}
        >
          <span aria-hidden="true"></span>
        </button>

        <aside class="chat-thread-column">
          <div class="chat-thread-topbar">
            <span>Thread</span>
            <button
              type="button"
              class="chat-close-thread"
              aria-label="Close thread"
              title="Close thread"
              onclick={closeThreadPane}
            >
              <ConstellationIcon name="close" size={16} stroke={1.9} />
            </button>
          </div>

          <div class="chat-thread-scroll" bind:this={threadStreamEl} onscroll={() => handleChatStreamScroll('thread')}>
            <article class="chat-root-message" style={messageStyle(activeThreadRoot)}>
              <header class="chat-message-header">
                <div class="chat-message-author">
                  <ConstellationPresenceSeed
                    label={activeThreadRoot.sender_name}
                    size="sm"
                    role={activeThreadRoot.sender_kind === 'agent' ? 'illo' : 'user'}
                    tone={participantResolvedTone(activeThreadRoot.sender_user_id, activeThreadRoot.sender_color)}
                    style={participantStyle(activeThreadRoot.sender_user_id, activeThreadRoot.sender_color)}
                    treatment="plain"
                  />

                  <div class="chat-message-author-copy">
                    <span>{activeThreadRoot.sender_name}</span>
                    <p>{formatMessageTime(activeThreadRoot.created_at)}</p>
                  </div>
                </div>
              </header>

              {#if activeThreadRoot.body}
                {@render messageBody(activeThreadRoot, 'chat-message-body')}
              {/if}

              {#if activeThreadRoot.attachments?.length}
                <div class="chat-attachments">
                  {#each activeThreadRoot.attachments as attachment, attachmentIndex (`root-${attachment.url ?? attachment.filename ?? attachmentIndex}`)}
                    {@render attachmentCard(attachment)}
                  {/each}
                </div>
              {/if}

              {@render messageLinkAttachmentList(activeThreadRoot.body, activeThreadRoot.attachments)}
              {@render threadPreviewCards(activeThreadRoot)}
            </article>

            {#if threadPage.hasMore}
              <button
                type="button"
                class="chat-load-older"
                onclick={loadOlderThreadReplies}
                disabled={threadPage.loadingOlder}
              >
                {threadPage.loadingOlder ? 'Loading earlier replies…' : 'Load earlier replies'}
              </button>
            {/if}

            {#if threadPage.loading && threadPage.replies.length === 0}
              <div class="chat-state-row">Loading thread…</div>
            {:else if threadPage.error && threadPage.replies.length === 0}
              <ConstellationNotice
                title="Thread could not load"
                description={threadPage.error}
                tone="warning"
              />
            {:else}
              {#each threadPage.replies as reply, index (reply.id)}
                <article
                  class="chat-message"
                  class:has-header={shouldShowMessageHeader(threadPage.replies, index)}
                  class:is-continuation={!shouldShowMessageHeader(threadPage.replies, index)}
                  class:is-own={isOwnMessage(reply)}
                  style={messageStyle(reply)}
                >
                  {#if shouldShowMessageHeader(threadPage.replies, index)}
                    <header class="chat-message-header">
                      <div class="chat-message-author">
                        <ConstellationPresenceSeed
                          label={reply.sender_name}
                          size="sm"
                          role={reply.sender_kind === 'agent' ? 'illo' : 'user'}
                          tone={participantResolvedTone(reply.sender_user_id, reply.sender_color)}
                          style={participantStyle(reply.sender_user_id, reply.sender_color)}
                          treatment="plain"
                        />

                        <div class="chat-message-author-copy">
                          <span>{reply.sender_name}</span>
                          <p>{formatMessageTime(reply.created_at)}</p>
                        </div>
                      </div>
                    </header>
                  {/if}

                  {#if reply.body}
                    {@render messageBody(reply, 'chat-message-body')}
                  {/if}

                  {#if reply.attachments?.length}
                    <div class="chat-attachments">
                      {#each reply.attachments as attachment, attachmentIndex (`${reply.id}-${attachment.url ?? attachment.filename ?? attachmentIndex}`)}
                        {@render attachmentCard(attachment)}
                      {/each}
                    </div>
                  {/if}

                  {@render messageLinkAttachmentList(reply.body, reply.attachments)}
                  {@render threadPreviewCards(reply)}
                </article>
              {/each}
            {/if}

            <ConversationScrollCue
              visible={showThreadScrollCue}
              label="Jump to latest thread reply"
              onclick={() => scrollChatStreamToBottom('thread', true)}
            />

            {#if threadDraftKey}
              <div class="chat-thread-composer">
                <ChatComposer
                  tone="spectral"
                  variant="thread"
                  placeholder="Reply in thread…"
                  value={threadComposerValue}
                  attachments={composerAttachments(threadAttachments)}
                  mentionOptions={chatMentionOptions}
                  disabled={threadPage.sending}
                  loading={threadPage.sending}
                  primaryActionLabel="Reply"
                  workingLabel="Sending"
                  attachLabel="Attach file"
                  onValueChange={handleThreadComposerValueChange}
                  onPaste={(event) => threadDraftKey && handleComposerPaste(event, threadDraftKey)}
                  onAttach={() => threadDraftKey && queueAttachmentPicker(threadDraftKey)}
                  onRemoveAttachment={(index) => threadDraftKey && removeAttachment(threadDraftKey, index)}
                  onSubmit={() => void sendThreadReply()}
                />
              </div>
            {/if}
          </div>
        </aside>
      {/if}
    </div>
  {/if}
    </div>
  </div>
</section>

{#snippet unreadMessagePreview(message: ChatMessage, variant: 'root' | 'reply')}
  <article
    class={`chat-unread-message is-${variant}`}
    style={messageStyle(message)}
  >
    <header class="chat-unread-message-header">
      <ConstellationPresenceSeed
        label={message.sender_name}
        size="sm"
        role={message.sender_kind === 'agent' ? 'illo' : 'user'}
        tone={participantResolvedTone(message.sender_user_id, message.sender_color)}
        style={participantStyle(message.sender_user_id, message.sender_color)}
        treatment="plain"
      />
      <div class="chat-unread-message-meta">
        <span>{message.sender_name}</span>
        <small>{formatMessageTime(message.created_at)}</small>
      </div>
    </header>

    {@render threadMiniSummary(message)}

    {#if message.body}
      {@render messageBody(message, 'chat-unread-message-body')}
    {/if}

    {#if message.attachments?.length}
      <div class="chat-attachments">
        {#each message.attachments as attachment, attachmentIndex (`unread-${message.id}-${attachment.url ?? attachment.filename ?? attachmentIndex}`)}
          {@render attachmentCard(attachment)}
        {/each}
      </div>
    {/if}

    {@render messageLinkAttachmentList(message.body, message.attachments)}
    {@render threadPreviewCards(message)}
  </article>
{/snippet}

{#snippet messageBody(message: ChatMessage, className: string)}
  <p class={className}>
    {#each messageTextSegments(message.body) as segment, segmentIndex (segmentIndex)}
      {#if segment.mention}
        <span class="chat-mention">{segment.text}</span>
      {:else}
        {segment.text}
      {/if}
    {/each}
  </p>
{/snippet}

{#snippet threadMiniSummary(message: ChatMessage)}
  {#if shouldShowThreadSummary(message)}
    <button
      type="button"
      class="chat-thread-mini-summary"
      title={threadPreviewTitle(message)}
      onclick={() => openThread(message)}
    >
      <span class="chat-thread-mini-count">{threadReplyCountLabel(message)}</span>
    </button>
  {/if}
{/snippet}

{#snippet attachmentPreviewVisual(attachment: any)}
  {@const kind = attachmentKind(attachment)}
  <span class={`chat-attachment-preview is-${kind}`} aria-hidden="true">
    {#if kind === 'image'}
      <img src={attachmentUrl(attachment)} alt="" loading="lazy" />
    {:else if kind === 'video'}
      <video src={attachmentUrl(attachment)} muted playsinline preload="metadata"></video>
    {:else}
      <span class="chat-attachment-preview-icon">
        <ConstellationIcon name={attachmentIconName(attachment)} size={28} stroke={1.7} />
      </span>
      <span class="chat-attachment-preview-label">{attachmentKindLabel(attachment)}</span>
    {/if}
  </span>
{/snippet}

{#snippet attachmentCard(attachment: any)}
  {@const kind = attachmentKind(attachment)}
  {@const previewable = attachmentCanPreview(attachment)}
  {#if previewable}
    <button
      type="button"
      class={`chat-attachment is-${kind} is-previewable`}
      aria-label={`Preview ${attachmentLabel(attachment)}`}
      onclick={(event) => openAttachmentPreview(event, attachment)}
    >
      {@render attachmentPreviewVisual(attachment)}

      <span class="chat-attachment-copy">
        <strong>{attachmentLabel(attachment)}</strong>
        {#if attachmentDetail(attachment)}
          <span>{attachmentDetail(attachment)}</span>
        {/if}
      </span>
    </button>
  {:else}
    <div class={`chat-attachment is-${kind}`}>
      {@render attachmentPreviewVisual(attachment)}

      <span class="chat-attachment-copy">
        <strong>{attachmentLabel(attachment)}</strong>
        {#if attachmentDetail(attachment)}
          <span>{attachmentDetail(attachment)}</span>
        {/if}
      </span>
    </div>
  {/if}
{/snippet}

{#snippet messageLinkAttachmentList(body: string | null | undefined, attachments: ChatAttachmentPayload[] | null | undefined)}
  {@const linkAttachments = messageLinkAttachments(body, attachments)}
  {#if linkAttachments.length}
    <div class="chat-attachments chat-link-attachments">
      {#each linkAttachments as attachment (attachment.url)}
        {@render attachmentCard(attachment)}
      {/each}
    </div>
  {/if}
{/snippet}

{#snippet threadPreviewCards(message: ChatMessage)}
  {#if message.thread_references?.length}
    <div class="chat-thread-link-previews">
      {#each message.thread_references as reference (`${message.id}-${reference.thread_id ?? reference.original_ref ?? reference.url}`)}
        <ThreadLinkPreviewCard {reference} compact />
      {/each}
    </div>
  {/if}
{/snippet}

{#if previewAttachment && previewAttachmentUrl}
  <AttachmentPreviewDialog
    url={previewAttachmentUrl}
    label={previewAttachmentLabel}
    detail={previewAttachmentDetail}
    kind={previewAttachmentKind}
    fallbackIcon={attachmentIconName(previewAttachment)}
    onClose={closeAttachmentPreview}
  />
{/if}

<style>
.chat-hidden-file-input {
  display: none;
}

.chat-dock-shell {
  --chat-attachment-image-max-height: min(44vh, 360px);
  --chat-shell-border: rgba(255, 255, 255, 0.06);
  --chat-shell-background: rgba(9, 12, 19, 0.96);
  --chat-shell-text: rgba(244, 246, 252, 0.95);
  --chat-shell-shadow: none;
  --chat-meta-text: rgba(240, 240, 250, 0.56);
  --chat-rail-border: rgba(255, 255, 255, 0.08);
  --chat-channel-hover-background: rgba(255, 255, 255, 0.035);
  --chat-channel-cue-background: color-mix(in srgb, var(--constellation-color-spectral) 14%, transparent);
  --chat-channel-title: rgba(244, 246, 252, 0.88);
  --chat-channel-meta: rgba(240, 240, 250, 0.44);
  --chat-empty-icon-border: rgba(255, 255, 255, 0.08);
  --chat-empty-icon-background: rgba(255, 255, 255, 0.035);
  --chat-empty-icon-text: rgba(234, 239, 250, 0.8);
  --chat-heading-text: rgba(244, 246, 252, 0.95);
  --chat-description-text: rgba(240, 240, 250, 0.56);
  --chat-unread-accent: var(--thread-accent, var(--constellation-color-user-accent, #57CFA0));
  --chat-unread-background: color-mix(in srgb, var(--chat-unread-accent) 90%, transparent);
  --chat-unread-text: rgba(12, 10, 8, 0.92);
  --chat-unread-ring: rgba(9, 12, 19, 0.96);
  --chat-unread-thread-border: rgba(255, 255, 255, 0.08);
  --chat-unread-thread-background: rgba(255, 255, 255, 0.025);
  --chat-unread-thread-background-hover: rgba(255, 255, 255, 0.045);
  --chat-unread-thread-mark: color-mix(in srgb, var(--chat-unread-accent) 82%, rgba(255, 255, 255, 0.18));
  --chat-thread-border: rgba(255, 255, 255, 0.08);
  --chat-thread-splitter-background: rgba(255, 255, 255, 0.12);
  --chat-thread-splitter-hover-background: rgba(141, 183, 255, 0.42);
  --chat-action-border: rgba(255, 255, 255, 0.08);
  --chat-action-background: rgba(255, 255, 255, 0.03);
  --chat-action-background-hover: rgba(255, 255, 255, 0.06);
  --chat-action-text: rgba(229, 234, 244, 0.84);
  --chat-root-message-border: rgba(255, 255, 255, 0.08);
  --chat-message-active-background: rgba(255, 255, 255, 0.03);
  --chat-message-active-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.04);
  --chat-message-hover-background: rgba(255, 255, 255, 0.025);
  --chat-message-hover-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
  --chat-message-meta-text: rgba(240, 240, 250, 0.5);
  --chat-message-body-text: rgba(246, 248, 253, 0.95);
  --chat-attachment-border: var(--constellation-chat-attachment-border);
  --chat-attachment-background: var(--constellation-chat-attachment-background);
  --chat-attachment-hover-border: rgba(255, 255, 255, 0.14);
  --chat-attachment-hover-background: rgba(255, 255, 255, 0.05);
  --chat-attachment-preview-background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.06), transparent 48%),
    rgba(0, 0, 0, 0.2);
  --chat-attachment-preview-text: rgba(240, 240, 250, 0.8);
  --chat-attachment-icon-border: rgba(255, 255, 255, 0.08);
  --chat-attachment-icon-background: rgba(255, 255, 255, 0.05);
  --chat-attachment-icon-text: rgba(236, 240, 248, 0.88);
  --chat-attachment-label-background: rgba(0, 0, 0, 0.36);
  --chat-attachment-label-text: rgba(246, 248, 253, 0.9);
  --chat-preview-backdrop-background: rgba(6, 9, 16, 0.72);
  --chat-preview-panel-border: rgba(255, 255, 255, 0.1);
  --chat-preview-panel-background:
    linear-gradient(180deg, rgba(16, 20, 30, 0.94), rgba(8, 12, 19, 0.92)),
    rgba(8, 12, 19, 0.92);
  --chat-preview-panel-shadow:
    0 28px 90px rgba(0, 0, 0, 0.46),
    0 0 0 1px rgba(255, 255, 255, 0.03) inset;
  --chat-preview-toolbar-border: rgba(255, 255, 255, 0.08);
  --chat-preview-meta-title: rgba(246, 248, 253, 0.94);
  --chat-preview-meta-text: rgba(240, 240, 250, 0.56);
  --chat-preview-frame-background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.035), transparent 45%),
    rgba(0, 0, 0, 0.24);
  --chat-file-fallback-text: rgba(240, 240, 250, 0.7);
  --chat-thread-summary-text: rgba(240, 240, 250, 0.56);
  --chat-thread-summary-link-text: rgba(208, 216, 232, 0.78);
  --chat-thread-summary-link-background-hover: rgba(255, 255, 255, 0.04);
  --chat-thread-summary-link-text-hover: rgba(228, 236, 249, 0.92);
  --chat-thread-summary-accent: rgba(150, 188, 255, 0.94);
  --chat-mention-background: rgba(150, 188, 255, 0.16);
  --chat-mention-text: rgba(207, 224, 255, 0.98);
  --chat-presence-seed-core-base: #050910;
  --chat-presence-seed-owner-base: rgba(246, 248, 253, 0.96);
  --chat-presence-seed-ring: rgba(255, 255, 255, 0.12);
  --chat-message-actions-border: rgba(255, 255, 255, 0.08);
  --chat-message-actions-background: rgba(8, 11, 18, 0.96);
  --chat-message-actions-shadow: 0 12px 28px rgba(0, 0, 0, 0.28);
  --chat-message-actions-text: rgba(236, 240, 248, 0.86);
  --chat-drop-overlay-border: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 42%, transparent);
  --chat-drop-overlay-background: rgba(14, 17, 24, 0.72);
  --chat-drop-overlay-text: rgba(246, 239, 224, 0.94);
  --chat-drop-overlay-icon-background: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 20%, transparent);
  --chat-drop-overlay-shadow:
    0 18px 42px rgba(0, 0, 0, 0.26),
    inset 0 0 0 1px rgba(255, 255, 255, 0.04);
}

.chat-thread-link-previews {
  display: grid;
  gap: 8px;
  margin-top: 8px;
}

.chat-dock-shell {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 0;
  container-type: inline-size;
  container-name: chat-dock;
  min-height: 0;
  height: 100%;
  padding: 14px 16px 16px;
  border-radius: 22px;
  border: 1px solid var(--chat-shell-border);
  background: var(--chat-shell-background);
  color: var(--chat-shell-text);
  box-shadow: var(--chat-shell-shadow);
  isolation: isolate;
}

.chat-dock-shell.is-workspace-surface {
  border-color: transparent;
  background: transparent;
  box-shadow: none;
}

.chat-drop-overlay {
  position: absolute;
  inset: 10px;
  z-index: 20;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  border: 1px solid var(--chat-drop-overlay-border);
  border-radius: 18px;
  background: var(--chat-drop-overlay-background);
  color: var(--chat-drop-overlay-text);
  font-family: var(--constellation-font-sans);
  font-size: 12px;
  font-weight: 640;
  line-height: 1;
  pointer-events: none;
  box-shadow: var(--chat-drop-overlay-shadow);
  backdrop-filter: blur(10px) saturate(1.04);
  -webkit-backdrop-filter: blur(10px) saturate(1.04);
}

.chat-drop-overlay-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 999px;
  background: var(--chat-drop-overlay-icon-background);
}

:global(:root[data-color-scheme='light']) .chat-dock-shell {
  --chat-shell-border: rgba(24, 35, 49, 0.08);
  --chat-shell-background: rgba(247, 250, 253, 0.94);
  --chat-shell-text: rgba(17, 24, 35, 0.94);
  --chat-shell-shadow:
    0 18px 40px rgba(24, 35, 49, 0.08),
    inset 0 1px 0 rgba(255, 255, 255, 0.42);
  --chat-meta-text: rgba(78, 91, 108, 0.62);
  --chat-rail-border: rgba(24, 35, 49, 0.08);
  --chat-channel-hover-background: rgba(255, 255, 255, 0.48);
  --chat-channel-cue-background: rgba(83, 121, 184, 0.14);
  --chat-channel-title: rgba(17, 24, 35, 0.88);
  --chat-channel-meta: rgba(78, 91, 108, 0.56);
  --chat-empty-icon-border: rgba(24, 35, 49, 0.08);
  --chat-empty-icon-background: rgba(255, 255, 255, 0.52);
  --chat-empty-icon-text: rgba(45, 57, 73, 0.8);
  --chat-heading-text: rgba(17, 24, 35, 0.92);
  --chat-description-text: rgba(78, 91, 108, 0.58);
  --chat-unread-background: color-mix(in srgb, var(--chat-unread-accent) 88%, transparent);
  --chat-unread-text: rgba(28, 20, 12, 0.9);
  --chat-unread-ring: rgba(247, 250, 253, 0.94);
  --chat-unread-thread-border: rgba(24, 35, 49, 0.08);
  --chat-unread-thread-background: rgba(255, 255, 255, 0.46);
  --chat-unread-thread-background-hover: rgba(255, 255, 255, 0.72);
  --chat-unread-thread-mark: color-mix(in srgb, var(--chat-unread-accent) 76%, rgba(24, 35, 49, 0.12));
  --chat-thread-border: rgba(24, 35, 49, 0.08);
  --chat-thread-splitter-background: rgba(24, 35, 49, 0.12);
  --chat-thread-splitter-hover-background: rgba(95, 137, 200, 0.42);
  --chat-action-border: rgba(24, 35, 49, 0.08);
  --chat-action-background: rgba(255, 255, 255, 0.52);
  --chat-action-background-hover: rgba(255, 255, 255, 0.72);
  --chat-action-text: rgba(45, 57, 73, 0.84);
  --chat-root-message-border: rgba(24, 35, 49, 0.08);
  --chat-message-active-background: rgba(255, 255, 255, 0.48);
  --chat-message-active-shadow: inset 0 0 0 1px rgba(24, 35, 49, 0.06);
  --chat-message-hover-background: rgba(255, 255, 255, 0.42);
  --chat-message-hover-shadow: inset 0 0 0 1px rgba(24, 35, 49, 0.04);
  --chat-message-meta-text: rgba(78, 91, 108, 0.58);
  --chat-message-body-text: rgba(33, 44, 58, 0.92);
  --chat-attachment-border: var(--constellation-chat-attachment-border);
  --chat-attachment-background: var(--constellation-chat-attachment-background);
  --chat-attachment-hover-border: rgba(83, 121, 184, 0.2);
  --chat-attachment-hover-background: rgba(255, 255, 255, 0.7);
  --chat-attachment-preview-background:
    linear-gradient(135deg, rgba(83, 121, 184, 0.08), transparent 48%),
    rgba(17, 24, 35, 0.06);
  --chat-attachment-preview-text: rgba(45, 57, 73, 0.84);
  --chat-attachment-icon-border: rgba(24, 35, 49, 0.08);
  --chat-attachment-icon-background: rgba(255, 255, 255, 0.56);
  --chat-attachment-icon-text: rgba(45, 57, 73, 0.84);
  --chat-attachment-label-background: rgba(255, 255, 255, 0.72);
  --chat-attachment-label-text: rgba(17, 24, 35, 0.8);
  --chat-preview-backdrop-background: rgba(234, 241, 247, 0.78);
  --chat-preview-panel-border: rgba(24, 35, 49, 0.1);
  --chat-preview-panel-background:
    linear-gradient(180deg, rgba(252, 254, 255, 0.96), rgba(241, 247, 251, 0.94)),
    rgba(247, 250, 253, 0.94);
  --chat-preview-panel-shadow:
    0 28px 90px rgba(24, 35, 49, 0.2),
    0 0 0 1px rgba(255, 255, 255, 0.6) inset;
  --chat-preview-toolbar-border: rgba(24, 35, 49, 0.08);
  --chat-preview-meta-title: rgba(17, 24, 35, 0.92);
  --chat-preview-meta-text: rgba(78, 91, 108, 0.6);
  --chat-preview-frame-background:
    linear-gradient(135deg, rgba(83, 121, 184, 0.05), transparent 48%),
    rgba(17, 24, 35, 0.06);
  --chat-file-fallback-text: rgba(78, 91, 108, 0.62);
  --chat-thread-summary-text: rgba(78, 91, 108, 0.58);
  --chat-thread-summary-link-text: rgba(61, 76, 95, 0.82);
  --chat-thread-summary-link-background-hover: rgba(255, 255, 255, 0.52);
  --chat-thread-summary-link-text-hover: rgba(17, 24, 35, 0.92);
  --chat-thread-summary-accent: #486fa8;
  --chat-mention-background: rgba(72, 111, 168, 0.14);
  --chat-mention-text: #315a91;
  --chat-presence-seed-core-base: rgba(246, 250, 253, 0.96);
  --chat-presence-seed-owner-base: rgba(19, 28, 40, 0.94);
  --chat-presence-seed-ring: rgba(24, 35, 49, 0.12);
  --chat-message-actions-border: rgba(24, 35, 49, 0.08);
  --chat-message-actions-background: rgba(247, 250, 253, 0.96);
  --chat-message-actions-shadow: 0 12px 28px rgba(24, 35, 49, 0.12);
  --chat-message-actions-text: rgba(45, 57, 73, 0.84);
  --chat-drop-overlay-border: rgba(135, 100, 52, 0.28);
  --chat-drop-overlay-background: rgba(250, 250, 246, 0.78);
  --chat-drop-overlay-text: rgba(45, 57, 73, 0.92);
  --chat-drop-overlay-icon-background: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 18%, transparent);
  --chat-drop-overlay-shadow:
    0 18px 42px rgba(24, 35, 49, 0.14),
    inset 0 0 0 1px rgba(255, 255, 255, 0.52);
}

.chat-dock-shell.is-thread-surface {
  border-radius: 18px;
  padding: 16px;
}

.chat-dock-shell :global(.constellation-presence-seed:not(.is-illo)) {
  --constellation-presence-seed-user-core-accent-strength: 78%;
  --constellation-presence-seed-user-core-base: var(--chat-presence-seed-core-base);
  --constellation-presence-seed-user-owner-accent-strength: 24%;
  --constellation-presence-seed-user-owner-base: var(--chat-presence-seed-owner-base);
}

.chat-dock-shell :global(.constellation-presence-seed:not(.is-illo) .constellation-presence-seed-core) {
  border-color: color-mix(in srgb, var(--seed-accent) 44%, var(--chat-presence-seed-ring));
  box-shadow:
    inset 0 0 0 1px color-mix(in srgb, var(--seed-accent) 20%, transparent),
    0 0 15px color-mix(in srgb, var(--seed-accent) 24%, transparent);
}

.chat-thread-topbar span {
  margin: 0;
  color: var(--chat-meta-text);
  font-family: var(--constellation-font-mono);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.16em;
  line-height: 1.2;
  text-transform: uppercase;
}

.chat-workspace-layout {
  display: grid;
  grid-template-columns: clamp(178px, 19vw, 236px) minmax(0, 1fr);
  gap: 16px;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}

.chat-channel-rail,
.chat-workspace-conversation {
  min-width: 0;
  min-height: 0;
}

.chat-channel-rail {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
  padding-right: 12px;
  border-right: 1px solid var(--chat-rail-border);
}

.chat-channel-list {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
  width: 100%;
  min-height: 0;
  overflow: auto;
  padding: 2px 0 2px 2px;
}

.chat-channel-row {
  appearance: none;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 10px;
  width: 100%;
  height: 50px;
  min-height: 50px;
  padding: 0 10px;
  border: 0;
  border-radius: 16px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  isolation: isolate;
  transition:
    transform 150ms ease;
}

.chat-channel-row::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: var(--chat-channel-cue-background);
  opacity: 0;
  filter: none;
  transition:
    opacity 150ms ease,
    transform 150ms ease;
  z-index: -1;
}

.chat-channel-row:hover,
  .chat-channel-row:focus-visible,
  .chat-channel-row.is-active {
  background: var(--chat-channel-hover-background);
}

.chat-channel-row:hover {
  transform: translateX(1px);
}

.chat-channel-row:hover::before,
.chat-channel-row:focus-visible::before,
.chat-channel-row.is-active::before {
  opacity: 0.72;
  transform: none;
}

.chat-channel-row:focus-visible {
  outline: 2px solid var(--constellation-control-focus-ring);
  outline-offset: 2px;
}

.chat-channel-row-team {
  height: 54px;
  min-height: 54px;
}

.chat-channel-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 36px;
  width: 36px;
  height: 36px;
  border-radius: 12px;
  background: var(--chat-channel-cue-background);
  color: var(--chat-channel-title);
}

.chat-channel-presence-stack {
  display: flex;
  flex: 0 0 38px;
  flex-wrap: wrap;
  align-content: center;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  overflow: visible;
}

.chat-channel-presence-stack :global(.constellation-presence-seed) {
  margin: -2px;
}

.chat-channel-row :global(.constellation-presence-seed) {
  filter: saturate(1.04) brightness(1.02);
  transition: filter 150ms ease;
}

.chat-channel-row:hover :global(.constellation-presence-seed),
.chat-channel-row:focus-visible :global(.constellation-presence-seed),
.chat-channel-row.is-active :global(.constellation-presence-seed) {
  filter: saturate(1.16) brightness(1.1);
}

.chat-channel-copy {
  display: grid;
  gap: 3px;
  min-width: 0;
  text-align: left;
}

.chat-channel-copy span,
.chat-channel-copy small {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-channel-copy span {
  color: var(--chat-channel-title);
  font-size: 13px;
  font-weight: 620;
  line-height: 1.08;
}

.chat-channel-copy small {
  color: var(--chat-channel-meta);
  font-size: 10.5px;
  font-weight: 520;
  line-height: 1.1;
}

.chat-empty-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--chat-empty-icon-border);
  border-radius: 999px;
  background: var(--chat-empty-icon-background);
  color: var(--chat-empty-icon-text);
}

.chat-channel-unread {
  position: absolute;
  top: -3px;
  right: -3px;
  min-width: 19px;
  padding: 3px 5px;
  border-radius: 999px;
  background: var(--chat-unread-background);
  color: var(--chat-unread-text);
  font-family: var(--constellation-font-mono);
  font-size: 10px;
  font-weight: 800;
  line-height: 1;
  text-align: center;
  box-shadow: 0 0 0 2px var(--chat-unread-ring);
}

.chat-workspace-conversation {
  display: flex;
  flex-direction: column;
}

.chat-dm-column {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}

.chat-conversation-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 2px 12px 12px;
}

.chat-conversation-heading {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.chat-conversation-heading h3,
.chat-conversation-heading p,
.chat-empty-conversation h3,
.chat-empty-conversation p {
  margin: 0;
}

.chat-conversation-heading h3 {
  min-width: 0;
  overflow: hidden;
  color: var(--chat-heading-text);
  font-size: 15px;
  font-weight: 600;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-conversation-heading p {
  min-width: 0;
  overflow: hidden;
  color: var(--chat-description-text);
  font-size: 11px;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-empty-conversation {
  display: grid;
  place-items: center;
  align-content: center;
  gap: 10px;
  flex: 1 1 auto;
  min-height: 240px;
  padding: 24px;
  color: var(--chat-description-text);
  text-align: center;
}

.chat-empty-conversation h3 {
  color: var(--chat-heading-text);
  font-size: 16px;
  font-weight: 600;
  line-height: 1.2;
}

.chat-empty-conversation p {
  max-width: 300px;
  font-size: 13px;
  line-height: 1.45;
}

.chat-unread-column {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.chat-unread-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 2px 12px 14px;
}

.chat-unread-heading {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.chat-unread-heading h3,
.chat-unread-heading p {
  margin: 0;
}

.chat-unread-kicker {
  color: var(--chat-meta-text);
  font-family: var(--constellation-font-mono);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.14em;
  line-height: 1.1;
  text-transform: uppercase;
}

.chat-unread-heading h3 {
  color: var(--chat-heading-text);
  font-size: 15px;
  font-weight: 640;
  line-height: 1.2;
}

.chat-unread-heading p {
  color: var(--chat-description-text);
  font-size: 11px;
  line-height: 1.25;
}

.chat-unread-refresh,
.chat-unread-more {
  appearance: none;
  border: 1px solid var(--chat-action-border);
  background: var(--chat-action-background);
  color: var(--chat-action-text);
  cursor: pointer;
  transition:
    background-color 150ms ease,
    border-color 150ms ease,
    transform 150ms ease,
    opacity 150ms ease;
}

.chat-unread-refresh {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 32px;
  height: 32px;
  padding: 0;
  border-radius: 10px;
}

.chat-unread-refresh:hover,
.chat-unread-more:hover {
  background: var(--chat-action-background-hover);
}

.chat-unread-refresh:focus-visible,
.chat-unread-more:focus-visible,
.chat-unread-thread-open:focus-visible {
  outline: 2px solid var(--constellation-control-focus-ring);
  outline-offset: 2px;
}

.chat-unread-stream {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 14px;
  min-height: 0;
  overflow: auto;
  padding: 0 12px 12px;
}

.chat-unread-thread {
  display: grid;
  gap: 10px;
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--chat-unread-thread-border);
  border-radius: 16px;
  background: var(--chat-unread-thread-background);
  transition:
    background-color 150ms ease,
    border-color 150ms ease;
}

.chat-unread-thread:hover {
  background: var(--chat-unread-thread-background-hover);
}

.chat-unread-thread-open {
  appearance: none;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
}

.chat-unread-thread-mark {
  width: 9px;
  height: 9px;
  border-radius: 999px;
  background: var(--chat-unread-thread-mark);
  box-shadow: 0 0 14px color-mix(in srgb, var(--chat-unread-accent) 34%, transparent);
}

.chat-unread-thread-copy {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.chat-unread-thread-copy > span {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
  overflow: hidden;
  color: var(--chat-heading-text);
  font-size: 13px;
  font-weight: 650;
  line-height: 1.18;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-unread-thread-copy small {
  min-width: 0;
  overflow: hidden;
  color: var(--chat-description-text);
  font-size: 11px;
  font-weight: 520;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-unread-thread-copy > span + span {
  color: var(--chat-meta-text);
  font-size: 11px;
  font-weight: 520;
}

.chat-unread-thread-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  color: var(--chat-action-text);
  opacity: 0.68;
  transition:
    opacity 150ms ease,
    transform 150ms ease;
}

.chat-unread-thread-open:hover .chat-unread-thread-action {
  opacity: 1;
  transform: translateX(2px);
}

.chat-unread-message {
  display: grid;
  gap: 7px;
  min-width: 0;
  padding: 4px 0 2px 19px;
}

.chat-unread-message.is-reply {
  margin-left: 18px;
  padding-left: 14px;
  border-left: 1px solid var(--chat-thread-border);
}

.chat-unread-message-header {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.chat-unread-message-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.chat-unread-message-meta span {
  color: var(--chat-message-author-color, var(--chat-heading-text));
  font-size: 13px;
  font-weight: 620;
  line-height: 1.15;
}

.chat-unread-message-meta small {
  color: var(--chat-message-meta-text);
  font-size: 11px;
  line-height: 1.2;
}

.chat-unread-message-body {
  margin: 0;
  color: var(--chat-message-body-text);
  font-size: 14px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.chat-unread-more {
  justify-self: start;
  margin-left: 19px;
  padding: 7px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 620;
  line-height: 1;
}

.chat-dm-stream {
  padding-top: 4px;
}

  .chat-loading-row {
    padding: 8px 0 2px;
    color: var(--chat-meta-text);
    font-size: 12px;
  }

  .chat-main-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
    flex: 1 1 auto;
    min-height: 0;
    padding-top: 6px;
  }

  .chat-main-layout.has-thread {
    grid-template-columns:
      minmax(0, calc(var(--chat-thread-split, 50%) - 6px))
      12px
      minmax(0, calc(100% - var(--chat-thread-split, 50%) - 6px));
  }

  .chat-main-layout.is-resizing {
    user-select: none;
  }

  .chat-room-column,
  .chat-thread-column {
    display: flex;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
  }

  .chat-thread-column {
    padding-left: 18px;
    border-left: 1px solid var(--chat-thread-border);
  }

  .chat-thread-splitter {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 12px;
    padding: 0;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: col-resize;
  }

  .chat-thread-splitter span {
    width: 3px;
    height: 72px;
    border-radius: 999px;
    background: var(--chat-thread-splitter-background);
    transition:
      background-color 140ms ease,
      transform 140ms ease;
  }

  .chat-thread-splitter:hover span,
  .chat-main-layout.is-resizing .chat-thread-splitter span {
    background: var(--chat-thread-splitter-hover-background);
    transform: scaleX(1.08);
  }

  .chat-thread-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 2px 0 14px;
  }

  .chat-close-thread,
  .chat-load-older,
  .chat-message-action {
    appearance: none;
    border: 0;
    cursor: pointer;
    transition:
      transform 160ms ease,
      background-color 160ms ease,
      border-color 160ms ease,
      opacity 160ms ease;
  }

  .chat-load-older {
    padding: 9px 12px;
    border-radius: 999px;
    border: 1px solid var(--chat-action-border);
    background: var(--chat-action-background);
    color: var(--chat-action-text);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .chat-close-thread {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex: 0 0 auto;
    width: 30px;
    height: 30px;
    padding: 0;
    border-radius: 10px;
    border: 1px solid var(--chat-action-border);
    background: var(--chat-action-background);
    color: var(--chat-action-text);
    line-height: 1;
  }

  .chat-close-thread :global(svg) {
    width: 16px;
    height: 16px;
  }

  .chat-load-older {
    align-self: center;
    margin-bottom: 10px;
  }

  .chat-close-thread:hover,
  .chat-load-older:hover {
    background: var(--chat-action-background-hover);
  }

  .chat-pane-stream {
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    padding: 0 12px 10px;
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .chat-thread-scroll {
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    padding: 0 8px 2px;
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .chat-state-row {
    padding: 18px 0;
    color: var(--chat-meta-text);
    text-align: center;
  }

  .chat-root-message,
  .chat-message {
    display: grid;
    gap: 6px;
    width: 100%;
    max-width: 100%;
    min-width: 0;
    padding: 10px 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
  }

  .chat-root-message {
    margin-bottom: 8px;
    padding: 0 0 14px;
    border-bottom: 1px solid var(--chat-root-message-border);
  }

  .chat-message {
    align-self: flex-start;
    position: relative;
    padding: 10px 12px;
  }

  .chat-message::before {
    content: '';
    position: absolute;
    inset: 1px 0;
    border-radius: 14px;
    background: transparent;
    box-shadow: inset 0 0 0 1px transparent;
    transition:
      background-color 140ms ease,
      box-shadow 140ms ease;
    pointer-events: none;
    z-index: 0;
  }

  .chat-message > * {
    position: relative;
    z-index: 1;
  }

  .chat-message.is-own {
    align-self: stretch;
  }

  .chat-message.is-thread-active {
    padding: 12px;
    border-radius: 14px;
    background: var(--chat-message-active-background);
    box-shadow: var(--chat-message-active-shadow);
  }

  .chat-message:hover::before,
  .chat-message:focus-within::before {
    background: var(--chat-message-hover-background);
    box-shadow: var(--chat-message-hover-shadow);
  }

  .chat-message.is-thread-active::before {
    background: var(--chat-message-active-background);
    box-shadow: var(--chat-message-active-shadow);
  }

  .chat-message.is-continuation {
    gap: 0;
    padding-top: 2px;
  }

  .chat-message-header {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: 12px;
    min-width: 0;
  }

  .chat-message-author {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    min-width: 0;
  }

  .chat-message-author-copy {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
  }

  .chat-message-author-copy span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--chat-message-author-color, var(--chat-heading-text));
    font-size: 14px;
    font-weight: 560;
    line-height: 1.1;
  }

  .chat-message-author-thread-count {
    display: none;
  }

  .chat-message-author-copy p {
    margin: 0;
    color: var(--chat-message-meta-text);
    font-size: 11px;
    line-height: 1.2;
  }

  .chat-message-body {
    margin: 0;
    color: var(--chat-message-body-text);
    font-size: 14px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .chat-mention {
    display: inline;
    padding: 0 0.24em;
    border-radius: 5px;
    background: var(--chat-mention-background);
    color: var(--chat-mention-text);
    font-weight: 680;
    -webkit-box-decoration-break: clone;
    box-decoration-break: clone;
  }

  .chat-attachments,
  .chat-composer-attachments {
    display: grid;
    gap: 10px;
  }

  .chat-attachment {
    display: grid;
    gap: 10px;
    grid-template-columns: minmax(0, 1fr);
    width: 100%;
    padding: 12px;
    border-radius: var(--radius-xl, 16px);
    border: 1px solid var(--chat-attachment-border);
    background: var(--chat-attachment-background);
    color: inherit;
    font: inherit;
    text-decoration: none;
    text-align: left;
  }

  .chat-attachment.is-image,
  .chat-attachment.is-video {
    max-width: min(760px, 100%);
  }

  button.chat-attachment {
    appearance: none;
    cursor: pointer;
  }

  .chat-attachment.is-previewable {
    cursor: zoom-in;
    transition:
      border-color 180ms ease,
      background-color 180ms ease,
      transform 180ms ease;
  }

  .chat-attachment.is-previewable:hover,
  .chat-attachment.is-previewable:focus-visible {
    border-color: var(--chat-attachment-hover-border);
    background: var(--chat-attachment-hover-background);
  }

  .chat-attachment:not(.is-image):not(.is-video) {
    grid-template-columns: 56px minmax(0, 1fr);
    align-items: center;
    padding: 10px;
  }

  .chat-attachment:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .chat-attachment-preview {
    display: grid;
    position: relative;
    place-items: center;
    width: 100%;
    min-height: 0;
    overflow: hidden;
    border-radius: var(--radius-lg, 12px);
    background: var(--chat-attachment-preview-background);
    color: var(--chat-attachment-preview-text);
  }

  .chat-attachment:not(.is-image):not(.is-video) .chat-attachment-preview {
    width: 56px;
    height: 56px;
    border-radius: var(--radius-md, 10px);
  }

  .chat-attachment.is-image img,
  .chat-attachment.is-video video {
    display: block;
    width: 100%;
    height: auto;
    max-height: var(--chat-attachment-image-max-height);
    object-fit: contain;
    object-position: center;
    transition: transform 220ms ease;
  }

  .chat-attachment.is-previewable:hover .chat-attachment-preview > img,
  .chat-attachment.is-previewable:focus-visible .chat-attachment-preview > img,
  .chat-attachment.is-previewable:hover .chat-attachment-preview > video,
  .chat-attachment.is-previewable:focus-visible .chat-attachment-preview > video {
    transform: scale(1.01);
  }

  .chat-attachment-preview-icon {
    display: inline-flex;
    width: 48px;
    height: 48px;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-lg, 12px);
    border: 1px solid var(--chat-attachment-icon-border);
    background: var(--chat-attachment-icon-background);
    color: var(--chat-attachment-icon-text);
  }

  .chat-attachment:not(.is-image):not(.is-video) .chat-attachment-preview-icon {
    width: 34px;
    height: 34px;
    border-radius: var(--radius-md, 10px);
  }

  .chat-attachment-preview-label {
    position: absolute;
    right: 10px;
    bottom: 9px;
    max-width: calc(100% - 20px);
    overflow: hidden;
    padding: 4px 8px;
    border-radius: var(--radius-sm, 6px);
    background: var(--chat-attachment-label-background);
    color: var(--chat-attachment-label-text);
    font-size: 10px;
    font-weight: 700;
    line-height: 1;
    text-overflow: ellipsis;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .chat-attachment.is-link .chat-attachment-preview-label {
    text-transform: none;
  }

  .chat-attachment:not(.is-image):not(.is-video) .chat-attachment-preview-label {
    display: none;
  }

  .chat-attachment-copy {
    display: grid;
    gap: 3px;
  }

  .chat-attachment-copy strong {
    color: var(--constellation-chat-attachment-label);
    font-size: 13px;
    font-weight: 560;
  }

  .chat-attachment-copy span {
    color: var(--constellation-chat-attachment-detail);
    font-size: 11px;
  }

  .chat-thread-mini-summary {
    appearance: none;
    display: none;
    align-items: center;
    gap: 5px;
    min-width: 0;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--chat-thread-summary-accent);
    font: inherit;
    cursor: pointer;
  }

  .chat-thread-mini-count {
    min-width: 0;
    overflow: hidden;
    font-size: 11px;
    font-weight: 620;
    line-height: 1;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-thread-mini-summary:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .chat-thread-summary {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    flex-wrap: wrap;
    gap: 10px;
    padding-top: 2px;
    margin-left: 10px;
    width: calc(100% - 10px);
    color: var(--chat-thread-summary-text);
    font-size: 11px;
  }

  .chat-thread-summary-link {
    display: inline-flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    min-height: 30px;
    width: 100%;
    min-width: 0;
    padding: 5px 8px 5px 6px;
    border: 0;
    border-radius: 12px;
    background: transparent;
    color: var(--chat-thread-summary-link-text);
    font-size: 11px;
    font-weight: 560;
    line-height: 1.2;
    cursor: pointer;
    transition:
      background-color 160ms ease,
      color 160ms ease,
      transform 160ms ease;
  }

  .chat-thread-summary-link:hover,
  .chat-thread-summary-link:focus-visible,
  .chat-thread-summary-link.is-active-thread {
    background: var(--chat-thread-summary-link-background-hover);
    color: var(--chat-thread-summary-link-text-hover);
  }

  .chat-thread-summary-link:hover {
    transform: translateX(1px);
  }

  .chat-thread-summary-link:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .chat-thread-summary-leading {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .chat-thread-summary-count {
    color: var(--chat-thread-summary-accent);
    font-weight: 600;
    white-space: nowrap;
  }

  .chat-thread-preview-stack {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    padding-right: 0;
  }

  .chat-thread-preview-avatar {
    display: inline-flex;
    margin-left: 0;
    border-radius: 999px;
    box-shadow: none;
  }

  .chat-thread-preview-avatar:first-child {
    margin-left: 0;
  }

  .chat-thread-summary-meta {
    display: grid;
    align-items: center;
    min-width: 0;
  }

  .chat-thread-summary-default,
  .chat-thread-summary-hover {
    grid-area: 1 / 1;
    transition:
      opacity 150ms ease,
      transform 150ms ease,
      color 150ms ease;
    white-space: nowrap;
  }

  .chat-thread-summary-default {
    color: var(--chat-thread-summary-text);
  }

  .chat-thread-summary-hover {
    opacity: 0;
    transform: translateY(2px);
    color: var(--chat-thread-summary-accent);
  }

  .chat-thread-summary-link:hover .chat-thread-summary-default,
  .chat-thread-summary-link:focus-visible .chat-thread-summary-default,
  .chat-thread-summary-link.is-active-thread .chat-thread-summary-default {
    opacity: 0;
    transform: translateY(-2px);
  }

  .chat-thread-summary-link:hover .chat-thread-summary-hover,
  .chat-thread-summary-link:focus-visible .chat-thread-summary-hover,
  .chat-thread-summary-link.is-active-thread .chat-thread-summary-hover {
    opacity: 1;
    transform: translateY(0);
  }

  .chat-thread-summary-arrow {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    opacity: 0;
    transform: translateX(-4px);
    transition:
      opacity 150ms ease,
      transform 150ms ease;
  }

  .chat-thread-summary-arrow :global(svg) {
    width: 16px;
    height: 16px;
  }

  .chat-thread-summary-link:hover .chat-thread-summary-arrow,
  .chat-thread-summary-link:focus-visible .chat-thread-summary-arrow,
  .chat-thread-summary-link.is-active-thread .chat-thread-summary-arrow {
    opacity: 0.92;
    transform: translateX(0);
  }

  .chat-message-actions {
    position: absolute;
    top: -10px;
    right: clamp(12px, 10%, 40px);
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 4px;
    border-radius: 12px;
    border: 1px solid var(--chat-message-actions-border);
    background: var(--chat-message-actions-background);
    box-shadow: var(--chat-message-actions-shadow);
    opacity: 0;
    pointer-events: none;
    transform: translateY(4px);
    transition:
      opacity 140ms ease,
      transform 140ms ease;
    z-index: 2;
  }

  .chat-message:hover .chat-message-actions,
  .chat-message:focus-within .chat-message-actions {
    opacity: 1;
    pointer-events: auto;
    transform: translateY(0);
  }

  .chat-message-action {
    color: var(--chat-message-actions-text);
  }

  .chat-message-action :global(.constellation-icon-button-icon) {
    width: 16px;
    height: 16px;
  }

  .chat-message-action :global(svg) {
    width: 100%;
    height: 100%;
  }

  .chat-room-composer {
    padding: 12px 0 0;
  }

  .chat-thread-composer {
    padding: 10px 0 2px;
  }

  .chat-close-thread:disabled,
  .chat-load-older:disabled,
  .chat-unread-refresh:disabled,
  .chat-thread-summary-link:disabled,
  .chat-message-action:disabled {
    opacity: 0.48;
    cursor: not-allowed;
  }

  @media (hover: none) {
    .chat-message-actions {
      opacity: 1;
      pointer-events: auto;
      transform: translateY(0);
    }
  }

  @container chat-dock (max-width: 760px) {
    .chat-workspace-layout {
      grid-template-columns: 54px minmax(0, 1fr);
      gap: 10px;
    }

    .chat-channel-rail {
      align-items: center;
      padding-right: 8px;
    }

    .chat-channel-list {
      align-items: center;
      max-height: none;
    }

    .chat-channel-row {
      justify-content: center;
      width: 44px;
      height: 42px;
      min-height: 42px;
      padding: 0;
      border-radius: 999px;
    }

    .chat-channel-row-team {
      height: 48px;
      min-height: 48px;
    }

    .chat-channel-copy {
      display: none;
    }

    .chat-main-layout.has-thread {
      grid-template-columns: minmax(0, 1fr);
    }

    .chat-thread-splitter {
      display: none;
    }

    .chat-thread-column {
      min-height: 280px;
      padding-left: 0;
      padding-top: 16px;
      border-left: 0;
      border-top: 1px solid var(--chat-thread-border);
    }

    .chat-attachment-preview {
      height: 180px;
    }

  }

  @container chat-dock (max-width: 520px) {
    .chat-pane-stream {
      padding-inline: 6px;
    }

    .chat-message {
      padding-inline: 8px;
    }

    .chat-message-body {
      font-size: 13px;
      line-height: 1.5;
    }

    .chat-conversation-header {
      padding-inline: 4px;
    }

    .chat-attachment-preview {
      height: 150px;
    }
  }

  @media (max-width: 1080px) {
    .chat-workspace-layout {
      grid-template-columns: clamp(156px, 24vw, 200px) minmax(0, 1fr);
      gap: 12px;
    }

    .chat-main-layout.has-thread {
      grid-template-columns: minmax(0, 1fr);
    }

    .chat-thread-splitter {
      display: none;
    }

    .chat-thread-column {
      min-height: 380px;
      padding-left: 0;
      padding-top: 16px;
      border-left: 0;
      border-top: 1px solid var(--chat-thread-border);
    }
  }

  @media (max-width: 720px) {
    .chat-dock-shell {
      padding: 12px;
      border-radius: 18px;
    }

    .chat-workspace-layout {
      grid-template-columns: 52px minmax(0, 1fr);
      gap: 10px;
    }

    .chat-channel-rail {
      padding-right: 8px;
      align-items: center;
    }

    .chat-channel-list {
      align-items: center;
      max-height: none;
    }

    .chat-channel-row {
      justify-content: center;
      width: 44px;
      height: 42px;
      min-height: 42px;
      padding: 0;
      border-radius: 999px;
    }

    .chat-channel-row-team {
      height: 48px;
      min-height: 48px;
    }

    .chat-channel-copy {
      display: none;
    }

    .chat-conversation-header {
      padding-inline: 4px;
    }

    .chat-attachment-preview {
      height: 150px;
    }
  }

</style>
