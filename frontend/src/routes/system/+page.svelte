<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { getContext, onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationNotice,
    ConstellationPageFrame,
  } from '$lib/components/constellation';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalContext,
  } from '$lib/components/constellation/constellationPageFrameContext';
  import { cortex } from '$lib/stores/cortex.svelte';
  import {
    closeOAuthPopup,
    navigateOpenAIOAuthPopup,
    openOpenAIOAuthPopup,
  } from '$lib/utils/oauthPopup';
  import { hasPersonalOpenAIRuntimeConnection } from '$lib/utils/runtimeOnboarding';
  import { auth } from '$lib/stores/auth.svelte';

  import MemoryCard from './MemoryCard.svelte';
  import ModelsCard from './ModelsCard.svelte';
  import ProviderConnections from './ProviderConnections.svelte';
  import VoiceCard from './VoiceCard.svelte';
  import type {
    EmbedderKey,
    MemoryCheck,
    MemoryDraft,
    MemoryNoticeState,
    NoticeState,
    RuntimeOption,
    RuntimeSettings,
    VoiceDraft,
  } from './types';

  type CodexSignInCallbackMode = 'auto' | 'server' | 'local_bridge';
  type MemoryVaultProvider = 'openai' | 'gemini';
  type VaultSecret = {
    id: number;
    key_name: string;
    description?: string | null;
    category?: string | null;
  };
  type StoredVaultSession = {
    token: string;
    expiresAt: string;
    savedAt: string;
  };

  const VAULT_SESSION_STORAGE_PREFIX = 'illo:vault:unlock:v1';
  const VAULT_SESSION_EXPIRY_SKEW_MS = 5000;

  let settings = $state<RuntimeSettings | null>(null);
  let loading = $state(true);
  let loadError = $state('');
  let oauthCallback = $state('');
  let oauthUrl = $state('');
  let oauthState = $state('');
  let oauthCallbackAvailable = $state(true);
  let oauthCallbackMode = $state<'server' | 'local_bridge' | 'manual'>('local_bridge');
  let oauthChannel: BroadcastChannel | null = null;
  let oauthExchangeInFlight = false;
  let savingConnection = $state(false);
  let personalOpenAIKey = $state('');
  let orgOpenAIKey = $state('');
  let memoryApiKey = $state('');
  let savingPersonalApiKey = $state(false);
  let savingOrgApiKey = $state(false);
  let savingMemoryApiKey = $state(false);
  let savingChanges = $state(false);
  let checkingMemory = $state(false);
  let memoryCheck = $state<MemoryCheck | null>(null);
  let notice = $state<NoticeState | null>(null);
  let startingIntro = $state(false);
  let modelDraft = $state<{ default: string; thinking: string }>({ default: '', thinking: 'high' });
  let memoryDraft = $state<MemoryDraft>({
    embedder: 'local_gpu',
    embedding_model: 'text-embedding-3-small',
    reranker: 'weighted',
  });
  let voiceDraft = $state<VoiceDraft>({
    provider: 'openai',
    language: 'auto',
    model_size: 'base',
  });
  let vaultSecrets = $state<VaultSecret[]>([]);
  let vaultLoading = $state(false);
  let vaultLoadError = $state('');
  let vaultToken = $state<string | null>(null);
  let selectedMemoryVaultKey = $state('');
  let syncingMemoryVaultKey = $state(false);
  let lastVaultLoadUserId = '';

  const workspacePageModalContext = getContext<ConstellationPageFrameModalContext | undefined>(
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
  );

  $effect(() => {
    return workspacePageModalContext?.registerRefreshAction({
      label: loading ? 'Loading settings' : 'Refresh settings',
      disabled: loading,
      onclick: loadSettings,
    });
  });

  const canManageSettings = $derived(settings?.permissions?.can_manage_settings ?? false);
  const hasPersonalOpenAIConnection = $derived(hasPersonalOpenAIRuntimeConnection(settings));
  const hasOrgOpenAIConnection = $derived(Boolean(settings?.connection?.has_org_key));
  const connectionStatus = $derived(settings?.connection?.status ?? 'missing');
  const modelOptions = $derived(
    (settings?.models?.catalog ?? []).map((entry) => ({
      key: entry.id,
      label: entry.label,
      description: entry.description,
      group: entry.provider,
    })),
  );
  const selectedModelCatalogEntry = $derived(
    settings?.models?.catalog.find((entry) => entry.id === modelDraft.default),
  );
  const thinkingOptions = $derived(
    (settings?.models?.thinking_options ?? []).filter(
      (option) =>
        !selectedModelCatalogEntry ||
        selectedModelCatalogEntry.supported_effort_tiers.includes(
          option.key as RuntimeSettings['models']['thinking'],
        ),
    ),
  );
  const setupMode = $derived($page.url.searchParams.get('setup') === '1');
  const setupCanContinue = $derived(setupMode && connectionStatus === 'connected');
  const memoryVaultProvider = $derived<MemoryVaultProvider>(embeddingProvider(memoryDraft.embedder));
  const memoryVaultKeyOptions = $derived(buildMemoryVaultKeyOptions());
  const hasRuntimeChanges = $derived(Boolean(settings) && (modelDraftDirty() || memoryDraftDirty() || voiceDraftDirty()));
  const runCheckDisabled = $derived(!canManageSettings || memoryChangeNeedsRebuild());
  const vaultSessionStorageKey = $derived(
    auth.user?.org_id && auth.user?.id
      ? `${VAULT_SESSION_STORAGE_PREFIX}:${String(auth.user.org_id)}:${String(auth.user.id)}`
      : '',
  );

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

  $effect(() => {
    const vaultScope = auth.user?.org_id && auth.user?.id
      ? `${String(auth.user.org_id)}:${String(auth.user.id)}`
      : '';
    if (!vaultScope || vaultScope === lastVaultLoadUserId) return;
    lastVaultLoadUserId = vaultScope;
    void loadVaultSecrets();
  });

  function hydrate(next: RuntimeSettings) {
    settings = next;
    modelDraft = {
      default: next.models.default,
      thinking: next.models.thinking || 'high',
    };
    memoryDraft = {
      embedder: next.memory.embedder,
      embedding_model: next.memory.embedding_model || defaultEmbeddingModel(next.memory.embedder, next),
      reranker: next.memory.reranker || 'weighted',
    };
    voiceDraft = {
      provider: next.voice.provider,
      language: next.voice.language || 'auto',
      model_size: next.voice.model_size || 'base',
    };
  }

  async function loadSettings() {
    loading = true;
    loadError = '';
    try {
      const next = await api.runtimeSettings();
      hydrate(next);
      void loadVaultSecrets();
    } catch (error) {
      loadError = error instanceof Error ? error.message : 'System setup failed to load.';
    } finally {
      loading = false;
    }
  }

  function normalizeCodexSignInCallbackMode(value: unknown): CodexSignInCallbackMode {
    return value === 'server' || value === 'local_bridge' ? value : 'auto';
  }

  async function startCodexSignIn(callbackMode: unknown = 'auto') {
    const requestedCallbackMode = normalizeCodexSignInCallbackMode(callbackMode);
    const popup = openOpenAIOAuthPopup();
    savingConnection = true;
    notice = null;
    try {
      const result = await api.startRuntimeOpenAIOAuth({ callback_mode: requestedCallbackMode });
      oauthUrl = result.url || '';
      oauthState = result.state || '';
      oauthCallbackAvailable = result.callback_available ?? true;
      oauthCallbackMode = result.callback_mode || 'local_bridge';
      let openedOAuthWindow = false;
      if (oauthUrl) {
        openedOAuthWindow = navigateOpenAIOAuthPopup(popup, oauthUrl);
        if (!openedOAuthWindow) {
          closeOAuthPopup(popup);
        }
      } else {
        closeOAuthPopup(popup);
      }
      notice = oauthCallbackAvailable
        ? {
            tone: 'info',
            title: openedOAuthWindow ? 'Codex sign-in opened.' : 'Codex sign-in ready.',
            detail: codexSignInReadyDetail(openedOAuthWindow),
          }
        : {
            tone: 'warning',
            title: openedOAuthWindow ? 'Codex sign-in opened.' : 'Codex sign-in ready.',
            detail: result.callback_detail || 'The automatic callback bridge is unavailable. Use the manual callback fallback if the window cannot return here.',
          };
    } catch (error) {
      closeOAuthPopup(popup);
      notice = errorNotice('Could not start Codex sign-in.', error, 'Try again from the System tab.');
    } finally {
      savingConnection = false;
    }
  }

  function codexSignInReadyDetail(openedOAuthWindow: boolean) {
    if (oauthCallbackMode === 'server') {
      return 'Finish in the OpenAI window. It should return to this Illo server automatically; paste the callback URL below if it does not.';
    }
    if (openedOAuthWindow) {
      return 'Finish in the OpenAI window. This page should update automatically; paste the callback URL below if it does not.';
    }
    return 'Open OpenAI sign-in below. This page should update automatically; paste the callback URL below if it does not.';
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

  async function savePersonalOpenAIKey() {
    const value = personalOpenAIKey.trim();
    if (!value || !settings) return;
    savingPersonalApiKey = true;
    notice = null;
    try {
      const connection = await api.connectRuntimeOpenAIKey({ api_key: value });
      settings = { ...settings, connection };
      personalOpenAIKey = '';
      notice = { tone: 'success', title: 'Personal OpenAI key saved.' };
    } catch (error) {
      notice = errorNotice('Personal OpenAI key was not saved.', error, 'Check the key and try again.');
    } finally {
      savingPersonalApiKey = false;
    }
  }

  async function saveOrgOpenAIKey() {
    const value = orgOpenAIKey.trim();
    if (!value || !settings || !canManageSettings) return;
    savingOrgApiKey = true;
    notice = null;
    try {
      const connection = await api.connectRuntimeOpenAIOrgKey({ api_key: value });
      settings = { ...settings, connection };
      orgOpenAIKey = '';
      notice = { tone: 'success', title: 'Workspace OpenAI key rotated.' };
    } catch (error) {
      notice = errorNotice('Workspace OpenAI key was not rotated.', error, 'Check the key and try again.');
    } finally {
      savingOrgApiKey = false;
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
      captureManualCodexCallback(data.callback);
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

  function captureManualCodexCallback(callback: string) {
    oauthCallback = callback.trim();
    savingConnection = false;
    notice = {
      tone: 'info',
      title: 'Callback URL captured.',
      detail: 'Press Finish to complete Codex sign-in.',
    };
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

  async function saveRuntimeChanges() {
    if (!canManageSettings || !settings) return;
    const shouldSaveModels = modelDraftDirty();
    const shouldSaveMemory = memoryDraftDirty();
    const shouldSaveVoice = voiceDraftDirty();
    if (!shouldSaveModels && !shouldSaveMemory && !shouldSaveVoice) {
      notice = { tone: 'info', title: 'Runtime settings are current.' };
      return;
    }

    savingChanges = true;
    notice = null;
    try {
      let nextSettings = settings;
      const saved: string[] = [];
      if (shouldSaveModels) {
        const models = await api.updateRuntimeModels(modelDraft);
        nextSettings = { ...nextSettings, models };
        cortex.applyWorkspaceRunDefaults(
          models.default,
          models.thinking,
          models.catalog,
        );
        saved.push('Model');
      }
      if (shouldSaveMemory) {
        const memory = await api.updateRuntimeMemory(memoryPayload());
        nextSettings = { ...nextSettings, memory };
        memoryCheck = null;
        saved.push('Memory');
      }
      if (shouldSaveVoice) {
        const voice = await api.updateRuntimeVoice(voicePayload());
        nextSettings = { ...nextSettings, voice };
        saved.push('Voice');
      }
      settings = nextSettings;
      notice = {
        tone: 'success',
        title: saved.length > 1 ? 'Runtime settings saved.' : `${saved[0]} saved.`,
      };
    } catch (error) {
      notice = errorNotice(
        'Runtime settings were not saved.',
        error,
        'Check the selected provider, models, and memory settings.',
      );
    } finally {
      savingChanges = false;
    }
  }

  function updateModel(value: string) {
    const entry = settings?.models?.catalog.find((candidate) => candidate.id === value);
    const thinking = entry?.supported_effort_tiers.includes(
      modelDraft.thinking as RuntimeSettings['models']['thinking'],
    )
      ? modelDraft.thinking
      : entry?.supported_effort_tiers[0] || modelDraft.thinking;
    modelDraft = { default: value, thinking };
  }

  function updateThinking(value: string) {
    modelDraft = { ...modelDraft, thinking: value };
  }

  function updateMemoryDraft(key: keyof MemoryDraft, value: string) {
    if (key === 'embedder') {
      const embedder = value as EmbedderKey;
      memoryDraft = {
        ...memoryDraft,
        embedder,
        embedding_model: defaultEmbeddingModel(embedder),
      };
      reconcileSelectedMemoryVaultKey(embedder);
    } else {
      memoryDraft = { ...memoryDraft, [key]: value } as MemoryDraft;
    }
    memoryCheck = null;
  }

  function updateVoiceDraft(key: keyof VoiceDraft, value: string) {
    voiceDraft = { ...voiceDraft, [key]: value } as VoiceDraft;
  }

  function getVaultSessionStorage(): Storage | null {
    if (typeof sessionStorage === 'undefined') return null;
    try {
      return sessionStorage;
    } catch {
      return null;
    }
  }

  function clearPersistedVaultSession() {
    const storage = getVaultSessionStorage();
    if (!storage || !vaultSessionStorageKey) return;
    try {
      storage.removeItem(vaultSessionStorageKey);
    } catch {
      // Browser storage can be blocked; the page still works without persistence.
    }
  }

  function readPersistedVaultSession(): string | null {
    const storage = getVaultSessionStorage();
    if (!storage || !vaultSessionStorageKey) return null;
    try {
      const raw = storage.getItem(vaultSessionStorageKey);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Partial<StoredVaultSession>;
      if (!parsed.token || !parsed.expiresAt) {
        clearPersistedVaultSession();
        return null;
      }
      const expiresAt = Date.parse(parsed.expiresAt);
      if (!Number.isFinite(expiresAt) || expiresAt <= Date.now() + VAULT_SESSION_EXPIRY_SKEW_MS) {
        clearPersistedVaultSession();
        return null;
      }
      return parsed.token;
    } catch {
      clearPersistedVaultSession();
      return null;
    }
  }

  async function loadVaultSecrets() {
    if (!auth.user?.id) {
      vaultSecrets = [];
      vaultToken = null;
      vaultLoadError = '';
      selectedMemoryVaultKey = '';
      return;
    }

    vaultToken = readPersistedVaultSession();
    vaultLoadError = '';
    vaultLoading = true;
    try {
      const next = (await api.listSecrets()) as VaultSecret[];
      vaultSecrets = next;
      reconcileSelectedMemoryVaultKey(memoryDraft.embedder, next);
    } catch (error: any) {
      vaultSecrets = [];
      selectedMemoryVaultKey = '';
      vaultLoadError = error?.detail || error?.message || 'Vault keys could not be loaded.';
    } finally {
      vaultLoading = false;
    }
  }

  function memoryPayload() {
    return {
      embedder: memoryDraft.embedder,
      embedding_model: usesApiEmbedder(memoryDraft.embedder) ? memoryDraft.embedding_model : null,
      reranker: memoryDraft.reranker,
    };
  }

  function voicePayload() {
    return {
      provider: voiceDraft.provider,
      language: voiceDraft.language,
      model_size: voiceDraft.model_size,
    };
  }

  function modelDraftDirty() {
    if (!settings) return false;
    return (
      modelDraft.default !== settings.models.default ||
      modelDraft.thinking !== settings.models.thinking
    );
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

  function voiceDraftDirty() {
    if (!settings) return false;
    return (
      voiceDraft.provider !== settings.voice.provider ||
      voiceDraft.language !== (settings.voice.language || 'auto') ||
      voiceDraft.model_size !== (settings.voice.model_size || 'base')
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

  function localEmbedderLabel(embedder: EmbedderKey) {
    if (embedder === 'local_cpu') return 'Local CPU';
    if (embedder === 'local_gpu') return 'Local GPU';
    if (embedder === 'gemini') return 'Gemini';
    return 'OpenAI';
  }

  function usesApiEmbedder(embedder: EmbedderKey) {
    return embedder === 'openai' || embedder === 'gemini';
  }

  function embeddingProvider(embedder: EmbedderKey): MemoryVaultProvider {
    return embedder === 'gemini' ? 'gemini' : 'openai';
  }

  function embeddingModelOptions(embedder = memoryDraft.embedder, source = settings) {
    const provider = embeddingProvider(embedder);
    return (source?.memory.embedding_model_options ?? []).filter((option) => option.group === provider);
  }

  function defaultEmbeddingModel(embedder: EmbedderKey, source = settings) {
    return (
      embeddingModelOptions(embedder, source)[0]?.key ||
      (embedder === 'gemini' ? 'gemini-embedding-2' : 'text-embedding-3-small')
    );
  }

  function hasApiKeyForEmbedder(embedder: EmbedderKey) {
    return Boolean(settings?.memory.api_key_statuses?.[embeddingProvider(embedder)]);
  }

  function providerLabel(provider: MemoryVaultProvider) {
    return provider === 'gemini' ? 'Gemini' : 'OpenAI';
  }

  function buildMemoryVaultKeyOptions(): RuntimeOption[] {
    if (!usesApiEmbedder(memoryDraft.embedder)) {
      return [{ key: '', label: 'No key needed', description: localEmbedderLabel(memoryDraft.embedder), disabled: true }];
    }

    const provider = memoryVaultProvider;
    const label = providerLabel(provider);

    if (vaultLoading) {
      return [{ key: '', label: 'Checking Vault', description: 'Reading saved keys.', disabled: true }];
    }
    if (vaultLoadError) {
      return [{ key: '', label: 'Unable to load keys', description: vaultLoadError, disabled: true }];
    }

    const matches = matchingVaultSecrets(provider);
    if (matches.length === 0) {
      return [{ key: '', label: 'No API key in Vault', disabled: true }];
    }

    const placeholder = {
      key: '',
      label: 'Choose API key',
      description: `${label} keys in Vault.`,
      disabled: true,
    };

    return [
      ...(selectedMemoryVaultKey ? [] : [placeholder]),
      ...matches.map((secret) => ({
        key: secret.key_name,
        label: secret.key_name,
        description: secret.description || secret.category || 'Vault secret',
      })),
    ];
  }

  function matchingVaultSecrets(provider: MemoryVaultProvider, source = vaultSecrets) {
    const apiSecrets = source.filter(isApiVaultSecret);
    const providerSecrets = apiSecrets.filter((secret) => isProviderVaultSecret(secret, provider));
    return providerSecrets.length > 0 ? providerSecrets : apiSecrets;
  }

  function isApiVaultSecret(secret: VaultSecret) {
    const haystack = vaultSecretSearchText(secret);
    return haystack.includes('api') || haystack.includes('key') || haystack.includes('token');
  }

  function isProviderVaultSecret(secret: VaultSecret, provider: MemoryVaultProvider) {
    const keyName = normalizedVaultSecretKey(secret);
    const haystack = vaultSecretSearchText(secret);
    if (provider === 'gemini') {
      return keyName.includes('GEMINI') || haystack.includes('gemini') || haystack.includes('google');
    }
    return keyName.includes('OPENAI') || haystack.includes('openai');
  }

  function vaultSecretSearchText(secret: VaultSecret) {
    return [secret.key_name, secret.description, secret.category]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
  }

  function normalizedVaultSecretKey(secret: VaultSecret) {
    return secret.key_name.toUpperCase();
  }

  function reconcileSelectedMemoryVaultKey(embedder = memoryDraft.embedder, source = vaultSecrets) {
    if (!usesApiEmbedder(embedder)) {
      selectedMemoryVaultKey = '';
      return;
    }
    const provider = embeddingProvider(embedder);
    const matches = matchingVaultSecrets(provider, source);
    if (selectedMemoryVaultKey && matches.some((secret) => secret.key_name === selectedMemoryVaultKey)) return;
    selectedMemoryVaultKey = hasApiKeyForEmbedder(embedder) ? preferredVaultSecretKey(provider, matches) : '';
  }

  function preferredVaultSecretKey(provider: MemoryVaultProvider, matches: VaultSecret[]) {
    const preferredNames = provider === 'gemini'
      ? ['GEMINI_API_KEY']
      : ['OPENAI_EMBEDDING_API_KEY', 'OPENAI_API_KEY'];
    for (const name of preferredNames) {
      const exactMatch = matches.find((secret) => normalizedVaultSecretKey(secret) === name);
      if (exactMatch) return exactMatch.key_name;
    }
    if (provider === 'gemini') {
      return matches[0]?.key_name || '';
    }
    return (
      matches.find((secret) => normalizedVaultSecretKey(secret).includes('EMBEDDING'))?.key_name ||
      matches[0]?.key_name ||
      ''
    );
  }

  async function handleMemoryVaultKeyChange(keyName: string) {
    if (!keyName || !canManageSettings) return;
    await selectMemoryVaultKey(keyName);
  }

  async function selectMemoryVaultKey(keyName: string) {
    const provider = memoryVaultProvider;
    const token = vaultToken || readPersistedVaultSession();

    syncingMemoryVaultKey = true;
    notice = null;
    try {
      const data = await api.revealSecret(keyName, token);
      const value = String(data?.value || '');
      if (!value) throw new Error('Vault secret is empty.');
      if (provider === 'gemini') {
        await api.connectRuntimeGeminiKey({ api_key: value });
      } else {
        await api.connectRuntimeOpenAIEmbeddingKey({ api_key: value });
      }
      selectedMemoryVaultKey = keyName;
      if (settings) {
        settings = {
          ...settings,
          memory: {
            ...settings.memory,
            api_key_statuses: {
              ...(settings.memory.api_key_statuses ?? {}),
              [provider]: true,
            },
          },
        };
      }
      memoryCheck = null;
      notice = {
        tone: 'success',
        title: 'Memory key selected.',
        detail: `${providerLabel(provider)} is available for memory.`,
      };
    } catch (error: any) {
      if (error?.status === 423) {
        vaultToken = null;
        selectedMemoryVaultKey = '';
        clearPersistedVaultSession();
      }
      notice = errorNotice('Memory key was not selected.', error, 'Unlock Vault before using this key.');
    } finally {
      syncingMemoryVaultKey = false;
    }
  }

  async function saveMemoryApiKey() {
    const value = memoryApiKey.trim();
    if (!value || !settings || !canManageSettings || !usesApiEmbedder(memoryDraft.embedder)) return;
    const provider = memoryVaultProvider;
    savingMemoryApiKey = true;
    notice = null;
    try {
      const memory = provider === 'gemini'
        ? await api.connectRuntimeGeminiKey({ api_key: value })
        : await api.connectRuntimeOpenAIEmbeddingKey({ api_key: value });
      settings = { ...settings, memory };
      memoryApiKey = '';
      selectedMemoryVaultKey = '';
      memoryCheck = null;
      notice = {
        tone: 'success',
        title: 'Memory key rotated.',
        detail: `${providerLabel(provider)} is available for memory.`,
      };
    } catch (error) {
      notice = errorNotice('Memory key was not rotated.', error, 'Check the key and try again.');
    } finally {
      savingMemoryApiKey = false;
    }
  }

  function memoryNotice(): MemoryNoticeState | null {
    if (!settings) return null;
    if (memoryChangeNeedsRebuild()) {
      return {
        tone: 'warning',
        title: 'Memory rebuild required.',
        detail: `${settings.memory.indexed_vectors} vectors use ${localEmbedderLabel(settings.memory.embedder)}. Rebuild before changing embedder or model.`,
      };
    }
    if (memoryDraftDirty()) {
      const label = localEmbedderLabel(memoryDraft.embedder);
      if (usesApiEmbedder(memoryDraft.embedder)) {
        const hasKey = hasApiKeyForEmbedder(memoryDraft.embedder);
        return {
          tone: hasKey ? 'info' : 'warning',
          title: hasKey ? 'Memory has unsaved changes.' : 'Memory is not set up.',
          detail: hasKey ? 'Save changes to apply.' : undefined,
        };
      }
      return {
        tone: 'info',
        title: 'Memory has unsaved changes.',
        detail: `${label} selected. Save changes to apply.`,
      };
    }
    if (!settings.memory.embedding_detail) return null;
    const needsApiKey = usesApiEmbedder(settings.memory.embedder) && settings.memory.embedding_status === 'missing_key';
    return {
      tone: 'warning',
      title: needsApiKey ? 'Memory is not set up.' : 'Memory status',
      detail: needsApiKey ? undefined : settings.memory.embedding_detail,
    };
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
  eyebrow="System"
  title="AI Runtime"
  subtitle="Configure providers, model routing, and memory."
  contentClassName="system-page"
>
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
    <div class="system-loading">Loading runtime...</div>
  {:else if settings}
    <div class="runtime-config-layout">
      <div class="runtime-primary-column">
        <ProviderConnections
          {oauthUrl}
          {oauthCallback}
          oauthPending={Boolean(oauthUrl)}
          {oauthCallbackMode}
          hasPersonalConnection={hasPersonalOpenAIConnection}
          hasOrgConnection={hasOrgOpenAIConnection}
          {canManageSettings}
          personalApiKey={personalOpenAIKey}
          orgApiKey={orgOpenAIKey}
          {savingConnection}
          {savingPersonalApiKey}
          {savingOrgApiKey}
          onCallbackChange={(value) => (oauthCallback = value)}
          onPersonalApiKeyChange={(value) => (personalOpenAIKey = value)}
          onOrgApiKeyChange={(value) => (orgOpenAIKey = value)}
          onStartCodexSignIn={() => startCodexSignIn()}
          onStartLocalCodexSignIn={() => startCodexSignIn('local_bridge')}
          onFinishCodexSignIn={finishCodexSignIn}
          onSavePersonalApiKey={savePersonalOpenAIKey}
          onSaveOrgApiKey={saveOrgOpenAIKey}
        />

        <ModelsCard
          {modelDraft}
          {modelOptions}
          {thinkingOptions}
          {canManageSettings}
          onUpdateModel={updateModel}
          onUpdateThinking={updateThinking}
        />
      </div>

      <div class="runtime-secondary-column">
        <MemoryCard
          memory={settings.memory}
          {memoryDraft}
          embeddingModelOptions={embeddingModelOptions()}
          vaultKeyOptions={memoryVaultKeyOptions}
          selectedVaultKey={selectedMemoryVaultKey}
          {memoryApiKey}
          notice={memoryNotice()}
          {memoryCheck}
          {canManageSettings}
          {vaultLoading}
          syncingVaultKey={syncingMemoryVaultKey}
          {savingMemoryApiKey}
          onUpdateMemory={updateMemoryDraft}
          onSelectVaultKey={handleMemoryVaultKeyChange}
          onMemoryApiKeyChange={(value) => (memoryApiKey = value)}
          onSaveMemoryApiKey={saveMemoryApiKey}
        />

        <VoiceCard
          voice={settings.voice}
          {voiceDraft}
          {canManageSettings}
          onUpdateVoice={updateVoiceDraft}
        />
      </div>
    </div>

    <footer class="runtime-footer">
      <p>Unsaved changes apply to future runs.</p>
      <div class="runtime-footer-actions">
        {#if setupCanContinue}
          <ConstellationButton
            variant="quiet"
            onclick={continueToCortex}
            loading={startingIntro}
            loadingLabel="Opening"
          >
            Continue to Illo
          </ConstellationButton>
        {/if}
        <ConstellationButton
          variant="secondary"
          onclick={checkMemory}
          loading={checkingMemory}
          disabled={runCheckDisabled}
        >
          Run check
        </ConstellationButton>
        <ConstellationButton
          onclick={saveRuntimeChanges}
          loading={savingChanges}
          loadingLabel="Saving"
          disabled={!canManageSettings || !hasRuntimeChanges}
        >
          Save changes
        </ConstellationButton>
      </div>
    </footer>
  {/if}
</ConstellationPageFrame>

<style>
  :global(.system-page) {
    display: grid;
    gap: 16px;
    min-height: 100%;
  }

  .runtime-config-layout {
    display: grid;
    grid-template-columns: minmax(480px, 1.04fr) minmax(420px, 0.86fr);
    gap: clamp(26px, 3.4vw, 44px);
    align-items: start;
    width: 100%;
    max-width: 1540px;
    min-width: 0;
    box-sizing: border-box;
    margin: 0 auto;
    padding: 0 clamp(12px, 1.4vw, 22px);
  }

  .runtime-primary-column,
  .runtime-secondary-column {
    display: grid;
    min-width: 0;
  }

  .runtime-primary-column {
    gap: 0;
  }

  .runtime-secondary-column {
    position: sticky;
    top: 0;
    align-self: start;
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

  .runtime-footer {
    position: sticky;
    bottom: 0;
    z-index: 2;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
    width: calc(100% - clamp(24px, 2.8vw, 44px));
    max-width: 1540px;
    margin: 6px auto 0;
    box-sizing: border-box;
    padding: 14px 0 2px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
    background:
      linear-gradient(
        to top,
        var(--constellation-surface-page-background, var(--constellation-surface-panel-background)) 68%,
        color-mix(in srgb, var(--constellation-surface-page-background, var(--constellation-surface-panel-background)) 0%, transparent)
      );
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .runtime-footer p {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .runtime-footer-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
    min-width: 0;
    flex-wrap: wrap;
  }

  @media (max-width: 1080px) {
    .runtime-config-layout {
      grid-template-columns: 1fr;
    }

    .runtime-secondary-column {
      position: static;
    }
  }

  @media (max-width: 700px) {
    .runtime-footer {
      align-items: stretch;
      flex-direction: column;
    }

    .runtime-footer-actions {
      justify-content: flex-start;
    }
  }
</style>
