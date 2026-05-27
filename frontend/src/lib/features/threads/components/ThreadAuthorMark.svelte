<script lang="ts">
  import { Astre, ConstellationPresenceSeed } from '$lib/components/constellation';
  import type {
    CortexThreadStageMessageRole,
    CortexThreadStageTone,
  } from '$lib/features/threads/domain/threadTranscriptAdapter';

  let {
    author,
    role = 'illo',
    tone,
    isIllo,
    presenceStyle,
  }: {
    author: string;
    role?: CortexThreadStageMessageRole | null;
    tone: CortexThreadStageTone;
    isIllo: boolean;
    presenceStyle?: string;
  } = $props();
</script>

{#if isIllo}
  <span class="thread-illo-mini-astre-shell">
    <Astre
      letter="I"
      owner={author}
      {tone}
      scale="compact"
      semanticLevel="symbol"
      activity="idle"
      presence="online"
      archivedCount={0}
      animated={false}
      className="thread-illo-mini-astre"
      style="left: 50%; top: 50%; width: 18px; height: 18px;"
    />
  </span>
{:else}
  <ConstellationPresenceSeed
    label={author}
    role={role ?? 'illo'}
    {tone}
    size="xs"
    treatment="plain"
    className="thread-presence-seed"
    style={presenceStyle}
  />
{/if}

<style>
  :global(.thread-presence-seed) {
    flex-shrink: 0;
  }

  .thread-illo-mini-astre-shell {
    position: relative;
    display: inline-block;
    width: 18px;
    height: 18px;
    flex-shrink: 0;
    overflow: visible;
  }

  :global(.thread-illo-mini-astre.constellation-astre) {
    --astre-outer-ring-opacity: 0.22;
    --astre-halo-rest-opacity: 0.18;
    --astre-ring-opacity: 0.62;
    --astre-core-opacity: 0.94;
    --astre-core-glow-strong: color-mix(in srgb, var(--thread-message-owner) 28%, transparent);
    --astre-core-glow-soft: color-mix(in srgb, var(--thread-message-owner) 14%, transparent);
    pointer-events: none;
  }

  :global(.thread-illo-mini-astre .constellation-astre-letter) {
    font-size: 8px;
    transform: translate(0, 0);
  }

  :global(.thread-illo-mini-astre .constellation-astre-presence-dot) {
    display: none;
  }
</style>
