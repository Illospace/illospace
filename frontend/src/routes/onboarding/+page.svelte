<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationIcon,
    ConstellationNotice,
    ConstellationPanel,
    ConstellationTextInput,
  } from '$lib/components/constellation';
  import IllospaceLogo from '$lib/components/layout/IllospaceLogo.svelte';
  import type { EmbedderKey, RuntimeOption, RuntimeSettings } from '../system/types';

  type OnboardingStatus = 'loading' | 'missing' | 'connecting' | 'connected' | 'error';
  type OnboardingStep = 'openai' | 'memory';
  type NoticeState = {
    tone: 'success' | 'warning' | 'danger' | 'info';
    title: string;
    detail?: string;
  };

  let status = $state<OnboardingStatus>('loading');
  let notice = $state<NoticeState | null>(null);
  let oauthUrl = $state('');
  let oauthState = $state('');
  let oauthCallback = $state('');
  let oauthCallbackAvailable = $state(true);
  let oauthCallbackMode = $state<'server' | 'local_bridge' | 'manual'>('local_bridge');
  let oauthExchangeInFlight = $state(false);
  let oauthChannel: BroadcastChannel | null = null;
  let runtimeSettings = $state<RuntimeSettings | null>(null);
  let selectedEmbedder = $state<EmbedderKey>('local_gpu');
  let selectedEmbeddingModel = $state('');
  let embedderApiKey = $state('');
  let savingMemory = $state(false);
  let memorySaved = $state(false);
  let activeStep = $state<OnboardingStep>('openai');

  const isWorking = $derived(status === 'loading' || status === 'connecting');
  const showManualCallback = $derived(Boolean(!oauthCallbackAvailable && (oauthUrl || oauthState || status === 'connecting')));
  const connectLabel = $derived(status === 'connected' ? 'Continue' : 'Connect OpenAI');
  const isMemoryStep = $derived(activeStep === 'memory');
  const embedderOptions = $derived(memoryEmbedderOptions());
  const selectedEmbedderNeedsKey = $derived(usesApiEmbedder(selectedEmbedder));
  const selectedEmbedderHasKey = $derived(hasApiKeyForEmbedder(selectedEmbedder));
  const needsMemoryApiKeyInput = $derived(selectedEmbedderNeedsKey && !selectedEmbedderHasKey);
  const selectedEmbedderLabel = $derived(embedderLabel(selectedEmbedder));
  const selectedEmbedderArticle = $derived(selectedEmbedder === 'openai' ? 'an' : 'a');
  const selectedEmbedderPlaceholder = $derived(selectedEmbedder === 'gemini' ? 'Google AI Studio key' : 'sk-...');
  const canManageMemorySetup = $derived(runtimeSettings?.permissions?.can_manage_settings ?? false);
  const showMemorySetup = $derived(canManageMemorySetup && !memorySaved);
  const pageTitle = $derived(isMemoryStep ? 'Set up memory' : 'Connect OpenAI');
  const pageLede = $derived(
    isMemoryStep
      ? 'Choose how Illo should remember, retrieve, and summarize workspace context.'
      : 'Connect your OpenAI account so Illo can start working with you in Cortex.',
  );

  onMount(() => {
    void loadRuntime();
    window.addEventListener('message', handleOpenAIMessage);
    try {
      oauthChannel = new BroadcastChannel('illo:openai-oauth');
      oauthChannel.onmessage = (event) => void handleOpenAIPayload(event.data);
    } catch {
      oauthChannel = null;
    }

    return () => {
      window.removeEventListener('message', handleOpenAIMessage);
      oauthChannel?.close();
      oauthChannel = null;
    };
  });

  async function loadRuntime() {
    status = 'loading';
    notice = null;
    try {
      const runtime = await api.runtimeSettings();
      hydrateRuntime(runtime);
      if (runtimeHasOnboardingOpenAIConnection(runtime)) {
        status = 'connected';
        if (runtimeNeedsMemorySetup(runtime)) {
          activeStep = 'memory';
          return;
        }
        await openCortexIntro();
        return;
      }
      activeStep = 'openai';
      status = 'missing';
    } catch (error) {
      status = 'error';
      notice = errorNotice('Could not load onboarding.', error, 'Refresh and try again.');
    }
  }

  function hydrateRuntime(runtime: RuntimeSettings) {
    runtimeSettings = runtime;
    selectedEmbedder = runtime?.memory?.embedder || 'local_gpu';
    selectedEmbeddingModel = runtime?.memory?.embedding_model || defaultEmbeddingModel(selectedEmbedder, runtime);
    memorySaved = runtime?.memory?.embedding_status === 'ready';
  }

  function runtimeNeedsMemorySetup(runtime: RuntimeSettings) {
    return Boolean(runtime?.permissions?.can_manage_settings && runtime?.memory?.embedding_status !== 'ready');
  }

  function runtimeHasOnboardingOpenAIConnection(runtime: RuntimeSettings) {
    return Boolean(runtime?.connection?.status === 'connected' && ['user_default', 'org_main'].includes(runtime.connection.source || ''));
  }

  function selectEmbedder(embedder: EmbedderKey) {
    selectedEmbedder = embedder;
    selectedEmbeddingModel =
      runtimeSettings?.memory?.embedder === embedder
        ? runtimeSettings.memory.embedding_model || defaultEmbeddingModel(embedder)
        : defaultEmbeddingModel(embedder);
    memorySaved = runtimeSettings?.memory?.embedder === embedder && runtimeSettings?.memory?.embedding_status === 'ready';
    notice = null;
  }

  async function saveMemorySetup() {
    if (!runtimeSettings || !showMemorySetup) return;
    const value = embedderApiKey.trim();
    if (selectedEmbedderNeedsKey && !selectedEmbedderHasKey && !value) {
      notice = { tone: 'warning', title: `Paste ${selectedEmbedderArticle} ${selectedEmbedderLabel} API key first.` };
      return;
    }
    savingMemory = true;
    notice = null;
    try {
      if (selectedEmbedder === 'openai' && !selectedEmbedderHasKey) {
        await api.connectRuntimeOpenAIEmbeddingKey({ api_key: value });
      }
      if (selectedEmbedder === 'gemini' && !selectedEmbedderHasKey) {
        await api.connectRuntimeGeminiKey({ api_key: value });
      }
      const memory = await api.updateRuntimeMemory({
        embedder: selectedEmbedder,
        embedding_model: usesApiEmbedder(selectedEmbedder) ? selectedEmbeddingModel : null,
        reranker: runtimeSettings.memory.reranker || 'weighted',
      });
      embedderApiKey = '';
      runtimeSettings = { ...runtimeSettings, memory };
      memorySaved = memory?.embedding_status === 'ready';
      notice = {
        tone: 'success',
        title: 'Memory setup saved.',
        detail: `${selectedEmbedderLabel} will power memory, retrieval, and summaries.`,
      };
      if (memorySaved) activeStep = 'openai';
      await openCortexIntro();
    } catch (error) {
      notice = errorNotice('Memory setup was not saved.', error, 'Skip for now, or update it later in AI Runtime.');
    } finally {
      savingMemory = false;
    }
  }

  async function skipMemorySetup() {
    notice = {
      tone: 'info',
      title: 'Memory setup skipped.',
      detail: 'You can configure memory later in AI Runtime.',
    };
    await openCortexIntro();
  }

  function memoryEmbedderOptions(): RuntimeOption[] {
    const options = runtimeSettings?.memory?.embedder_options || [
      { key: 'openai', label: 'OpenAI' },
      { key: 'gemini', label: 'Gemini' },
      { key: 'local_gpu', label: 'Local GPU' },
    ];
    const openai = options.find((option) => option.key === 'openai');
    const gemini = options.find((option) => option.key === 'gemini');
    const local =
      (isLocalEmbedder(selectedEmbedder) && options.find((option) => option.key === selectedEmbedder)) ||
      options.find((option) => option.key === 'local_gpu') ||
      options.find((option) => option.key === 'local_cpu');

    return [
      openai && { ...openai, label: 'OpenAI' },
      gemini && { ...gemini, label: 'Google' },
      local && { ...local, label: 'Local' },
    ].filter(Boolean) as RuntimeOption[];
  }

  function embedderLabel(embedder: EmbedderKey) {
    if (embedder === 'gemini') return 'Google';
    if (embedder === 'openai') return 'OpenAI';
    return 'Local';
  }

  function optionLabel(option: RuntimeOption) {
    if (option.key === 'gemini') return 'Google';
    if (option.key === 'local_gpu') return 'Local';
    if (option.key === 'local_cpu') return 'Local';
    return option.label;
  }

  function usesApiEmbedder(embedder: EmbedderKey) {
    return embedder === 'openai' || embedder === 'gemini';
  }

  function isLocalEmbedder(embedder: EmbedderKey) {
    return embedder === 'local_cpu' || embedder === 'local_gpu';
  }

  function embeddingProvider(embedder: EmbedderKey) {
    return embedder === 'gemini' ? 'gemini' : 'openai';
  }

  function hasApiKeyForEmbedder(embedder: EmbedderKey) {
    if (!usesApiEmbedder(embedder)) return true;
    return Boolean(runtimeSettings?.memory?.api_key_statuses?.[embeddingProvider(embedder)]);
  }

  function defaultEmbeddingModel(embedder: EmbedderKey, source = runtimeSettings) {
    const provider = embeddingProvider(embedder);
    const option = (source?.memory?.embedding_model_options || []).find((item) => item.group === provider);
    return option?.key || (embedder === 'gemini' ? 'gemini-embedding-2' : 'text-embedding-3-small');
  }

  async function primaryAction() {
    if (status === 'connected') {
      await openCortexIntro();
      return;
    }
    await startOpenAI();
  }

  async function startOpenAI() {
    status = 'connecting';
    notice = null;
    try {
      const result = await api.startRuntimeOpenAIOAuth({ callback_mode: 'auto' });
      oauthUrl = result.url || '';
      oauthState = result.state || '';
      oauthCallbackAvailable = result.callback_available ?? true;
      oauthCallbackMode = result.callback_mode || 'local_bridge';

      if (oauthUrl && typeof window !== 'undefined') {
        const popup = window.open('about:blank', 'illo-openai-oauth', 'popup,width=540,height=760');
        if (popup) {
          popup.location.href = oauthUrl;
          popup.focus();
        } else {
          window.location.assign(oauthUrl);
          return;
        }
      }

      notice = oauthCallbackAvailable
        ? {
            tone: 'info',
            title: 'OpenAI sign-in opened.',
            detail:
              oauthCallbackMode === 'server'
                ? 'Finish in the OpenAI window. It will return automatically.'
                : 'Finish in the OpenAI window. This page will update when it returns.',
          }
        : {
            tone: 'warning',
            title: 'OpenAI sign-in opened.',
            detail: result.callback_detail || 'Finish sign-in, then paste the callback URL below.',
          };
    } catch (error) {
      status = 'missing';
      notice = errorNotice('Could not start OpenAI sign-in.', error, 'Try again.');
    }
  }

  async function finishManualCallback() {
    const callback = oauthCallback.trim();
    if (!callback) {
      notice = { tone: 'warning', title: 'Paste the callback URL first.' };
      return;
    }
    await completeOpenAI(callback);
  }

  async function completeOpenAI(callback: string) {
    if (oauthExchangeInFlight) return;
    oauthExchangeInFlight = true;
    status = 'connecting';
    notice = null;
    try {
      await api.exchangeRuntimeOpenAIOAuth({ callback });
      await confirmOpenAIConnection();
    } catch (error) {
      status = 'missing';
      notice = errorNotice('OpenAI sign-in failed.', error, 'Start the sign-in again.');
    } finally {
      oauthExchangeInFlight = false;
    }
  }

  function handleOpenAIMessage(event: MessageEvent) {
    if (!isTrustedOAuthOrigin(event.origin)) return;
    void handleOpenAIPayload(event.data);
  }

  async function handleOpenAIPayload(payload: unknown) {
    const data = payload as { type?: string; status?: string; state?: string; callback?: string; detail?: string } | null;
    if (!data || data.type !== 'illo:openai-oauth') return;
    if (oauthState && data.state && data.state !== oauthState) return;
    if (oauthState && data.status === 'success' && !data.state) return;

    if (data.status === 'callback' && data.callback) {
      await completeOpenAI(data.callback);
      return;
    }

    if (data.status === 'success') {
      await confirmOpenAIConnection();
      return;
    }

    if (data.status === 'error') {
      status = 'missing';
      if (oauthCallbackMode === 'server') {
        oauthCallbackAvailable = false;
      }
      notice = {
        tone: 'danger',
        title: 'OpenAI sign-in failed.',
        detail: data.detail || 'Start the sign-in again.',
      };
    }
  }

  async function confirmOpenAIConnection() {
    status = 'connecting';
    try {
      const runtime = await api.runtimeSettings();
      if (runtimeHasOnboardingOpenAIConnection(runtime)) {
        hydrateRuntime(runtime);
        oauthUrl = '';
        oauthState = '';
        oauthCallback = '';
        oauthCallbackAvailable = true;
        oauthCallbackMode = 'local_bridge';
        status = 'connected';
        if (runtimeNeedsMemorySetup(runtime)) {
          activeStep = 'memory';
          notice = {
            tone: 'success',
            title: 'OpenAI connected.',
            detail: 'Set up memory now, or skip and configure it later in AI Runtime.',
          };
          return;
        }
        await openCortexIntro();
        return;
      }
      status = 'missing';
      notice = {
        tone: 'warning',
        title: 'OpenAI is not connected yet.',
        detail: 'Finish the callback or start the sign-in again.',
      };
    } catch (error) {
      status = 'missing';
      notice = errorNotice('Could not confirm OpenAI.', error, 'Refresh and try again.');
    }
  }

  async function openCortexIntro() {
    status = 'connected';
    notice = { tone: 'success', title: 'OpenAI connected.' };
    try {
      const params = new URLSearchParams({ onboarding: 'runtime-ready' });
      await goto(`/cortex?${params.toString()}`);
    } catch (error) {
      status = 'connected';
      notice = errorNotice('OpenAI connected, but Cortex did not open.', error, 'Open Cortex from the sidebar.');
    }
  }

  function isTrustedOAuthOrigin(origin: string) {
    if (typeof window === 'undefined') return false;
    return origin === window.location.origin || isLocalOpenAICallbackOrigin(origin);
  }

  function isLocalOpenAICallbackOrigin(origin: string) {
    try {
      const url = new URL(origin);
      return url.protocol === 'http:' && url.port === '1455' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    } catch {
      return false;
    }
  }

  function errorNotice(title: string, error: unknown, fallback: string): NoticeState {
    const detail =
      error instanceof Error
        ? error.message
        : typeof error === 'object' && error !== null && 'detail' in error
          ? String((error as { detail?: unknown }).detail || fallback)
          : fallback;
    return { tone: 'danger', title, detail };
  }
</script>

<svelte:head>
  <title>{pageTitle}</title>
</svelte:head>

<main class="onboarding-shell">
  <section class="onboarding-stage">
    <div class="onboarding-brand">
      <IllospaceLogo className="onboarding-logo" variant="small" title="Illospace" />
    </div>

    <ConstellationPanel padding="lg" tone={status === 'connected' ? 'success' : 'info'} className="onboarding-panel">
      <div class="onboarding-copy">
        <p class="eyebrow">Workspace ready</p>
        <h1>{pageTitle}</h1>
        <p class="lede">{pageLede}</p>
      </div>

      <div class="step-strip" aria-label="Onboarding progress">
        <div class:active={!isMemoryStep} class:complete={status === 'connected'} class="step-pill">
          <span>1</span>
          <strong>OpenAI</strong>
        </div>
        <div class:active={isMemoryStep} class:pending={!isMemoryStep} class="step-pill">
          <span>2</span>
          <strong>Memory</strong>
        </div>
      </div>

      {#if notice}
        <ConstellationNotice title={notice.title} description={notice.detail || ''} tone={notice.tone} compact />
      {/if}

      {#if !isMemoryStep}
        <div class="setup-section">
          <div class="section-copy">
            <div class="section-title">
              <span class:connected={status === 'connected'}></span>
              <div>
                <strong>{status === 'connected' ? 'OpenAI connected' : 'OpenAI account'}</strong>
                <p>
                  {#if status === 'connecting'}
                    Waiting for OpenAI to finish.
                  {:else if status === 'connected'}
                    Continue and Illo will introduce itself.
                  {:else}
                    Required for Illo to think and respond.
                  {/if}
                </p>
              </div>
            </div>
          </div>

          <ConstellationButton onclick={primaryAction} loading={isWorking} loadingLabel="Working">
            {#snippet leadingVisual()}
              <ConstellationIcon name={status === 'connected' ? 'cortex' : 'external-link'} size={14} />
            {/snippet}
            {connectLabel}
          </ConstellationButton>

          {#if showManualCallback}
            <div class="manual-callback">
              <label for="openai-callback">Callback URL</label>
              <div class="manual-row">
                <ConstellationTextInput
                  id="openai-callback"
                  bind:value={oauthCallback}
                  placeholder="http://localhost:1455/auth/callback?code=..."
                  autocomplete="off"
                />
                <ConstellationButton variant="secondary" onclick={finishManualCallback} loading={oauthExchangeInFlight}>
                  Finish
                </ConstellationButton>
              </div>
            </div>
          {/if}
        </div>
      {:else if showMemorySetup}
        <div class="setup-section memory-section">
          <div class="section-copy memory-copy">
            <div class="section-title">
              <span></span>
              <div>
                <strong>Memory setup</strong>
                <p>Optional for onboarding, recommended for context-aware work.</p>
              </div>
            </div>
          </div>

          <div class="memory-controls" class:has-key-input={needsMemoryApiKeyInput}>
            <div class="embedder-choice" aria-label="Memory embedder">
              {#each embedderOptions as option}
                <button
                  type="button"
                  class:selected={selectedEmbedder === option.key}
                  disabled={option.disabled}
                  onclick={() => selectEmbedder(option.key as EmbedderKey)}
                >
                  {optionLabel(option)}
                </button>
              {/each}
            </div>

            {#if needsMemoryApiKeyInput}
              <ConstellationTextInput
                bind:value={embedderApiKey}
                type="password"
                placeholder={selectedEmbedderPlaceholder}
                autocomplete="off"
              />
            {/if}

            <div class="memory-actions">
              <ConstellationButton
                className="memory-save-button"
                onclick={saveMemorySetup}
                loading={savingMemory}
                loadingLabel="Saving"
              >
                Confirm and continue
              </ConstellationButton>
              <ConstellationButton variant="secondary" onclick={skipMemorySetup} disabled={savingMemory}>
                Skip for now
              </ConstellationButton>
            </div>
          </div>

          <p class="runtime-note">You can configure this later in AI Runtime.</p>
        </div>
      {/if}

    </ConstellationPanel>
  </section>
</main>

<style>
  .onboarding-shell {
    position: relative;
    min-height: 100vh;
    overflow: hidden;
    isolation: isolate;
    background: var(--constellation-workspace-theme-background);
  }

  .onboarding-stage {
    display: grid;
    width: min(100%, 720px);
    min-height: 100vh;
    align-content: center;
    gap: 18px;
    margin: 0 auto;
    box-sizing: border-box;
    padding: 42px 24px;
  }

  .onboarding-brand {
    display: flex;
    justify-content: center;
  }

  .onboarding-brand :global(.onboarding-logo) {
    width: 112px;
    height: 64px;
  }

  :global(.onboarding-panel .constellation-panel-content) {
    display: grid;
    gap: 18px;
  }

  .onboarding-copy {
    display: grid;
    gap: 8px;
  }

  .eyebrow,
  h1,
  .lede,
  .section-title p,
  .manual-callback label,
  .runtime-note {
    margin: 0;
  }

  .eyebrow,
  .manual-callback label {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  h1 {
    color: var(--constellation-color-text-primary);
    font-size: 26px;
    font-weight: 620;
    line-height: 1.12;
    letter-spacing: 0;
  }

  .lede,
  .section-title p,
  .runtime-note {
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.45;
  }

  .step-strip {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .step-pill {
    display: flex;
    align-items: center;
    gap: 9px;
    min-width: 0;
    min-height: 42px;
    padding: 8px 10px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-surface-nested-background) 64%, transparent);
    color: var(--constellation-color-text-secondary);
  }

  .step-pill span {
    display: inline-grid;
    flex: 0 0 auto;
    width: 22px;
    height: 22px;
    place-items: center;
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
  }

  .step-pill strong {
    min-width: 0;
    overflow-wrap: anywhere;
    font-size: 13px;
    font-weight: 650;
  }

  .step-pill.active {
    border-color: color-mix(in srgb, var(--constellation-color-accent, #5ecfa0) 42%, var(--constellation-surface-panel-separator));
    background: color-mix(in srgb, var(--constellation-color-accent, #5ecfa0) 10%, var(--constellation-surface-nested-background));
    color: var(--constellation-color-text-primary);
  }

  .step-pill.complete span {
    background: var(--constellation-control-pill-success-text);
    color: var(--constellation-surface-panel-background);
  }

  .step-pill.pending {
    opacity: 0.66;
  }

  .setup-section {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 16px;
    align-items: center;
    min-width: 0;
    padding: 15px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-surface-nested-background) 70%, transparent);
  }

  .memory-section {
    grid-template-columns: 1fr;
    gap: 14px;
    align-items: stretch;
  }

  .section-title {
    display: flex;
    gap: 12px;
    align-items: center;
    min-width: 0;
  }

  .section-title > span {
    flex: 0 0 auto;
    width: 9px;
    height: 9px;
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-color-text-muted);
    opacity: 0.7;
  }

  .section-title > span.connected {
    background: var(--constellation-control-pill-success-text);
    box-shadow: 0 0 14px color-mix(in srgb, var(--constellation-control-pill-success-text) 42%, transparent);
    opacity: 1;
  }

  .section-title strong {
    display: block;
    margin-bottom: 3px;
    color: var(--constellation-color-text-primary);
    font-size: 14px;
    font-weight: 600;
  }

  .memory-controls {
    display: grid;
    grid-template-columns: minmax(260px, 1fr) auto;
    gap: 12px;
    align-items: center;
    min-width: 0;
  }

  .memory-controls.has-key-input {
    grid-template-columns: minmax(210px, 0.9fr) minmax(210px, 1fr);
  }

  .memory-controls :global(.constellation-text-input) {
    min-height: 40px;
  }

  .memory-controls :global(.memory-save-button) {
    justify-self: end;
  }

  .memory-controls.has-key-input .memory-actions {
    grid-column: 1 / -1;
    justify-self: end;
  }

  .memory-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    justify-content: flex-end;
    min-width: 0;
  }

  .embedder-choice {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 4px;
    min-width: 0;
    padding: 4px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-control-field-background) 74%, transparent);
  }

  .embedder-choice button {
    min-width: 0;
    height: 32px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    cursor: pointer;
  }

  .embedder-choice button.selected {
    background: var(--constellation-button-secondary-background);
    color: var(--constellation-control-button-secondary-text);
    box-shadow: var(--constellation-button-secondary-shadow);
  }

  .embedder-choice button:disabled {
    cursor: not-allowed;
    opacity: 0.48;
  }

  .embedder-choice button:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .manual-callback {
    display: grid;
    grid-column: 1 / -1;
    gap: 10px;
  }

  .manual-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
  }

  @media (max-width: 760px) {
    .onboarding-stage {
      align-content: start;
      padding: 30px 18px;
    }

    .setup-section,
    .memory-controls,
    .memory-controls.has-key-input,
    .manual-row {
      grid-template-columns: 1fr;
      align-items: stretch;
    }

    .step-strip {
      grid-template-columns: 1fr;
    }

    .memory-controls.has-key-input .memory-actions {
      grid-column: auto;
      justify-self: stretch;
    }

    .memory-actions {
      display: grid;
      grid-template-columns: 1fr;
      justify-content: stretch;
    }

    .memory-actions :global(.constellation-button) {
      width: 100%;
    }

    .memory-controls :global(.memory-save-button) {
      width: 100%;
      justify-self: stretch;
    }
  }
</style>
