<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationIcon,
    ConstellationNotice,
    ConstellationPageFrame,
  } from '$lib/components/constellation';

  import AccessCard from './AccessCard.svelte';
  import MemoryCard from './MemoryCard.svelte';
  import ModelsCard from './ModelsCard.svelte';
  import StartupGuide from './StartupGuide.svelte';
  import type {
    EmbedderKey,
    MemoryCheck,
    MemoryDraft,
    MemoryNoticeState,
    ModelTier,
    NoticeState,
    PillTone,
    RuntimeOption,
    RuntimeSettings,
    StartupGuideStep,
    StartupStepKey,
  } from './types';

  type CodexSignInCallbackMode = 'auto' | 'server' | 'local_bridge';

  let settings = $state<RuntimeSettings | null>(null);
  let loading = $state(true);
  let loadError = $state('');
  let apiKey = $state('');
  let openaiEmbedderApiKey = $state('');
  let oauthCallback = $state('');
  let oauthUrl = $state('');
  let oauthState = $state('');
  let oauthCallbackAvailable = $state(true);
  let oauthCallbackMode = $state<'server' | 'local_bridge' | 'manual'>('local_bridge');
  let oauthChannel: BroadcastChannel | null = null;
  let oauthExchangeInFlight = false;
  let savingConnection = $state(false);
  let savingOpenAIEmbedderKey = $state(false);
  let savingModels = $state(false);
  let savingMemory = $state(false);
  let checkingMemory = $state(false);
  let memoryCheck = $state<MemoryCheck | null>(null);
  let notice = $state<NoticeState | null>(null);
  let geminiApiKey = $state('');
  let savingGeminiKey = $state(false);
  let startingIntro = $state(false);
  let setupEmbedderPromptSkipped = $state(false);
  let modelDraft = $state<Record<ModelTier, string>>({ low: '', medium: '', high: '' });
  let memoryDraft = $state<MemoryDraft>({
    embedder: 'local_gpu',
    embedding_model: 'text-embedding-3-small',
    reranker: 'weighted',
  });

  const canManageSettings = $derived(settings?.permissions?.can_manage_settings ?? false);
  const connectionStatus = $derived(settings?.connection?.status ?? 'missing');
  const connectionTone = $derived<PillTone>(connectionStatus === 'connected' ? 'success' : 'warning');
  const memoryStatus = $derived(memoryCardStatus());
  const memoryTone = $derived<PillTone>(memoryCardTone());
  const modelOptions = $derived(settings?.models?.options ?? []);
  const setupMode = $derived($page.url.searchParams.get('setup') === '1');
  const setupCanContinue = $derived(setupMode && connectionStatus === 'connected');
  const hasEmbeddingApiKey = $derived(Boolean(settings?.memory.api_key_statuses?.openai || settings?.memory.api_key_statuses?.gemini));
  const showEmbedderKeyPrompt = $derived(setupMode && canManageSettings && !hasEmbeddingApiKey && !setupEmbedderPromptSkipped);
  const startupGuideSteps = $derived(buildStartupGuideSteps());
  const showStartupGuide = $derived(shouldShowStartupGuide());

  onMount(() => {
    loadSettings();
    window.addEventListener('message', handleCodexSignInMessage);
    try {
      oauthChannel = new BroadcastChannel('illo:openai-oauth');
      oauthChannel.onmessage = (event) => void handleCodexSignInPayload(event.data);
    } catch {
      oauthChannel = null;
    }

    return () => {
      window.removeEventListener('message', handleCodexSignInMessage);
      oauthChannel?.close();
      oauthChannel = null;
    };
  });

  function hydrate(next: RuntimeSettings) {
    settings = next;
    modelDraft = {
      low: next.models.low,
      medium: next.models.medium,
      high: next.models.high,
    };
    memoryDraft = {
      embedder: next.memory.embedder,
      embedding_model: next.memory.embedding_model || defaultEmbeddingModel(next.memory.embedder, next),
      reranker: next.memory.reranker || 'weighted',
    };
  }

  async function loadSettings() {
    loading = true;
    loadError = '';
    try {
      hydrate(await api.runtimeSettings());
    } catch (error) {
      loadError = error instanceof Error ? error.message : 'System setup failed to load.';
    } finally {
      loading = false;
    }
  }

  async function connectWithApiKey() {
    const value = apiKey.trim();
    if (!value) {
      notice = { tone: 'warning', title: 'Paste an OpenAI API key first.' };
      return;
    }
    savingConnection = true;
    notice = null;
    try {
      await api.connectRuntimeOpenAIKey({ api_key: value });
      apiKey = '';
      notice = {
        tone: 'success',
        title: canManageSettings ? 'Workspace OpenAI key connected.' : 'OpenAI API key connected.',
      };
      await openRuntimeReadyIntro({ openExisting: true });
    } catch (error) {
      notice = errorNotice('Connection failed.', error, 'OpenAI rejected the credential.');
    } finally {
      savingConnection = false;
    }
  }

  async function connectWithOpenAIEmbeddingKey() {
    const value = openaiEmbedderApiKey.trim();
    if (!value) {
      notice = { tone: 'warning', title: 'Paste an OpenAI API key first.' };
      return;
    }
    savingOpenAIEmbedderKey = true;
    notice = null;
    try {
      const memory = await api.connectRuntimeOpenAIEmbeddingKey({ api_key: value });
      openaiEmbedderApiKey = '';
      setupEmbedderPromptSkipped = true;
      if (settings) hydrate({ ...settings, memory });
      notice = {
        tone: 'success',
        title: 'Workspace memory key saved.',
        detail: 'Memory and retrieval can use OpenAI embeddings across the workspace.',
      };
    } catch (error) {
      notice = errorNotice('Memory key was not saved.', error, 'Check the OpenAI API key.');
    } finally {
      savingOpenAIEmbedderKey = false;
    }
  }

  async function connectWithGeminiKey() {
    const value = geminiApiKey.trim();
    if (!value) {
      notice = { tone: 'warning', title: 'Paste a Gemini API key first.' };
      return;
    }
    savingGeminiKey = true;
    notice = null;
    try {
      await api.connectRuntimeGeminiKey({ api_key: value });
      geminiApiKey = '';
      setupEmbedderPromptSkipped = true;
      notice = { tone: 'success', title: 'Gemini API key connected.' };
      await loadSettings();
    } catch (error) {
      notice = errorNotice('Gemini key was not saved.', error, 'Check the Google AI Studio key.');
    } finally {
      savingGeminiKey = false;
    }
  }

  function normalizeCodexSignInCallbackMode(value: unknown): CodexSignInCallbackMode {
    return value === 'server' || value === 'local_bridge' ? value : 'auto';
  }

  async function startCodexSignIn(callbackMode: unknown = 'auto') {
    const requestedCallbackMode = normalizeCodexSignInCallbackMode(callbackMode);
    savingConnection = true;
    notice = null;
    try {
      const result = await api.startRuntimeOpenAIOAuth({ callback_mode: requestedCallbackMode });
      oauthUrl = result.url || '';
      oauthState = result.state || '';
      oauthCallbackAvailable = result.callback_available ?? true;
      oauthCallbackMode = result.callback_mode || 'local_bridge';
      let openedOAuthWindow = false;
      if (oauthUrl && typeof window !== 'undefined') {
        const popup = window.open('about:blank', 'illo-openai-oauth', 'popup,width=540,height=760');
        if (popup) {
          popup.location.href = oauthUrl;
          popup.focus();
          openedOAuthWindow = true;
        }
      }
      notice = oauthCallbackAvailable
        ? {
            tone: 'info',
            title: openedOAuthWindow ? 'Codex sign-in opened.' : 'Codex sign-in ready.',
            detail:
              oauthCallbackMode === 'server'
                ? 'Finish in the OpenAI window. It should return to this Illo server automatically; paste the callback URL below if it does not.'
                : openedOAuthWindow
                  ? 'Finish in the OpenAI window. This page should update automatically; paste the callback URL below if it does not.'
                  : 'Open OpenAI sign-in below. This page should update automatically; paste the callback URL below if it does not.',
          }
        : {
            tone: 'warning',
            title: openedOAuthWindow ? 'Codex sign-in opened.' : 'Codex sign-in ready.',
            detail: result.callback_detail || 'The automatic callback bridge is unavailable. Use the manual callback fallback if the window cannot return here.',
          };
    } catch (error) {
      notice = errorNotice('Could not start Codex sign-in.', error, 'Try again from the System tab.');
    } finally {
      savingConnection = false;
    }
  }

  async function finishCodexSignIn() {
    const callback = oauthCallback.trim();
    if (!callback) {
      notice = { tone: 'warning', title: 'Paste the callback URL first.' };
      return;
    }
    await completeCodexSignIn(callback);
  }

  async function completeCodexSignIn(callback: string) {
    const cleanCallback = callback.trim();
    if (!cleanCallback || oauthExchangeInFlight) return;
    oauthExchangeInFlight = true;
    savingConnection = true;
    notice = null;
    try {
      await api.exchangeRuntimeOpenAIOAuth({ callback: cleanCallback });
      oauthCallback = '';
      oauthUrl = '';
      oauthState = '';
      oauthCallbackMode = 'local_bridge';
      notice = { tone: 'success', title: 'Codex connected.' };
      await openRuntimeReadyIntro({ openExisting: true });
    } catch (error) {
      notice = errorNotice('Codex sign-in failed.', error, 'Start the sign-in again.');
    } finally {
      oauthExchangeInFlight = false;
      savingConnection = false;
    }
  }

  function handleCodexSignInMessage(event: MessageEvent) {
    if (!isTrustedOAuthOrigin(event.origin)) return;
    void handleCodexSignInPayload(event.data);
  }

  async function handleCodexSignInPayload(payload: unknown) {
    const data = payload as { type?: string; status?: string; state?: string; callback?: string; detail?: string } | null;
    if (!data || data.type !== 'illo:openai-oauth') return;
    if (oauthState && data.state && data.state !== oauthState) return;
    if (oauthState && data.status === 'success' && !data.state) return;

    if (data.status === 'callback' && data.callback) {
      oauthCallback = data.callback;
      void completeCodexSignIn(data.callback);
      return;
    }

    if (data.status === 'success') {
      oauthCallback = '';
      oauthUrl = '';
      oauthState = '';
      oauthCallbackMode = 'local_bridge';
      savingConnection = true;
      try {
        const next = await api.runtimeSettings();
        hydrate(next);
        if (next.connection.status === 'connected') {
          notice = { tone: 'success', title: 'Codex connected.' };
          await openRuntimeReadyIntro({ openExisting: true });
        } else {
          notice = {
            tone: 'warning',
            title: 'Codex sign-in is not connected yet.',
            detail: 'Finish the OpenAI callback or start the sign-in again.',
          };
        }
      } catch (error) {
        notice = errorNotice('Could not confirm Codex sign-in.', error, 'Refresh System and try again.');
      } finally {
        savingConnection = false;
      }
      return;
    }

    if (data.status === 'error') {
      if (oauthCallbackMode === 'server') {
        oauthCallbackAvailable = false;
      }
      notice = {
        tone: 'danger',
        title: 'Codex sign-in failed.',
        detail: data.detail || 'Start the sign-in again.',
      };
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

  async function openRuntimeReadyIntro({ openExisting = false } = {}) {
    if (startingIntro) return;
    startingIntro = true;
    try {
      const params = new URLSearchParams({ onboarding: 'runtime-ready' });
      if (openExisting) params.set('open_existing', '1');
      await goto(`/cortex?${params.toString()}`);
    } catch (error) {
      await loadSettings();
      notice = errorNotice(
        'Runtime connected, but Cortex did not open.',
        error,
        'Open Cortex to continue.',
      );
    } finally {
      startingIntro = false;
    }
  }

  async function continueToCortex() {
    await openRuntimeReadyIntro({ openExisting: true });
  }

  async function saveModels() {
    if (!canManageSettings) return;
    savingModels = true;
    notice = null;
    try {
      const models = await api.updateRuntimeModels(modelDraft);
      if (settings) settings = { ...settings, models };
      notice = { tone: 'success', title: 'Model routing saved.' };
    } catch (error) {
      notice = errorNotice('Models were not saved.', error, 'Check the selected models.');
    } finally {
      savingModels = false;
    }
  }

  async function saveMemory() {
    if (!canManageSettings || memoryChangeNeedsRebuild()) return;
    savingMemory = true;
    notice = null;
    memoryCheck = null;
    try {
      const memory = await api.updateRuntimeMemory(memoryPayload());
      if (settings) settings = { ...settings, memory };
      notice = { tone: 'success', title: 'Memory setup saved.' };
    } catch (error) {
      notice = errorNotice('Memory setup was not saved.', error, 'Check the selected memory settings.');
    } finally {
      savingMemory = false;
    }
  }

  async function checkMemory() {
    if (!canManageSettings || memoryChangeNeedsRebuild()) return;
    checkingMemory = true;
    memoryCheck = null;
    try {
      const memory = await api.updateRuntimeMemory(memoryPayload());
      if (settings) settings = { ...settings, memory };
      memoryCheck = await api.checkRuntimeMemory();
    } catch (error) {
      memoryCheck = {
        status: 'error',
        detail: error instanceof Error ? error.message : 'Memory check failed.',
      };
    } finally {
      checkingMemory = false;
    }
  }

  function updateModelTier(tier: ModelTier, value: string) {
    modelDraft = { ...modelDraft, [tier]: value };
  }

  function updateMemoryDraft(key: keyof MemoryDraft, value: string) {
    if (key === 'embedder') {
      const embedder = value as EmbedderKey;
      memoryDraft = {
        ...memoryDraft,
        embedder,
        embedding_model: defaultEmbeddingModel(embedder),
      };
    } else {
      memoryDraft = { ...memoryDraft, [key]: value } as MemoryDraft;
    }
    memoryCheck = null;
  }

  function memoryPayload() {
    return {
      embedder: memoryDraft.embedder,
      embedding_model: usesApiEmbedder(memoryDraft.embedder) ? memoryDraft.embedding_model : null,
      reranker: memoryDraft.reranker,
    };
  }

  function optionLabel(options: RuntimeOption[], key: string | null | undefined) {
    return options.find((option) => option.key === key)?.label ?? key ?? 'Unset';
  }

  function connectionSummary() {
    if (!settings || settings.connection.status !== 'connected') {
      return 'Connect Codex or OpenAI for models. Owner API keys can also power workspace memory.';
    }
    const label = settings.connection.label || (settings.connection.method === 'chatgpt' ? 'Codex / ChatGPT' : 'OpenAI API key');
    return `${label} for models. Memory keys apply across the workspace.`;
  }

  function modelSummary() {
    if (!settings) return 'Choose low, medium, and high models.';
    return `Low handles summaries. Medium is ${optionLabel(modelOptions, settings.models.medium)}.`;
  }

  function memorySummary() {
    if (!settings) return 'Choose an embedder and ranking mode.';
    const dimensions = memoryDraftDimensions();
    return `${optionLabel(settings.memory.embedder_options, memoryDraft.embedder)} - ${dimensions || 'unknown'} dimensions, workspace-wide`;
  }

  function memoryCardStatus() {
    if (memoryDraftDirty()) return 'pending';
    return settings?.memory?.embedding_status ?? 'unknown';
  }

  function memoryCardTone(): PillTone {
    if (memoryDraftDirty()) return 'info';
    return settings?.memory?.embedding_status === 'ready' ? 'success' : 'warning';
  }

  function memoryDraftDirty() {
    if (!settings) return false;
    const savedModel = settings.memory.embedding_model || defaultEmbeddingModel(settings.memory.embedder);
    return (
      memoryDraft.embedder !== settings.memory.embedder ||
      (usesApiEmbedder(memoryDraft.embedder) && memoryDraft.embedding_model !== savedModel) ||
      memoryDraft.reranker !== settings.memory.reranker
    );
  }

  function memoryChangeNeedsRebuild() {
    if (!settings || !memoryDraftDirty() || (settings.memory.indexed_vectors || 0) === 0) return false;
    const savedModel = settings.memory.embedding_model || defaultEmbeddingModel(settings.memory.embedder);
    return (
      memoryDraft.embedder !== settings.memory.embedder ||
      (usesApiEmbedder(memoryDraft.embedder) && memoryDraft.embedding_model !== savedModel)
    );
  }

  function memoryDraftDimensions() {
    if (memoryDraft.embedder === 'openai') return 768;
    if (memoryDraft.embedder === 'gemini') return 768;
    if (memoryDraft.embedder === 'local_cpu') return 384;
    if (memoryDraft.embedder === 'local_gpu') return 2000;
    return settings?.memory?.embedding_dimensions ?? null;
  }

  function localEmbedderLabel(embedder: EmbedderKey) {
    if (embedder === 'local_cpu') return 'Local CPU';
    if (embedder === 'local_gpu') return 'Local GPU';
    if (embedder === 'gemini') return 'Gemini';
    return 'OpenAI';
  }

  function usesApiEmbedder(embedder: EmbedderKey) {
    return embedder === 'openai' || embedder === 'gemini';
  }

  function embeddingProvider(embedder: EmbedderKey) {
    return embedder === 'gemini' ? 'gemini' : 'openai';
  }

  function embeddingModelOptions(embedder = memoryDraft.embedder, source = settings) {
    const provider = embeddingProvider(embedder);
    return (source?.memory.embedding_model_options ?? []).filter((option) => option.group === provider);
  }

  function defaultEmbeddingModel(embedder: EmbedderKey, source = settings) {
    return embeddingModelOptions(embedder, source)[0]?.key || (embedder === 'gemini' ? 'gemini-embedding-2' : 'text-embedding-3-small');
  }

  function hasApiKeyForEmbedder(embedder: EmbedderKey) {
    return Boolean(settings?.memory.api_key_statuses?.[embeddingProvider(embedder)]);
  }

  function accessReady() {
    return settings?.connection.status === 'connected';
  }

  function modelsReady() {
    return Boolean(modelDraft.low && modelDraft.medium && modelDraft.high);
  }

  function memoryReady() {
    return Boolean(settings && !memoryDraftDirty() && settings.memory.embedding_status === 'ready');
  }

  function shouldShowStartupGuide() {
    if (!settings) return false;
    return !accessReady() || !modelsReady() || !memoryReady();
  }

  function buildStartupGuideSteps(): StartupGuideStep[] {
    if (!settings) return [];
    const accessDone = accessReady();
    const modelsDone = modelsReady();
    const memoryDone = memoryReady();
    const memoryBlocked = memoryChangeNeedsRebuild();
    return [
      {
        key: 'access',
        title: 'Connect Access',
        status: accessDone ? 'complete' : 'current',
        detail: accessDone
          ? connectionSummary()
          : 'Sign in with Codex or paste a workspace OpenAI key. Add an API key when cloud memory uses OpenAI or Gemini.',
      },
      {
        key: 'models',
        title: 'Choose Models',
        status: !canManageSettings ? 'blocked' : modelsDone ? 'complete' : accessDone ? 'current' : 'pending',
        detail: canManageSettings
          ? modelsDone
            ? `Low ${optionLabel(modelOptions, modelDraft.low)}, medium ${optionLabel(modelOptions, modelDraft.medium)}, high ${optionLabel(modelOptions, modelDraft.high)}.`
            : 'Pick one route for low, medium, and high work.'
          : 'An owner or admin chooses the low, medium, and high routes.',
      },
      {
        key: 'memory',
        title: 'Set Up Memory',
        status: !canManageSettings ? 'blocked' : memoryBlocked ? 'blocked' : memoryDone ? 'complete' : accessDone && modelsDone ? 'current' : 'pending',
        detail: memoryGuideDetail(memoryDone, memoryBlocked),
      },
    ];
  }

  function memoryGuideDetail(memoryDone: boolean, memoryBlocked: boolean) {
    if (!settings) return 'Choose one workspace-wide embedder, then Save & check.';
    if (!canManageSettings) return 'An owner or admin chooses one workspace-wide embedder and runs Save & check.';
    if (memoryBlocked) return 'Existing workspace vectors need a rebuild before changing embedder or embedding model.';
    if (memoryDone) return `${localEmbedderLabel(settings.memory.embedder)} memory is ready across this workspace.`;
    if (usesApiEmbedder(memoryDraft.embedder) && !hasApiKeyForEmbedder(memoryDraft.embedder)) {
      const keyLabel = memoryDraft.embedder === 'gemini' ? 'Gemini' : 'OpenAI';
      const article = memoryDraft.embedder === 'openai' ? 'an' : 'a';
      return `Add ${article} ${keyLabel} workspace API key, then Save & check.`;
    }
    return 'Choose one workspace-wide embedder, then Save & check.';
  }

  function memoryNotice(): MemoryNoticeState | null {
    if (!settings) return null;
    if (memoryChangeNeedsRebuild()) {
      return {
        tone: 'warning',
        title: 'Memory rebuild required.',
        detail: `${settings.memory.indexed_vectors} workspace vectors use ${localEmbedderLabel(settings.memory.embedder)}. Changing embedder or embedding model needs a rebuild before it is safe.`,
      };
    }
    if (memoryDraftDirty()) {
      const label = localEmbedderLabel(memoryDraft.embedder);
      if (usesApiEmbedder(memoryDraft.embedder)) {
        const hasKey = hasApiKeyForEmbedder(memoryDraft.embedder);
        const keyLabel = memoryDraft.embedder === 'gemini' ? 'Gemini' : 'OpenAI';
        const article = memoryDraft.embedder === 'openai' ? 'an' : 'a';
        return {
          tone: hasKey ? 'info' : 'warning',
          title: `${keyLabel} memory selected.`,
          detail:
            hasKey
              ? `Save & check to apply ${label} memory.`
              : `Paste ${article} ${keyLabel} workspace API key in Access, then Save & check.`,
          showAddKeyAction: !hasKey,
        };
      }
      return {
        tone: 'info',
        title: `${label} selected.`,
        detail: `Save memory or Save & check to apply ${label} across this workspace. No cloud API key is needed for this embedder.`,
      };
    }
    if (!settings.memory.embedding_detail) return null;
    const needsApiKey = usesApiEmbedder(settings.memory.embedder) && settings.memory.embedding_status === 'missing_key';
    const keyLabel = settings.memory.embedder === 'gemini' ? 'Gemini' : 'OpenAI';
    const article = settings.memory.embedder === 'openai' ? 'an' : 'a';
    return {
      tone: 'warning',
      title: needsApiKey ? `${keyLabel} API key needed.` : 'Memory status',
      detail: needsApiKey
        ? `Paste ${article} ${keyLabel} workspace API key in Access and click Connect key, or choose Local CPU.`
        : settings.memory.embedding_detail,
      showAddKeyAction: needsApiKey,
    };
  }

  function focusAccessKey() {
    if (typeof document === 'undefined') return;
    const fieldId = memoryDraft.embedder === 'gemini'
      ? 'gemini-api-key'
      : showEmbedderKeyPrompt
        ? 'openai-embedder-api-key'
        : 'openai-api-key';
    const field = document.getElementById(fieldId) as HTMLInputElement | null;
    field?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    window.requestAnimationFrame(() => field?.focus());
  }

  function skipEmbedderPrompt() {
    setupEmbedderPromptSkipped = true;
    notice = {
      tone: 'info',
      title: 'Memory key skipped.',
      detail: 'Illo can help connect memory and retrieval from Cortex later.',
    };
  }

  function scrollToSetupSection(section: StartupStepKey) {
    if (typeof document === 'undefined') return;
    const target = document.querySelector(`[data-setup-section="${section}"]`);
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function errorNotice(title: string, error: unknown, fallback: string): NoticeState {
    const detail =
      error instanceof Error
        ? error.message
        : typeof error === 'object' && error !== null && 'detail' in error
          ? String((error as { detail?: unknown }).detail || fallback)
          : fallback;
    return {
      tone: 'danger',
      title,
      detail,
    };
  }
</script>

<svelte:head>
  <title>System</title>
</svelte:head>

<ConstellationPageFrame
  eyebrow="Runtime setup"
  title="System"
  subtitle="Three things to get Illospace running: access, models, and memory."
  contentClassName="system-page"
>
  {#snippet actions()}
    <ConstellationButton variant="quiet" onclick={loadSettings} loading={loading} loadingLabel="Loading">
      {#snippet leadingVisual()}
        <ConstellationIcon name="refresh" size={14} />
      {/snippet}
      Refresh
    </ConstellationButton>
    {#if setupCanContinue}
      <ConstellationButton onclick={continueToCortex} loading={startingIntro} loadingLabel="Opening">
        Continue to Illo
      </ConstellationButton>
    {/if}
  {/snippet}

  {#if loadError}
    <ConstellationNotice title="System failed to load." description={loadError} tone="danger">
      {#snippet actions()}
        <ConstellationButton variant="secondary" size="sm" onclick={loadSettings}>Retry</ConstellationButton>
      {/snippet}
    </ConstellationNotice>
  {/if}

  {#if notice}
    <ConstellationNotice title={notice.title} description={notice.detail || ''} tone={notice.tone} compact />
  {/if}

  {#if loading && !settings}
    <div class="system-loading">Loading runtime setup...</div>
  {:else if settings}
    <div class="setup-cards">
      {#if showStartupGuide}
        <StartupGuide
          steps={startupGuideSteps}
          {canManageSettings}
          onGoToStep={scrollToSetupSection}
        />
      {/if}

      <div class="setup-section" data-setup-section="access">
        <AccessCard
          description={connectionSummary()}
          status={connectionStatus}
          statusTone={connectionTone}
          {apiKey}
          {openaiEmbedderApiKey}
          {geminiApiKey}
          {oauthUrl}
          {oauthCallback}
          oauthPending={Boolean(oauthUrl)}
          {oauthCallbackAvailable}
          {oauthCallbackMode}
          {showEmbedderKeyPrompt}
          {canManageSettings}
          {savingConnection}
          {savingOpenAIEmbedderKey}
          {savingGeminiKey}
          onApiKeyChange={(value) => (apiKey = value)}
          onOpenAIEmbedderApiKeyChange={(value) => (openaiEmbedderApiKey = value)}
          onGeminiApiKeyChange={(value) => (geminiApiKey = value)}
          onCallbackChange={(value) => (oauthCallback = value)}
          onConnectWithApiKey={connectWithApiKey}
          onConnectOpenAIEmbedderKey={connectWithOpenAIEmbeddingKey}
          onConnectWithGeminiKey={connectWithGeminiKey}
          onStartCodexSignIn={() => startCodexSignIn()}
          onStartLocalCodexSignIn={() => startCodexSignIn('local_bridge')}
          onFinishCodexSignIn={finishCodexSignIn}
          onSkipEmbedderPrompt={skipEmbedderPrompt}
        />
      </div>

      <div class="setup-section" data-setup-section="models">
        <ModelsCard
          description={modelSummary()}
          {modelDraft}
          {modelOptions}
          {canManageSettings}
          {savingModels}
          onUpdateModel={updateModelTier}
          onSaveModels={saveModels}
        />
      </div>

      <div class="setup-section" data-setup-section="memory">
        <MemoryCard
          description={memorySummary()}
          status={memoryStatus}
          statusTone={memoryTone}
          memory={settings.memory}
          {memoryDraft}
          embeddingModelOptions={embeddingModelOptions()}
          notice={memoryNotice()}
          {memoryCheck}
          {canManageSettings}
          {savingMemory}
          {checkingMemory}
          onCheckMemory={checkMemory}
          onUpdateMemory={updateMemoryDraft}
          onSaveMemory={saveMemory}
          onAddApiKey={focusAccessKey}
          saveDisabled={memoryChangeNeedsRebuild()}
          checkDisabled={memoryChangeNeedsRebuild()}
        />
      </div>
    </div>
  {/if}
</ConstellationPageFrame>

<style>
  :global(.system-page),
  .setup-cards {
    display: grid;
    gap: 18px;
  }

  .system-loading {
    display: grid;
    min-height: 260px;
    place-items: center;
    border: 1px solid var(--constellation-surface-panel-border);
    border-radius: var(--constellation-radius-panel);
    color: var(--constellation-text-muted);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .setup-section {
    display: grid;
    min-width: 0;
    scroll-margin-top: 18px;
  }
</style>
