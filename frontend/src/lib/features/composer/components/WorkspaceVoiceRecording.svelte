<script lang="ts">
  import { voiceLevelToBarHeight } from '$lib/features/composer/domain/voiceLevels';

  let { elapsedMs, levels = [] }: { elapsedMs: number; levels?: number[] } = $props();

  const voiceWaveBars = $derived.by(() =>
    levels.map((level) => ({
      height: voiceLevelToBarHeight(level),
      opacity: Math.max(0.42, 0.58 + level * 0.38),
    })),
  );

  function formatVoiceDuration(ms: number) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${String(seconds).padStart(2, '0')}`;
  }
</script>

<div class="workspace-voice-recording" role="status" aria-live="polite">
  <span class="sr-only">Dictation recording</span>
  <span class="workspace-voice-wave" aria-hidden="true">
    {#each voiceWaveBars as bar}
      <span style={`height:${bar.height}px;opacity:${bar.opacity}`}></span>
    {/each}
  </span>
  <span class="workspace-voice-duration">{formatVoiceDuration(elapsedMs)}</span>
</div>

<style>
  .workspace-voice-recording {
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
    box-sizing: border-box;
    min-height: 40px;
    padding: 2px 3px 0;
    color: var(--constellation-composer-textarea);
  }

  .workspace-voice-wave {
    display: flex;
    align-items: center;
    gap: 2px;
    flex: 1 1 auto;
    min-width: 0;
    height: 24px;
  }

  .workspace-voice-wave span {
    flex: 0 0 2px;
    border-radius: 999px;
    background: currentColor;
    min-height: 3px;
    transition:
      height 80ms linear,
      opacity 80ms linear;
  }

  .workspace-voice-wave::after {
    content: '';
    display: block;
    flex: 1 1 auto;
    height: 1px;
    margin-left: 4px;
    background: currentColor;
    opacity: 0.28;
  }

  .workspace-voice-duration {
    flex: 0 0 auto;
    min-width: 34px;
    color: var(--constellation-composer-placeholder);
    font-size: 13px;
    line-height: 1;
    text-align: right;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
