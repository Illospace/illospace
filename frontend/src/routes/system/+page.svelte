<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { getContext, onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationIcon,
    ConstellationNotice,
    ConstellationPageFrame,
  } from '$lib/components/constellation';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalContext,
  } from '$lib/components/constellation/constellationPageFrameContext';
  import {
    closeOAuthPopup,
    navigateOpenAIOAuthPopup,
    openOpenAIOAuthPopup,
  } from '$lib/utils/oauthPopup';
  import { auth } from '$lib/stores/auth.svelte';

  import MemoryCard from './MemoryCard.svelte';
  import ModelsCard from './ModelsCard.svelte';
  import type {
    EmbedderKey,
    MemoryCheck,
    MemoryDraft,
    MemoryNoticeState,
    ModelTier,
    NoticeState,
    RuntimeOption,
    RuntimeSettings,
    RuntimeUpdateStatus,
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

  const OPEN_VAULT_SELECT_VALUE = '__open_vault__';
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
  let savingModels = $state(false);
  let checkingMemory = $state(false);
  let memoryCheck = $state<MemoryCheck | null>(null);
  let notice = $state<NoticeState | null>(null);
  let updateStatus = $state<RuntimeUpdateStatus | null>(null);
  let startingUpdate = $state(false);
  let startingIntro = $state(false);
  let modelDraft = $state<Record<ModelTier, string>>({ low: '', medium: '', high: '' });
  let memoryDraft = $state<MemoryDraft>({
    embedder: 'local_gpu',
    embedding_model: 'text-embedding-3-small',
    reranker: 'weighted',
  });
  let vaultSecrets = $state<VaultSecret[]>([]);
  let vaultLoading = $state(false);
  let vaultLocked = $state(true);
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
  const connectionStatus = $derived(settings?.connection?.status ?? 'missing');
  const modelOptions = $derived(settings?.models?.options ?? []);
  const setupMode = $derived($page.url.searchParams.get('setup') === '1');
  const setupCanContinue = $derived(setupMode && connectionStatus === 'connected');
  const updateRunning = $derived(startingUpdate || updateStatus?.status === 'running');
  const memoryVaultProvider = $derived<MemoryVaultProvider>(embeddingProvider(memoryDraft.embedder));
  const memoryVaultKeyOptions = $derived(buildMemoryVaultKeyOptions());
  const vaultSessionStorageKey = $derived(
    auth.user?.id ? `${VAULT_SESSION_STORAGE_PREFIX}:${String(auth.user.id)}` : '',
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
    const userId = auth.user?.id ?? '';
    if (!userId || userId === lastVaultLoadUserId) return;
    lastVaultLoadUserId = userId;
    void loadVaultSecrets();
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
      const next = await api.runtimeSettings();
      hydrate(next);
      if (next.permissions?.can_manage_settings) {
        void loadUpdateStatus();
      }
      void loadVaultSecrets();
    } catch (error) {
      loadError = error instanceof Error ? error.message : 'System setup failed to load.';
    } finally {
      loading = false;
    }
  }

  async function loadUpdateStatus() {
    if (!settings?.permissions?.can_manage_settings) return;
    try {
      updateStatus = await api.runtimeUpdateStatus();
    } catch {
      updateStatus = null;
    }
  }

  async function startIllospaceUpdate() {
    if (!canManageSettings || updateStatus?.available === false) return;
    startingUpdate = true;
    notice = null;
    try {
      const nextStatus = (await api.startRuntimeUpdate()) as RuntimeUpdateStatus;
      updateStatus = nextStatus;
      notice = {
        tone: 'info',
        title: 'Illospace update started.',
        detail: nextStatus.detail || updateNoticeDetail(nextStatus),
      };
    } catch (error) {
      notice = errorNotice('Update did not start.', error, 'Check the server update log and try again.');
    } finally {
      startingUpdate = false;
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
      closeOAuthPopup(popup);
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
      reconcileSelectedMemoryVaultKey(embedder);
    } else {
      memoryDraft = { ...memoryDraft, [key]: value } as MemoryDraft;
    }
    memoryCheck = null;
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
      vaultLocked = true;
      vaultLoadError = '';
      selectedMemoryVaultKey = '';
      return;
    }

    const token = readPersistedVaultSession();
    vaultToken = token;
    vaultLoadError = '';
    if (!token) {
      vaultSecrets = [];
      vaultLocked = true;
      selectedMemoryVaultKey = '';
      return;
    }

    vaultLoading = true;
    try {
      const next = (await api.listSecrets(undefined, token)) as VaultSecret[];
      vaultSecrets = next;
      vaultLocked = false;
      reconcileSelectedMemoryVaultKey(memoryDraft.embedder, next);
    } catch (error: any) {
      vaultSecrets = [];
      selectedMemoryVaultKey = '';
      if (error?.status === 423) {
        vaultToken = null;
        vaultLocked = true;
        clearPersistedVaultSession();
      } else {
        vaultLocked = false;
        vaultLoadError = error?.detail || error?.message || 'Vault keys could not be loaded.';
      }
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

  function modelDraftDirty() {
    if (!settings) return false;
    return (
      modelDraft.low !== settings.models.low ||
      modelDraft.medium !== settings.models.medium ||
      modelDraft.high !== settings.models.high
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
    return embeddingModelOptions(embedder, source)[0]?.key || (embedder === 'gemini' ? 'gemini-embedding-2' : 'text-embedding-3-small');
  }

  function hasApiKeyForEmbedder(embedder: EmbedderKey) {
    return Boolean(settings?.memory.api_key_statuses?.[embeddingProvider(embedder)]);
  }

  function providerLabel(provider: MemoryVaultProvider) {
    return provider === 'gemini' ? 'Gemini' : 'OpenAI';
  }

  function providerArticle(provider: MemoryVaultProvider) {
    return provider === 'openai' ? 'an' : 'a';
  }

  function buildMemoryVaultKeyOptions(): RuntimeOption[] {
    if (!usesApiEmbedder(memoryDraft.embedder)) {
      return [{ key: '', label: 'No key needed', description: localEmbedderLabel(memoryDraft.embedder), disabled: true }];
    }

    const provider = memoryVaultProvider;
    const label = providerLabel(provider);
    const openVault = vaultOpenOption(provider);

    if (vaultLoading) {
      return [{ key: '', label: 'Checking Vault', description: 'Reading saved keys.', disabled: true }];
    }
    if (vaultLocked) {
      return [
        { key: '', label: 'Vault locked', description: `Unlock Vault to choose ${providerArticle(provider)} ${label} key.`, disabled: true },
        openVault,
      ];
    }
    if (vaultLoadError) {
      return [
        { key: '', label: 'Vault unavailable', description: vaultLoadError, disabled: true },
        openVault,
      ];
    }

    const matches = matchingVaultSecrets(provider);
    const placeholder = hasApiKeyForEmbedder(memoryDraft.embedder) && matches.length === 0
      ? { key: '', label: 'Runtime key saved', description: 'Add a Vault key to manage it here.', disabled: true }
      : {
          key: '',
          label: matches.length > 0 ? 'Choose Vault key' : 'No matching key',
          description: matches.length > 0 ? `${label} keys in Vault.` : `Add ${providerArticle(provider)} ${label} key in Vault.`,
          disabled: true,
        };

    return [
      ...(selectedMemoryVaultKey ? [] : [placeholder]),
      ...matches.map((secret) => ({
        key: secret.key_name,
        label: secret.key_name,
        description: secret.description || secret.category || 'Vault secret',
      })),
      openVault,
    ];
  }

  function vaultOpenOption(provider: MemoryVaultProvider): RuntimeOption {
    return {
      key: OPEN_VAULT_SELECT_VALUE,
      label: 'Open Vault',
      description: `Add or unlock ${providerArticle(provider)} ${providerLabel(provider)} key.`,
    };
  }

  function matchingVaultSecrets(provider: MemoryVaultProvider, source = vaultSecrets) {
    return source.filter((secret) => isProviderVaultSecret(secret, provider));
  }

  function isProviderVaultSecret(secret: VaultSecret, provider: MemoryVaultProvider) {
    const keyName = secret.key_name.toUpperCase();
    const haystack = [secret.key_name, secret.description, secret.category]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
    if (provider === 'gemini') {
      return keyName.includes('GEMINI') || haystack.includes('gemini') || haystack.includes('google');
    }
    return keyName.includes('OPENAI') || haystack.includes('openai');
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
    if (provider === 'gemini') {
      return matches.find((secret) => secret.key_name.toUpperCase() === 'GEMINI_API_KEY')?.key_name
        || matches[0]?.key_name
        || '';
    }
    return matches.find((secret) => secret.key_name.toUpperCase() === 'OPENAI_EMBEDDING_API_KEY')?.key_name
      || matches.find((secret) => secret.key_name.toUpperCase() === 'OPENAI_API_KEY')?.key_name
      || matches.find((secret) => secret.key_name.toUpperCase().includes('EMBEDDING'))?.key_name
      || matches[0]?.key_name
      || '';
  }

  async function handleMemoryVaultKeyChange(keyName: string) {
    if (keyName === OPEN_VAULT_SELECT_VALUE) {
      await openVaultForMemoryKey(memoryVaultProvider);
      return;
    }
    if (!keyName || !canManageSettings) return;
    await selectMemoryVaultKey(keyName);
  }

  async function selectMemoryVaultKey(keyName: string) {
    const provider = memoryVaultProvider;
    const token = vaultToken || readPersistedVaultSession();
    if (!token) {
      vaultToken = null;
      vaultLocked = true;
      selectedMemoryVaultKey = '';
      await openVaultForMemoryKey(provider);
      return;
    }

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
        vaultLocked = true;
        selectedMemoryVaultKey = '';
        clearPersistedVaultSession();
      }
      notice = errorNotice('Memory key was not selected.', error, 'Unlock Vault and try again.');
    } finally {
      syncingMemoryVaultKey = false;
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
        const keyLabel = memoryDraft.embedder === 'gemini' ? 'Gemini' : 'OpenAI';
        return {
          tone: hasKey ? 'info' : 'warning',
          title: hasKey ? `${keyLabel} memory selected.` : `${keyLabel} key needed.`,
          detail: hasKey ? 'Save & check.' : 'Choose a Vault key, then Save & check.',
          showAddKeyAction: !hasKey,
        };
      }
      return {
        tone: 'info',
        title: `${label} selected.`,
        detail: 'Save & check.',
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
        ? `Choose ${article} ${keyLabel} Vault key, or switch to Local CPU.`
        : settings.memory.embedding_detail,
      showAddKeyAction: needsApiKey,
    };
  }

  async function openVaultForMemoryKey(provider = embeddingProvider(memoryDraft.embedder)) {
    const keyName = provider === 'gemini' ? 'GEMINI_API_KEY' : 'OPENAI_EMBEDDING_API_KEY';
    const description = provider === 'gemini'
      ? 'Gemini key for memory embeddings'
      : 'OpenAI key for memory embeddings';
    const params = new URLSearchParams({
      add_secret: keyName,
      category: 'api',
      description,
    });
    await goto(`/vault?${params.toString()}`);
  }

  function updateNoticeDetail(status: RuntimeUpdateStatus | null) {
    const activeRuns = status?.active_agent_runs ?? 0;
    if (activeRuns > 0) {
      return `${activeRuns} active AgentRun${activeRuns === 1 ? '' : 's'} will finish before the worker restarts on the new code.`;
    }
    return 'The server is fetching origin/main and applying the update.';
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
  title="AI runtime"
  subtitle="Composer models and memory."
  contentClassName="system-page"
>
  {#snippet actions()}
    {#if canManageSettings}
      <ConstellationButton
        variant="secondary"
        onclick={startIllospaceUpdate}
        loading={updateRunning}
        loadingLabel={startingUpdate ? 'Starting' : 'Updating'}
        disabled={updateStatus?.available === false}
      >
        {#snippet leadingVisual()}
          <ConstellationIcon name="git-branch" size={14} />
        {/snippet}
        Update Illospace
      </ConstellationButton>
    {/if}
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
    <div class="system-loading">Loading runtime...</div>
  {:else if settings}
    <div class="runtime-config-grid">
      <div class="setup-section setup-section-models" data-setup-section="models">
        <ModelsCard
          description=""
          connectionStatus={connectionStatus}
          {modelDraft}
          {modelOptions}
          {oauthUrl}
          {oauthCallback}
          oauthPending={Boolean(oauthUrl)}
          {oauthCallbackAvailable}
          {oauthCallbackMode}
          {canManageSettings}
          {savingConnection}
          {savingModels}
          onUpdateModel={updateModelTier}
          onSaveModels={saveModels}
          onCallbackChange={(value) => (oauthCallback = value)}
          onStartCodexSignIn={() => startCodexSignIn()}
          onStartLocalCodexSignIn={() => startCodexSignIn('local_bridge')}
          onFinishCodexSignIn={finishCodexSignIn}
        />
      </div>

      <div class="setup-section setup-section-memory" data-setup-section="memory">
        <MemoryCard
          description=""
          memory={settings.memory}
          {memoryDraft}
          embeddingModelOptions={embeddingModelOptions()}
          vaultKeyOptions={memoryVaultKeyOptions}
          selectedVaultKey={selectedMemoryVaultKey}
          notice={memoryNotice()}
          {memoryCheck}
          {canManageSettings}
          {checkingMemory}
          {vaultLoading}
          syncingVaultKey={syncingMemoryVaultKey}
          onCheckMemory={checkMemory}
          onUpdateMemory={updateMemoryDraft}
          onSelectVaultKey={handleMemoryVaultKeyChange}
          onAddApiKey={() => openVaultForMemoryKey()}
          checkDisabled={memoryChangeNeedsRebuild()}
        />
      </div>
    </div>
  {/if}
</ConstellationPageFrame>

<style>
  :global(.system-page) {
    display: grid;
    gap: 20px;
  }

  .runtime-config-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 18px;
    align-items: start;
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
