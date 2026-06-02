<script lang="ts">
  import { onDestroy, tick } from 'svelte';

  import ChatComposer from '$lib/components/chat/ChatComposer.svelte';
  import ChatStateView from '$lib/components/chat/ChatStateView.svelte';
  import ConversationScrollCue from '$lib/components/chat/ConversationScrollCue.svelte';
  import ThreadLinkPreviewCard from '$lib/features/threads/components/ThreadLinkPreviewCard.svelte';
  import {
    CONVERSATION_SCROLL_BOTTOM_THRESHOLD,
    conversationIsNearBottom,
    scrollConversationToBottom,
    shouldShowConversationScrollCue,
  } from '$lib/components/chat/conversationScroll';
  import { ConstellationNotice, ConstellationPresenceSeed } from '$lib/components/constellation';
  import { defaultIlloMentionOption } from '$lib/features/composer/domain/mentionAutocomplete';
  import {
    listThreadDiscussion,
    postThreadDiscussionComment,
    type ThreadDiscussionComment,
  } from '$lib/features/threads/api/threadApi';
  import { wsClient } from '$lib/stores/ws.svelte';
  import { buildPresenceSeedStyle, normalizeHexColor, presenceToneForColor } from '$lib/utils/constellationPresence';
  import { parseServerDate } from '$lib/utils/datetime';

  let {
    ideaId = null,
  }: {
    ideaId?: string | null;
  } = $props();

  let comments = $state<ThreadDiscussionComment[]>([]);
  let body = $state('');
  let loading = $state(false);
  let posting = $state(false);
  let error = $state('');
  let loadedForIdeaId = $state<string | null>(null);
  let streamEl: HTMLDivElement | undefined = $state();
  let userScrolledUp = $state(false);
  let showScrollCue = $state(false);
  let lastScrollIdeaId = $state<string | null>(null);

  type MessageTextSegment = {
    text: string;
    mention: boolean;
  };

  const MESSAGE_GROUP_WINDOW_MS = 15 * 60 * 1000;
  const MENTION_RENDER_RE = /(^|[^A-Za-z0-9_])@([A-Za-z0-9._-]+)([.,:;!?]?)/g;
  const THREAD_DISCUSSION_RUN_PREFIX = 'thread-discussion:';
  const THREAD_DISCUSSION_REPLY_TOOL = 'post_thread_discussion_reply';
  const DISCUSSION_REPLY_RECONCILE_DELAYS_MS = [1200, 3000, 7000, 15000, 30000, 45000];
  const tailSignature = $derived(`${comments.length}:${comments.at(-1)?.id ?? 'empty'}`);
  const composerPlaceholder = $derived(
    ideaId ? 'Comment on this Thread...' : 'Open a Thread to comment...',
  );
  const mentionOptions = $derived([defaultIlloMentionOption()]);
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;
  let replyReconcileTimers: ReturnType<typeof setTimeout>[] = [];

  $effect(() => {
    if (!ideaId) {
      comments = [];
      loadedForIdeaId = null;
      clearReplyReconcileTimers();
      clearScheduledRefresh();
      return;
    }
    if (loadedForIdeaId === ideaId) return;
    void loadComments(ideaId);
  });

  $effect(() => {
    if (ideaId === lastScrollIdeaId) return;
    lastScrollIdeaId = ideaId;
    userScrolledUp = false;
    showScrollCue = false;
  });

  $effect(() => {
    tailSignature;
    if (!streamEl || loading) return;
    tick().then(() => scrollDiscussionToBottom());
  });

  $effect(() => {
    const activeIdeaId = ideaId;
    if (!activeIdeaId) return;

    const unsubs = [
      wsClient.on('thread_discussion_comment', (msg) => {
        handleDiscussionCommentEvent(activeIdeaId, msg);
      }),
      wsClient.on('tool_finished', (msg) => {
        handleDiscussionRunEvent(activeIdeaId, msg, { requireReplyTool: true, delayMs: 180 });
      }),
      wsClient.on('run_completed', (msg) => {
        handleDiscussionRunEvent(activeIdeaId, msg, { delayMs: 450 });
      }),
      wsClient.onReconnect(() => {
        scheduleCommentRefresh(activeIdeaId, 150);
      }),
    ];

    return () => {
      unsubs.forEach((unsub) => unsub());
      clearReplyReconcileTimers();
      clearScheduledRefresh();
    };
  });

  onDestroy(() => {
    clearReplyReconcileTimers();
    clearScheduledRefresh();
  });

  async function loadComments(targetIdeaId = ideaId, options: { silent?: boolean } = {}) {
    if (!targetIdeaId) return;
    if (!options.silent) {
      loading = true;
      error = '';
    }
    try {
      const nextComments = await listThreadDiscussion(targetIdeaId);
      if (targetIdeaId !== ideaId) return;
      comments = sortDiscussionComments(nextComments);
      loadedForIdeaId = targetIdeaId;
    } catch (err: any) {
      if (options.silent) {
        console.warn('[thread-discussion] failed to refresh comments', err);
      } else {
        error = err?.detail || 'Failed to load Discussion.';
      }
    } finally {
      if (!options.silent) loading = false;
    }
  }

  async function submitComment(value = body) {
    const text = value.trim();
    if (!ideaId || !text || posting) return;
    posting = true;
    error = '';
    try {
      const result = await postThreadDiscussionComment(ideaId, {
        body: text,
        metadata: { source: 'thread_discussion_panel' },
      });
      upsertDiscussionComment(result.comment);
      body = '';
      userScrolledUp = false;
      if (result.trigger) scheduleReplyReconcile(ideaId);
    } catch (err: any) {
      error = err?.detail || 'Failed to post comment.';
    } finally {
      posting = false;
    }
  }

  function handleComposerValueChange(value: string) {
    body = value;
  }

  function authorLabel(comment: ThreadDiscussionComment) {
    if (comment.author_name) return comment.author_name;
    if (comment.author_kind === 'illo') return 'Illo';
    return 'Teammate';
  }

  function timeLabel(value: string | null) {
    if (!value) return '';
    try {
      const createdAt = parseServerDate(value);
      if (!createdAt) return '';
      const now = new Date();
      const diffMs = Math.max(0, now.getTime() - createdAt.getTime());
      const diffMinutes = Math.floor(diffMs / 60000);
      const diffHours = Math.floor(diffMs / 3600000);

      if (diffMs < 60000) return 'just now';
      if (diffMinutes < 60) return `${diffMinutes} min ago`;
      if (diffHours < 24) return `${diffHours} hr ago`;

      return createdAt.toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
    } catch {
      return '';
    }
  }

  function isIlloComment(comment: ThreadDiscussionComment) {
    return comment.author_kind === 'illo' || comment.author_kind === 'agent';
  }

  function handleDiscussionCommentEvent(targetIdeaId: string, msg: any) {
    if (!eventIdeaMatches(targetIdeaId, msg)) return;
    const comment = msg?.comment;
    if (comment && typeof comment === 'object') {
      upsertDiscussionComment(comment as ThreadDiscussionComment);
      if (isIlloComment(comment as ThreadDiscussionComment)) clearReplyReconcileTimers();
      return;
    }
    scheduleCommentRefresh(targetIdeaId, 150);
  }

  function handleDiscussionRunEvent(
    targetIdeaId: string,
    msg: any,
    options: { requireReplyTool?: boolean; delayMs?: number } = {},
  ) {
    if (!eventMatchesDiscussionRun(targetIdeaId, msg)) return;
    if (options.requireReplyTool && !eventUsesDiscussionReplyTool(msg)) return;
    scheduleCommentRefresh(targetIdeaId, options.delayMs ?? 350);
  }

  function eventIdeaMatches(targetIdeaId: string, msg: any) {
    const eventIdeaId = String(msg?.idea_id ?? msg?.thread_id ?? msg?.comment?.thread_id ?? '');
    return eventIdeaId === targetIdeaId;
  }

  function eventMatchesDiscussionRun(targetIdeaId: string, msg: any) {
    const discussionRunThreadId = `${THREAD_DISCUSSION_RUN_PREFIX}${targetIdeaId}`;
    return String(msg?.thread_id ?? '') === discussionRunThreadId
      || String(msg?.idea_id ?? '') === discussionRunThreadId;
  }

  function eventUsesDiscussionReplyTool(msg: any) {
    return String(msg?.tool_name ?? msg?.tool ?? '') === THREAD_DISCUSSION_REPLY_TOOL;
  }

  function upsertDiscussionComment(comment: ThreadDiscussionComment) {
    if (comment.thread_id && ideaId && String(comment.thread_id) !== String(ideaId)) return;
    const commentId = String(comment.id);
    const existingIndex = comments.findIndex((row) => String(row.id) === commentId);
    const nextComments = existingIndex >= 0
      ? comments.map((row, index) => (index === existingIndex ? { ...row, ...comment } : row))
      : [...comments, comment];
    comments = sortDiscussionComments(nextComments);
  }

  function sortDiscussionComments(rows: ThreadDiscussionComment[]) {
    return [...rows].sort((a, b) => {
      const byTime = discussionCommentTime(a) - discussionCommentTime(b);
      if (byTime !== 0) return byTime;
      return Number(a.id) - Number(b.id);
    });
  }

  function discussionCommentTime(comment: ThreadDiscussionComment) {
    const time = Date.parse(comment.created_at ?? '');
    return Number.isFinite(time) ? time : 0;
  }

  function scheduleCommentRefresh(targetIdeaId = ideaId, delayMs = 350) {
    if (!targetIdeaId) return;
    clearScheduledRefresh();
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      void loadComments(targetIdeaId, { silent: true });
    }, delayMs);
  }

  function scheduleReplyReconcile(targetIdeaId: string | null) {
    if (!targetIdeaId) return;
    clearReplyReconcileTimers();
    replyReconcileTimers = DISCUSSION_REPLY_RECONCILE_DELAYS_MS.map((delayMs) =>
      setTimeout(() => {
        void loadComments(targetIdeaId, { silent: true });
      }, delayMs),
    );
  }

  function clearScheduledRefresh() {
    if (refreshTimer === null) return;
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }

  function clearReplyReconcileTimers() {
    replyReconcileTimers.forEach((timer) => clearTimeout(timer));
    replyReconcileTimers = [];
  }

  function participantTone(comment: ThreadDiscussionComment) {
    if (isIlloComment(comment)) return 'spectral';
    return presenceToneForColor(comment.author_color);
  }

  function participantStyle(comment: ThreadDiscussionComment) {
    if (isIlloComment(comment)) return undefined;
    return buildPresenceSeedStyle(normalizeHexColor(comment.author_color)) || undefined;
  }

  function messageStyle(comment: ThreadDiscussionComment) {
    const accent = normalizeHexColor(comment.author_color);
    if (!accent || isIlloComment(comment)) return undefined;

    return [
      `--chat-message-author-color:color-mix(in srgb, ${accent} 76%, var(--constellation-color-text-primary))`,
      `--seed-accent:${accent}`,
    ].join('; ');
  }

  function shouldShowMessageHeader(rows: ThreadDiscussionComment[], index: number) {
    if (index === 0) return true;

    const current = rows[index];
    const previous = rows[index - 1];
    if (!current || !previous) return true;

    if (!sameDiscussionAuthor(current, previous)) return true;

    if (!current.created_at || !previous.created_at) return false;

    const currentDate = parseServerDate(current.created_at);
    const previousDate = parseServerDate(previous.created_at);
    if (!currentDate || !previousDate) return true;

    if (!sameCalendarDay(currentDate, previousDate)) return true;

    return currentDate.getTime() - previousDate.getTime() > MESSAGE_GROUP_WINDOW_MS;
  }

  function sameDiscussionAuthor(current: ThreadDiscussionComment, previous: ThreadDiscussionComment) {
    return (
      current.author_user_id === previous.author_user_id &&
      current.author_kind === previous.author_kind &&
      current.author_name === previous.author_name
    );
  }

  function sameCalendarDay(current: Date, previous: Date) {
    return (
      current.getFullYear() === previous.getFullYear() &&
      current.getMonth() === previous.getMonth() &&
      current.getDate() === previous.getDate()
    );
  }

  function messageTextSegments(bodyText: string): MessageTextSegment[] {
    const segments: MessageTextSegment[] = [];
    MENTION_RENDER_RE.lastIndex = 0;
    let cursor = 0;
    let match: RegExpExecArray | null;

    while ((match = MENTION_RENDER_RE.exec(bodyText)) !== null) {
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
        segments.push({ text: bodyText.slice(cursor, mentionStart), mention: false });
      }

      const mentionText = `@${token}`;
      segments.push({ text: mentionText, mention: true });
      cursor = mentionStart + mentionText.length;

      if (punctuation) {
        segments.push({ text: punctuation, mention: false });
        cursor += punctuation.length;
      }
    }

    if (cursor < bodyText.length) {
      segments.push({ text: bodyText.slice(cursor), mention: false });
    }

    return segments.length > 0 ? segments : [{ text: bodyText, mention: false }];
  }

  function syncScrollCue() {
    showScrollCue = shouldShowConversationScrollCue(streamEl);
  }

  function handleStreamScroll() {
    userScrolledUp = !conversationIsNearBottom(streamEl, CONVERSATION_SCROLL_BOTTOM_THRESHOLD);
    syncScrollCue();
  }

  function scrollDiscussionToBottom(force = false) {
    if (!streamEl) return;
    if (!force && userScrolledUp) return;
    scrollConversationToBottom(streamEl);
    requestAnimationFrame(() => {
      userScrolledUp = false;
      syncScrollCue();
    });
  }
</script>

<div class="thread-discussion-pane">
  <div
    class="discussion-stream"
    aria-label="Thread Discussion"
    bind:this={streamEl}
    onscroll={handleStreamScroll}
  >
    {#if !ideaId}
      <ChatStateView
        state="empty"
        title="No Thread selected"
        description="Open a Thread to read and write its Discussion."
        eyebrow="Discussion"
        compact
        surface="plain"
        className="discussion-state"
      />
    {:else if loading && comments.length === 0}
      <ChatStateView
        state="loading"
        compact
        surface="plain"
        className="discussion-state"
      />
    {:else if error && comments.length === 0}
      <ChatStateView
        state="error"
        title="Discussion could not load"
        description={error}
        tone="warning"
        actionLabel="Try again"
        onAction={() => void loadComments()}
        compact
        className="discussion-state"
      />
    {:else if comments.length === 0}
      <ChatStateView
        state="empty"
        title="No comments yet"
        description="Start a scoped note for teammates on this Thread."
        eyebrow="Discussion"
        compact
        surface="plain"
        className="discussion-state"
      />
    {:else}
      <div class="discussion-message-list">
        {#each comments as comment, index (comment.id)}
          {@const showHeader = shouldShowMessageHeader(comments, index)}
          {@const author = authorLabel(comment)}
          {@const timestamp = timeLabel(comment.created_at)}
          <article
            class="discussion-message"
            class:has-header={showHeader}
            class:is-continuation={!showHeader}
            class:is-illo={isIlloComment(comment)}
            style={messageStyle(comment)}
          >
            {#if showHeader}
              <header class="discussion-message-header">
                <ConstellationPresenceSeed
                  label={author}
                  size="sm"
                  role={isIlloComment(comment) ? 'illo' : 'user'}
                  tone={participantTone(comment)}
                  style={participantStyle(comment)}
                  treatment="plain"
                />

                <div class="discussion-message-author-copy">
                  <span>{author}</span>
                  {#if timestamp}
                    <time datetime={comment.created_at || undefined}>{timestamp}</time>
                  {/if}
                </div>
              </header>
            {/if}

            <p class="discussion-message-body">
              {#each messageTextSegments(comment.body) as segment, segmentIndex (segmentIndex)}
                {#if segment.mention}
                  <span class="chat-mention">{segment.text}</span>
                {:else}
                  {segment.text}
                {/if}
              {/each}
            </p>
            {#if comment.thread_references?.length}
              <div class="discussion-thread-link-previews">
                {#each comment.thread_references as reference (`${comment.id}-${reference.thread_id ?? reference.original_ref ?? reference.url}`)}
                  <ThreadLinkPreviewCard {reference} compact />
                {/each}
              </div>
            {/if}
          </article>
        {/each}
      </div>
    {/if}

    <ConversationScrollCue
      visible={showScrollCue}
      label="Jump to latest Discussion comment"
      onclick={() => scrollDiscussionToBottom(true)}
    />
  </div>

  <div class="discussion-composer">
    {#if error && comments.length > 0}
      <ConstellationNotice
        title="Comment was not posted"
        description={error}
        tone="warning"
        compact
      />
    {/if}

    <ChatComposer
      tone="spectral"
      variant="thread"
      placeholder={composerPlaceholder}
      value={body}
      mentionOptions={mentionOptions}
      disabled={posting || !ideaId}
      loading={posting}
      canSubmit={Boolean(ideaId) && body.trim().length > 0 && !posting}
      primaryActionLabel="Post"
      workingLabel="Posting"
      onValueChange={handleComposerValueChange}
      onSubmit={(value) => void submitComment(value)}
    />
  </div>
</div>

<style>
  .thread-discussion-pane {
    --chat-message-hover-background: rgba(255, 255, 255, 0.025);
    --chat-message-hover-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.03);
    --chat-message-body-text: var(--constellation-thread-message-illo-body);
    --chat-message-meta-text: var(--constellation-thread-message-illo-meta);
    --chat-message-author-color: var(--constellation-thread-message-author);
    --chat-mention-background: rgba(150, 188, 255, 0.16);
    --chat-mention-text: rgba(207, 224, 255, 0.98);
    width: 100%;
    height: 100%;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    color: var(--chat-message-body-text);
    container-type: inline-size;
  }

  :global(:root[data-color-scheme='light']) .thread-discussion-pane {
    --chat-message-hover-background: rgba(255, 255, 255, 0.42);
    --chat-message-hover-shadow: inset 0 0 0 1px rgba(24, 35, 49, 0.04);
    --chat-mention-background: rgba(72, 111, 168, 0.14);
    --chat-mention-text: #315a91;
  }

  .discussion-stream {
    position: relative;
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: auto;
    padding: 14px 14px 8px;
    scrollbar-color: var(--constellation-utility-panel-scrollbar) transparent;
  }

  .discussion-stream::-webkit-scrollbar {
    width: 4px;
  }

  .discussion-stream::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: var(--constellation-utility-panel-scrollbar);
  }

  .thread-discussion-pane :global(.discussion-state) {
    margin: auto 0;
    padding: 8px 2px;
  }

  .discussion-message-list {
    display: flex;
    flex-direction: column;
    gap: 0;
    min-width: 0;
    padding-bottom: 6px;
  }

  .discussion-message {
    position: relative;
    display: grid;
    align-self: stretch;
    gap: 7px;
    min-width: 0;
    padding: 10px 12px;
    border-radius: 14px;
    background: transparent;
    isolation: isolate;
  }

  .discussion-message::before {
    content: '';
    position: absolute;
    inset: 1px 0;
    z-index: -1;
    border-radius: inherit;
    background: transparent;
    box-shadow: none;
    transition:
      background-color 140ms ease,
      box-shadow 140ms ease;
    pointer-events: none;
  }

  .discussion-message:hover::before,
  .discussion-message:focus-within::before {
    background: var(--chat-message-hover-background);
    box-shadow: var(--chat-message-hover-shadow);
  }

  .discussion-message.is-continuation {
    gap: 0;
    padding-top: 2px;
  }

  .discussion-message-header {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    min-width: 0;
  }

  .discussion-message-author-copy {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
  }

  .discussion-message-author-copy span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--chat-message-author-color);
    font-size: 13px;
    font-weight: 600;
    line-height: 1.15;
  }

  .discussion-message-author-copy time {
    color: var(--chat-message-meta-text);
    font-size: 11px;
    line-height: 1.2;
  }

  .discussion-message-body {
    margin: 0;
    color: var(--chat-message-body-text);
    font-size: 14px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .discussion-thread-link-previews {
    display: grid;
    gap: 8px;
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

  .discussion-composer {
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
    min-width: 0;
    padding: 10px 12px 12px;
    border-top: 1px solid var(--constellation-utility-panel-header-border);
  }

  .discussion-composer :global(.chat-composer-shell.is-thread .cortex-workspace-composer) {
    min-height: 94px;
    border-radius: 18px;
  }

  @container (max-width: 360px) {
    .discussion-message {
      padding-inline: 10px;
    }

    .discussion-stream {
      padding-inline: 10px;
    }

    .discussion-composer {
      padding-inline: 10px;
    }
  }
</style>
