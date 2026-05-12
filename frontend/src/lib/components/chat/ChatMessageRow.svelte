<script lang="ts">
  import {
    ConstellationButton,
    ConstellationPill,
    ConstellationPresenceSeed,
    ConstellationPresenceStack,
  } from '$lib/components/constellation';

  import ChatUnreadChip from './ChatUnreadChip.svelte';
  import type { ChatAttachmentItem, ChatMessageItem } from './chatTypes';

  let {
    item,
    onOpenThread,
    className = '',
  }: {
    item: ChatMessageItem;
    onOpenThread?: (threadId: string) => void;
    className?: string;
  } = $props();

  function normalizeHexColor(value: string | null | undefined): string | null {
    if (typeof value !== 'string') return null;
    const trimmed = value.trim();
    if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(trimmed)) return null;
    if (trimmed.length === 4) {
      return `#${trimmed[1]}${trimmed[1]}${trimmed[2]}${trimmed[2]}${trimmed[3]}${trimmed[3]}`;
    }
    return trimmed;
  }

  const rootClass = $derived.by(() => {
    const role = item.role ?? 'illo';

    return [
      'chat-message-row',
      `is-${item.kind ?? 'message'}`,
      role === 'user' ? 'is-user' : role === 'system' ? 'is-system' : 'is-illo',
      item.error ? 'is-error' : '',
      item.pending ? 'is-pending' : '',
      item.kind === 'root-summary' ? 'is-root-summary' : '',
      className,
    ]
      .filter(Boolean)
      .join(' ');
  });

  const userStyle = $derived.by(() => {
    if ((item.role ?? 'illo') !== 'user') return '';

    const accent = normalizeHexColor(item.accentColor) ?? undefined;
    const core = normalizeHexColor(item.coreColor) ?? undefined;
    const owner = normalizeHexColor(item.ownerColor) ?? undefined;
    if (!accent || !core || !owner) return '';

    const seedCore = `color-mix(in srgb, ${accent} var(--constellation-presence-seed-user-core-accent-strength, 52%), var(--constellation-presence-seed-user-core-base, #050910))`;
    const seedOwner = `color-mix(in srgb, ${accent} var(--constellation-presence-seed-user-owner-accent-strength, 18%), var(--constellation-presence-seed-user-owner-base, #f0f0fa))`;

    return [
      `--thread-message-accent:${accent}`,
      `--thread-message-core:${core}`,
      `--thread-message-owner:${owner}`,
      `--thread-message-shell:color-mix(in srgb, ${core} var(--constellation-thread-message-user-fill-strength), var(--constellation-thread-message-user-shell-base))`,
      `--thread-message-border:color-mix(in srgb, ${accent} var(--constellation-thread-message-user-border-strength), var(--constellation-thread-message-user-border-base))`,
      `--seed-accent:${accent}`,
      `--seed-core:${seedCore}`,
      `--seed-owner:${seedOwner}`,
    ].join('; ');
  });

  const threadParticipants = $derived(
    (item.thread?.participants ?? []).map((member) => ({
      name: member.label,
      tone: member.tone ?? 'spectral',
      style: member.style,
    })),
  );

  function unreadChipStyle(color: string | null | undefined) {
    const accent = normalizeHexColor(color);
    return accent ? `--chat-unread-chip-accent:${accent};` : undefined;
  }

  function openThread() {
    if (!item.thread) return;
    item.thread.onOpen?.(item.thread.id);
    onOpenThread?.(item.thread.id);
  }

  function attachmentKey(attachment: ChatAttachmentItem, index: number) {
    return attachment.id ?? attachment.url ?? `${attachment.kind ?? 'file'}-${index}`;
  }
</script>

<article class={rootClass} style={userStyle}>
  {#if item.kind !== 'system'}
    <header class="chat-message-header">
      <ConstellationPresenceSeed
        label={item.author}
        role={item.role === 'illo' ? 'illo' : 'user'}
        tone={item.tone ?? 'spectral'}
        size="sm"
        treatment="plain"
        className="chat-message-seed"
        style={userStyle}
      />

      <div class="chat-message-meta">
        <span class="chat-message-author">{item.author}</span>

        {#if item.timestamp || item.tag || item.statusLabel}
          <span class="chat-message-meta-supplemental">
            {#if item.timestamp}
              <span>{item.timestamp}</span>
            {/if}

            {#if item.timestamp && (item.tag || item.statusLabel)}
              <span class="chat-message-meta-divider" aria-hidden="true"></span>
            {/if}

            {#if item.tag}
              <span>{item.tag}</span>
            {/if}

            {#if item.tag && item.statusLabel}
              <span class="chat-message-meta-divider" aria-hidden="true"></span>
            {/if}

            {#if item.statusLabel}
              <span>{item.statusLabel}</span>
            {/if}
          </span>
        {/if}
      </div>
    </header>
  {/if}

  <div class="chat-message-content">
    {#if item.summary}
      <p class="chat-message-summary">{item.summary}</p>
    {/if}

    {#if item.html}
      <div class="chat-message-html">{@html item.html}</div>
    {:else if item.body}
      <p>{item.body}</p>
    {/if}

    {#if item.attachments?.length}
      <div class="chat-message-attachments">
        {#each item.attachments as attachment, index (attachmentKey(attachment, index))}
          <a
            class={`chat-attachment ${attachment.kind === 'image' ? 'is-image' : ''}`}
            href={attachment.url}
            target={attachment.url ? '_blank' : undefined}
            rel={attachment.url ? 'noreferrer' : undefined}
          >
            {#if attachment.previewUrl}
              <img src={attachment.previewUrl} alt={attachment.label} />
            {/if}

            <span class="chat-attachment-copy">
              <span class="chat-attachment-label">{attachment.label}</span>
              {#if attachment.detail}
                <span class="chat-attachment-detail">{attachment.detail}</span>
              {/if}
            </span>
          </a>
        {/each}
      </div>
    {/if}
  </div>

  {#if item.thread}
    <div class="chat-thread-summary">
      <div class="chat-thread-summary-copy">
        <div class="chat-thread-summary-head">
          <ConstellationPill variant="status" leadingDot>
            {item.thread.label ?? 'Focused thread'}
          </ConstellationPill>

          {#if item.thread.unreadCount}
            <ChatUnreadChip
              count={item.thread.unreadCount}
              compact
              style={unreadChipStyle(item.thread.accentColor ?? item.accentColor)}
            />
          {/if}
        </div>

        <div class="chat-thread-summary-meta">
          {#if item.thread.replyCount}
            <span>{item.thread.replyCount} replies</span>
          {/if}

          {#if item.thread.lastReplyLabel}
            <span>{item.thread.lastReplyLabel}</span>
          {/if}

          {#if threadParticipants.length > 0}
            <ConstellationPresenceStack members={threadParticipants} size="sm" caption="" />
          {/if}
        </div>
      </div>

      <ConstellationButton variant="secondary" size="sm" onclick={openThread}>
        Open thread
      </ConstellationButton>
    </div>
  {/if}
</article>

<style>
  .chat-message-row {
    --thread-message-accent: var(--constellation-thread-message-illo-accent);
    --thread-message-core: var(--constellation-thread-message-illo-core);
    --thread-message-owner: var(--constellation-thread-message-illo-owner);
    --thread-message-body: var(--constellation-thread-message-illo-body);
    --thread-message-meta: var(--constellation-thread-message-illo-meta);
    display: grid;
    gap: 10px;
    width: min(100%, 760px);
    color: var(--thread-message-body);
  }

  .chat-message-row.is-illo {
    margin-right: auto;
  }

  .chat-message-row.is-user {
    --thread-message-accent: var(--constellation-color-spectral);
    --thread-message-core: var(--constellation-color-spectral-core);
    --thread-message-owner: var(--constellation-color-spectral-owner);
    --thread-message-shell:
      color-mix(
        in srgb,
        var(--thread-message-core) var(--constellation-thread-message-user-fill-strength),
        var(--constellation-thread-message-user-shell-base)
      );
    --thread-message-border:
      color-mix(
        in srgb,
        var(--thread-message-accent) var(--constellation-thread-message-user-border-strength),
        var(--constellation-thread-message-user-border-base)
      );
    width: min(100%, 640px);
    margin-left: auto;
    padding: 14px 16px 16px;
    border-radius: var(--constellation-radius-panel);
    border: 1px solid var(--thread-message-border);
    background: var(--thread-message-shell);
    box-shadow: var(--constellation-thread-message-user-shadow);
    isolation: isolate;
  }

  .chat-message-row.is-root-summary {
    width: min(100%, 100%);
    padding: 16px 18px;
    border-radius: calc(var(--constellation-radius-panel) + 2px);
    border: 1px solid rgba(141, 183, 255, 0.22);
    background:
      radial-gradient(circle at 18% 0%, rgba(141, 183, 255, 0.12), transparent 34%),
      linear-gradient(180deg, rgba(12, 16, 26, 0.92), rgba(9, 12, 19, 0.88));
    box-shadow:
      0 20px 40px rgba(0, 0, 0, 0.22),
      inset 0 1px 0 rgba(255, 255, 255, 0.04);
  }

  .chat-message-row.is-system {
    width: min(100%, 100%);
    padding: 12px 14px;
    border-left: 2px solid var(--constellation-chat-message-system-border);
    color: var(--constellation-chat-message-system-text);
  }

  .chat-message-row.is-error {
    border-color: rgba(219, 110, 130, 0.3);
  }

  .chat-message-row.is-pending {
    opacity: 0.82;
  }

  .chat-message-header {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .chat-message-meta {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px;
    min-width: 0;
  }

  .chat-message-author,
  .chat-message-meta-supplemental {
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .chat-message-author {
    color: var(--constellation-thread-message-author);
  }

  .chat-message-row.is-user .chat-message-author {
    color: var(--thread-message-owner);
  }

  .chat-message-meta-supplemental {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
    color: var(--thread-message-meta);
  }

  .chat-message-meta-divider {
    width: 3px;
    height: 3px;
    border-radius: 50%;
    background: currentColor;
  }

  .chat-message-content {
    display: grid;
    gap: 12px;
    color: var(--thread-message-body);
    font-family: var(--constellation-font-sans);
    font-size: 14px;
    line-height: 1.58;
  }

  .chat-message-content > * {
    margin: 0;
  }

  .chat-message-summary {
    color: var(--constellation-chat-message-summary);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    line-height: 1.5;
    text-transform: uppercase;
  }

  .chat-message-html :global(*) {
    margin: 0;
  }

  .chat-message-html :global(ul),
  .chat-message-html :global(ol) {
    padding-left: 18px;
  }

  .chat-message-html :global(li + li) {
    margin-top: 4px;
  }

  .chat-message-attachments {
    display: grid;
    gap: 10px;
  }

  .chat-attachment {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 12px;
    align-items: center;
    padding: 10px 12px;
    border-radius: 16px;
    border: 1px solid var(--constellation-chat-attachment-border);
    background: var(--constellation-chat-attachment-background);
    color: inherit;
    text-decoration: none;
  }

  .chat-attachment.is-image {
    align-items: start;
  }

  .chat-attachment img {
    width: 56px;
    height: 56px;
    border-radius: 12px;
    object-fit: cover;
    border: 1px solid var(--constellation-chat-attachment-image-border);
  }

  .chat-attachment-copy {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .chat-attachment-label {
    color: var(--constellation-chat-attachment-label);
    font-size: 12px;
    font-weight: 600;
  }

  .chat-attachment-detail {
    color: var(--constellation-chat-attachment-detail);
    font-family: var(--constellation-font-mono);
    font-size: 9px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .chat-thread-summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 14px;
    border-radius: 18px;
    border: 1px solid var(--constellation-chat-thread-summary-border);
    background: var(--constellation-chat-thread-summary-background);
  }

  .chat-thread-summary-copy {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .chat-thread-summary-head,
  .chat-thread-summary-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .chat-thread-summary-meta {
    color: var(--constellation-chat-thread-summary-meta);
    font-family: var(--constellation-font-mono);
    font-size: 9px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
</style>
