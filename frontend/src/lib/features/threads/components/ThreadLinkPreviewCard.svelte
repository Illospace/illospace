<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { ObjectReferencePayload } from '$lib/api/client';
  import { threadRoute } from '$lib/features/threads/domain/threadLinks';

  let {
    reference,
    compact = false,
  }: {
    reference: ObjectReferencePayload;
    compact?: boolean;
  } = $props();

  const available = $derived(reference?.status !== 'unavailable');
  const objectType = $derived(String(reference?.object_type || 'thread'));
  const isLaunchHandoff = $derived(objectType === 'launch_handoff');
  const title = $derived(
    available
      ? String(reference?.title || (isLaunchHandoff ? 'Untitled handoff' : 'Untitled thread'))
      : '',
  );
  const summary = $derived(
    available
      ? String(reference?.preview_summary || '').trim()
      : '',
  );
  const href = $derived(
    available
      ? String(
        reference?.route
        || reference?.launch_url
        || reference?.thread_route
        || reference?.url
        || (reference?.thread_id ? threadRoute(reference.thread_id) : '#'),
      )
      : '#',
  );
  const threadId = $derived(String(reference?.thread_id || '').trim());
  const objectId = $derived(String(reference?.launch_handoff_id || reference?.object_id || threadId || '').trim());
  const targetTool = $derived(String(reference?.target_tool || 'codex').trim());
  const targetLabel = $derived(targetTool ? `${targetTool[0].toUpperCase()}${targetTool.slice(1)}` : 'Codex');
  const kicker = $derived(isLaunchHandoff ? `${targetLabel} handoff` : 'Thread');
  const cardClass = $derived(
    ['thread-link-preview-card', compact ? 'is-compact' : '', available ? 'is-available' : 'is-unavailable']
      .filter(Boolean)
      .join(' '),
  );
</script>

{#if available}
  <a class={cardClass} href={href} data-thread-id={threadId} data-object-type={objectType} data-object-id={objectId}>
    <span class="thread-link-preview-icon" aria-hidden="true">
      <ConstellationIcon name="link" size={14} stroke={1.9} />
    </span>
    <span class="thread-link-preview-copy">
      <span class="thread-link-preview-kicker">{kicker}</span>
      <strong>{title}</strong>
      {#if summary}
        <span>{summary}</span>
      {/if}
    </span>
    <span class="thread-link-preview-action" aria-hidden="true">
      <ConstellationIcon name="chevron-right" size={14} stroke={1.9} />
    </span>
  </a>
{:else}
  <div class={cardClass}>
    <span class="thread-link-preview-icon" aria-hidden="true">
      <ConstellationIcon name="link" size={14} stroke={1.9} />
    </span>
    <span class="thread-link-preview-copy">
      <span class="thread-link-preview-kicker">{kicker}</span>
      <span class="thread-link-preview-unavailable">Unavailable</span>
    </span>
  </div>
{/if}

<style>
  .thread-link-preview-card {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    width: min(100%, 560px);
    margin-top: 10px;
    padding: 10px 12px;
    border: 1px solid var(--thread-link-preview-border, rgba(255, 255, 255, 0.08));
    border-radius: 8px;
    background: var(--thread-link-preview-background, rgba(255, 255, 255, 0.035));
    color: var(--thread-link-preview-text, rgba(240, 240, 250, 0.9));
    text-decoration: none;
    transition:
      border-color 160ms ease,
      background 160ms ease,
      transform 160ms ease;
  }

  .thread-link-preview-card.is-compact {
    padding: 9px 10px;
    gap: 8px;
  }

  .thread-link-preview-card.is-available:hover {
    border-color: var(--thread-link-preview-hover-border, rgba(141, 183, 255, 0.28));
    background: var(--thread-link-preview-hover-background, rgba(141, 183, 255, 0.075));
    transform: translateY(-1px);
  }

  .thread-link-preview-card.is-unavailable {
    opacity: 0.72;
  }

  .thread-link-preview-icon {
    display: inline-grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: var(--thread-link-preview-icon-background, rgba(141, 183, 255, 0.09));
    color: var(--thread-link-preview-icon-text, rgba(171, 203, 255, 0.92));
  }

  .thread-link-preview-copy {
    display: grid;
    min-width: 0;
    gap: 3px;
  }

  .thread-link-preview-kicker {
    color: var(--thread-link-preview-muted, rgba(240, 240, 250, 0.42));
    font-size: 0.66rem;
    font-weight: 700;
    letter-spacing: 0;
    text-transform: uppercase;
  }

  .thread-link-preview-copy strong {
    overflow: hidden;
    color: var(--thread-link-preview-title, rgba(255, 255, 255, 0.92));
    font-size: 0.9rem;
    font-weight: 700;
    line-height: 1.25;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .thread-link-preview-unavailable {
    color: var(--thread-link-preview-summary, rgba(240, 240, 250, 0.58));
    font-size: 0.86rem;
    font-weight: 650;
    line-height: 1.25;
  }

  .thread-link-preview-copy span:last-child:not(.thread-link-preview-kicker):not(.thread-link-preview-unavailable) {
    display: -webkit-box;
    overflow: hidden;
    color: var(--thread-link-preview-summary, rgba(240, 240, 250, 0.58));
    font-size: 0.8rem;
    line-height: 1.35;
    line-clamp: 2;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }

  .thread-link-preview-action {
    color: var(--thread-link-preview-muted, rgba(240, 240, 250, 0.42));
  }

  :global(:root[data-color-scheme='light']) .thread-link-preview-card {
    --thread-link-preview-border: rgba(24, 35, 49, 0.08);
    --thread-link-preview-background: rgba(255, 255, 255, 0.58);
    --thread-link-preview-hover-border: rgba(49, 95, 214, 0.2);
    --thread-link-preview-hover-background: rgba(255, 255, 255, 0.82);
    --thread-link-preview-text: rgba(28, 40, 53, 0.9);
    --thread-link-preview-title: rgba(18, 27, 36, 0.94);
    --thread-link-preview-summary: rgba(82, 98, 111, 0.78);
    --thread-link-preview-muted: rgba(82, 98, 111, 0.56);
    --thread-link-preview-icon-background: rgba(49, 95, 214, 0.08);
    --thread-link-preview-icon-text: rgba(49, 95, 214, 0.88);
  }
</style>
