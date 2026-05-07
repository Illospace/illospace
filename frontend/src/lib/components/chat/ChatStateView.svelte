<script lang="ts">
  import {
    ConstellationButton,
    ConstellationEmptyState,
    ConstellationNotice,
    ConstellationPanel,
    ConstellationSkeletonBlock,
  } from '$lib/components/constellation';

  import type { ChatNoticeTone } from './chatTypes';

  let {
    state = 'empty',
    title = '',
    description = '',
    eyebrow = '',
    tone = 'neutral',
    actionLabel = '',
    onAction,
    compact = false,
    surface = 'plain',
    className = '',
  }: {
    state?: 'loading' | 'empty' | 'error';
    title?: string;
    description?: string;
    eyebrow?: string;
    tone?: ChatNoticeTone;
    actionLabel?: string;
    onAction?: () => void;
    compact?: boolean;
    surface?: 'plain' | 'framed';
    className?: string;
  } = $props();

  const rootClass = $derived(['chat-state-view', className].filter(Boolean).join(' '));
</script>

{#if state === 'loading'}
  <div class={rootClass} data-chat-state="loading">
    {#if surface === 'framed'}
      <ConstellationPanel padding={compact ? 'sm' : 'md'} className="chat-state-panel">
        <div class="chat-state-loading">
          <ConstellationSkeletonBlock variant="text" lineCount={3} />
          <ConstellationSkeletonBlock variant="text" lineCount={4} />
          <ConstellationSkeletonBlock variant="text" lineCount={2} />
        </div>
      </ConstellationPanel>
    {:else}
      <div class="chat-state-loading">
        <ConstellationSkeletonBlock variant="text" lineCount={3} />
        <ConstellationSkeletonBlock variant="text" lineCount={4} />
        <ConstellationSkeletonBlock variant="text" lineCount={2} />
      </div>
    {/if}
  </div>
{:else if state === 'error'}
  <div class={rootClass} data-chat-state="error">
    <ConstellationNotice title={title || 'Something interrupted chat'} {description} {tone} compact={compact}>
      {#if actionLabel && onAction}
        {#snippet actions()}
          <ConstellationButton variant="secondary" size="sm" onclick={onAction}>
            {actionLabel}
          </ConstellationButton>
        {/snippet}
      {/if}
    </ConstellationNotice>
  </div>
{:else}
  <div class={rootClass} data-chat-state="empty">
    <ConstellationEmptyState
      title={title || 'Nothing here yet'}
      {description}
      {eyebrow}
      size={compact ? 'sm' : 'md'}
      surface={surface}
      align="start"
    >
      {#if actionLabel && onAction}
        {#snippet actions()}
          <ConstellationButton variant="secondary" size="sm" onclick={onAction}>
            {actionLabel}
          </ConstellationButton>
        {/snippet}
      {/if}
    </ConstellationEmptyState>
  </div>
{/if}

<style>
  .chat-state-view {
    width: 100%;
    min-width: 0;
  }

  .chat-state-loading {
    display: grid;
    gap: 12px;
  }
</style>
