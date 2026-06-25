<script lang="ts">
  import { ConstellationNotice } from '$lib/components/constellation';

  import RuntimeSelect from './RuntimeSelect.svelte';
  import type { RuntimeSettings, VoiceDraft } from './types';

  let {
    voice,
    voiceDraft,
    canManageSettings,
    onUpdateVoice,
  }: {
    voice: RuntimeSettings['voice'];
    voiceDraft: VoiceDraft;
    canManageSettings: boolean;
    onUpdateVoice: (key: keyof VoiceDraft, value: string) => void;
  } = $props();

  const voiceNotice = $derived.by(() => {
    if (voice.status === 'ready') return null;
    return {
      title: 'Voice dictation is not ready.',
      detail: voice.detail || 'Select an available provider and API key before using dictation.',
    };
  });
</script>

<section class="runtime-section voice-runtime" aria-labelledby="voice-runtime-heading">
  <header class="runtime-section-heading">
    <div>
      <h2 id="voice-runtime-heading">Voice dictation</h2>
      <p>Configure realtime transcription for composers.</p>
    </div>
  </header>

  <div class="voice-stack">
    {#if voiceNotice}
      <ConstellationNotice title={voiceNotice.title} description={voiceNotice.detail} tone="warning" compact />
    {/if}

    <div class="voice-flow-grid">
      <RuntimeSelect
        id="voice-provider"
        label="Provider"
        value={voiceDraft.provider}
        options={voice.provider_options}
        disabled={!canManageSettings}
        onValueChange={(value) => onUpdateVoice('provider', value)}
      />
      <RuntimeSelect
        id="voice-language"
        label="Preferred language"
        value={voiceDraft.language}
        options={voice.language_options}
        disabled={!canManageSettings}
        onValueChange={(value) => onUpdateVoice('language', value)}
      />
      {#if voiceDraft.provider === 'local'}
        <RuntimeSelect
          id="voice-model-size"
          label="Model size"
          value={voiceDraft.model_size}
          options={voice.model_size_options}
          disabled={!canManageSettings}
          onValueChange={(value) => onUpdateVoice('model_size', value)}
        />
      {/if}
      <label class="runtime-static-field" for="voice-model">
        <span>Transcription model</span>
        <div id="voice-model">{voice.model}</div>
      </label>
    </div>
  </div>
</section>

<style>
  .runtime-section {
    display: grid;
    gap: 18px;
    min-width: 0;
    padding: 22px 0;
  }

  .runtime-section-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
  }

  .runtime-section-heading div {
    display: grid;
    gap: 7px;
    min-width: 0;
  }

  .runtime-section-heading h2 {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 18px;
    font-weight: 560;
    line-height: 1.2;
    letter-spacing: 0;
  }

  .runtime-section-heading p {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .voice-stack,
  .voice-flow-grid {
    display: grid;
    gap: 14px;
    min-width: 0;
  }

  .runtime-static-field {
    display: grid;
    gap: 7px;
    min-width: 0;
    color: var(--constellation-text-muted);
  }

  .runtime-static-field span {
    min-width: 0;
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .runtime-static-field div {
    display: flex;
    min-height: 42px;
    align-items: center;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 12px;
    background: color-mix(in srgb, var(--constellation-control-input-background) 72%, transparent);
    color: var(--constellation-text-primary);
    padding: 0 12px;
  }
</style>
