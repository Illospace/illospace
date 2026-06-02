<script lang="ts">
  import type { ObjectReferencePayload } from '$lib/api/client';
  import ThreadLinkPreviewCard from '$lib/features/threads/components/ThreadLinkPreviewCard.svelte';

  let {
    objectReferences = [],
    threadReferences = [],
    compact = false,
    containerClass = 'object-reference-preview-list',
    keyPrefix = '',
  }: {
    objectReferences?: readonly ObjectReferencePayload[] | null;
    threadReferences?: readonly ObjectReferencePayload[] | null;
    compact?: boolean;
    containerClass?: string;
    keyPrefix?: string;
  } = $props();

  const references = $derived(
    objectReferences?.length ? objectReferences : (threadReferences ?? []),
  );

  const referenceKey = (reference: ObjectReferencePayload, index: number) => [
    keyPrefix,
    reference.thread_id,
    reference.launch_handoff_id,
    reference.object_id,
    reference.original_ref,
    reference.url,
    index,
  ].filter((value) => value !== undefined && value !== null && value !== '').join('-');
</script>

{#if references.length}
  <div class={containerClass}>
    {#each references as reference, index (referenceKey(reference, index))}
      <ThreadLinkPreviewCard {reference} {compact} />
    {/each}
  </div>
{/if}

<style>
  .object-reference-preview-list,
  .chat-thread-link-previews,
  .discussion-thread-link-previews,
  .thread-message-thread-previews {
    display: grid;
    gap: 8px;
  }

  .chat-thread-link-previews {
    margin-top: 8px;
  }
</style>
