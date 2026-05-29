<script lang="ts">
  import { ConstellationPresenceSeed } from '$lib/components/constellation';
  import type {
    CortexThreadStageMessageRole,
    CortexThreadStageTone,
  } from '$lib/features/threads/domain/threadTranscriptAdapter';

  let {
    author,
    role = 'user',
    tone,
    presenceStyle,
  }: {
    author: string;
    role?: CortexThreadStageMessageRole | null;
    tone: CortexThreadStageTone;
    presenceStyle?: string;
  } = $props();
</script>

<ConstellationPresenceSeed
  label={author}
  role={role ?? 'user'}
  {tone}
  size="xs"
  treatment="plain"
  className="thread-presence-seed"
  style={presenceStyle}
/>

<style>
  :global(.thread-presence-seed) {
    flex-shrink: 0;
  }
</style>
