<script lang="ts">
  import {
    listThreadDiscussion,
    postThreadDiscussionComment,
    type ThreadDiscussionComment,
  } from '$lib/features/threads/api/threadApi';

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

  $effect(() => {
    if (!ideaId) {
      comments = [];
      loadedForIdeaId = null;
      return;
    }
    if (loadedForIdeaId === ideaId) return;
    void loadComments(ideaId);
  });

  async function loadComments(targetIdeaId = ideaId) {
    if (!targetIdeaId) return;
    loading = true;
    error = '';
    try {
      comments = await listThreadDiscussion(targetIdeaId);
      loadedForIdeaId = targetIdeaId;
    } catch (err: any) {
      error = err?.detail || 'Failed to load Discussion.';
    } finally {
      loading = false;
    }
  }

  async function submitComment(event: SubmitEvent) {
    event.preventDefault();
    const text = body.trim();
    if (!ideaId || !text || posting) return;
    posting = true;
    error = '';
    try {
      const result = await postThreadDiscussionComment(ideaId, {
        body: text,
        metadata: { source: 'thread_discussion_panel' },
      });
      comments = [...comments, result.comment];
      body = '';
    } catch (err: any) {
      error = err?.detail || 'Failed to post comment.';
    } finally {
      posting = false;
    }
  }

  function authorLabel(comment: ThreadDiscussionComment) {
    if (comment.author_name) return comment.author_name;
    if (comment.author_kind === 'illo') return 'Illo';
    return 'Teammate';
  }

  function timeLabel(value: string | null) {
    if (!value) return '';
    try {
      return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    } catch {
      return '';
    }
  }
</script>

<div class="thread-discussion-pane">
  <div class="discussion-list" aria-label="Thread Discussion">
    {#if loading && comments.length === 0}
      <div class="discussion-empty">Loading Discussion...</div>
    {:else if error && comments.length === 0}
      <div class="discussion-empty discussion-error">{error}</div>
    {:else if comments.length === 0}
      <div class="discussion-empty">No Discussion yet.</div>
    {:else}
      {#each comments as comment (comment.id)}
        <article class="discussion-comment">
          <div class="discussion-comment-meta">
            <span class="discussion-author">{authorLabel(comment)}</span>
            {#if timeLabel(comment.created_at)}
              <time datetime={comment.created_at || undefined}>{timeLabel(comment.created_at)}</time>
            {/if}
          </div>
          <div class="discussion-body">{comment.body}</div>
        </article>
      {/each}
    {/if}
  </div>

  <form class="discussion-composer" onsubmit={submitComment}>
    {#if error && comments.length > 0}
      <div class="discussion-inline-error">{error}</div>
    {/if}
    <textarea
      bind:value={body}
      placeholder="Comment on this Thread..."
      rows="3"
      disabled={posting || !ideaId}
    ></textarea>
    <button type="submit" disabled={posting || !body.trim() || !ideaId}>
      {posting ? 'Posting...' : 'Post'}
    </button>
  </form>
</div>

<style>
  .thread-discussion-pane {
    min-height: 100%;
    display: grid;
    grid-template-rows: minmax(0, 1fr) auto;
    color: var(--constellation-utility-panel-tab-active-text);
  }

  .discussion-list {
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 12px;
    overflow-y: auto;
    scrollbar-color: var(--constellation-utility-panel-scrollbar) transparent;
  }

  .discussion-list::-webkit-scrollbar {
    width: 4px;
  }

  .discussion-list::-webkit-scrollbar-thumb {
    border-radius: 999px;
    background: var(--constellation-utility-panel-scrollbar);
  }

  .discussion-empty {
    padding: 22px 12px;
    color: var(--constellation-utility-panel-tab-text);
    font-size: 13px;
    text-align: center;
  }

  .discussion-error,
  .discussion-inline-error {
    color: #ff9a9a;
  }

  .discussion-comment {
    display: grid;
    gap: 5px;
    padding: 10px 11px;
    border: 1px solid var(--constellation-utility-panel-header-border);
    border-radius: 8px;
    background: var(--constellation-control-field-background);
  }

  .discussion-comment-meta {
    display: flex;
    min-width: 0;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    color: var(--constellation-utility-panel-tab-text);
    font-size: 11px;
  }

  .discussion-author {
    min-width: 0;
    overflow: hidden;
    color: var(--constellation-utility-panel-tab-active-text);
    font-weight: 690;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .discussion-body {
    color: var(--constellation-utility-panel-tab-active-text);
    font-size: 13px;
    line-height: 1.45;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }

  .discussion-composer {
    display: grid;
    gap: 8px;
    padding: 10px;
    border-top: 1px solid var(--constellation-utility-panel-header-border);
  }

  .discussion-composer textarea {
    width: 100%;
    min-width: 0;
    resize: vertical;
    max-height: 160px;
    padding: 9px 10px;
    border: 1px solid var(--constellation-control-field-border);
    border-radius: 8px;
    background: var(--constellation-control-field-background);
    color: var(--constellation-utility-panel-tab-active-text);
    font: inherit;
    font-size: 13px;
    line-height: 1.4;
  }

  .discussion-composer textarea:focus {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .discussion-composer button {
    justify-self: end;
    min-height: 32px;
    padding: 0 12px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    border-radius: 8px;
    background: var(--constellation-control-button-secondary-background);
    color: var(--constellation-control-button-secondary-text);
    font: inherit;
    font-size: 12px;
    font-weight: 720;
    cursor: pointer;
  }

  .discussion-composer button:hover:not(:disabled),
  .discussion-composer button:focus-visible {
    border-color: var(--constellation-control-focus-ring);
  }

  .discussion-composer button:disabled,
  .discussion-composer textarea:disabled {
    cursor: default;
    opacity: 0.55;
  }

  .discussion-inline-error {
    font-size: 12px;
  }
</style>
