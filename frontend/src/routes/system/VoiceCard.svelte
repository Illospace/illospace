<script lang="ts">
  import { ConstellationNotice } from '$lib/components/constellation';

  import './runtime-card.css';
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

  <div class="runtime-stack">
    {#if voiceNotice}
      <ConstellationNotice title={voiceNotice.title} description={voiceNotice.detail} tone="warning" compact />
    {/if}

    <div class="runtime-flow-grid">
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
