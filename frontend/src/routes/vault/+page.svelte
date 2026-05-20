<script lang="ts">
  import { getContext, onMount, onDestroy } from 'svelte';
  import { dev } from '$app/environment';
  import { page } from '$app/stores';
  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationEmptyState,
    ConstellationIcon,
    ConstellationIconButton,
    ConstellationNotice,
    ConstellationPageFrame,
    ConstellationPageTabs,
    ConstellationPanel,
    ConstellationPill,
    ConstellationSearchField,
    ConstellationSelect,
    ConstellationTextarea,
    ConstellationTextInput,
  } from '$lib/components/constellation';
  import {
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
    type ConstellationPageFrameModalContext,
  } from '$lib/components/constellation/constellationPageFrameContext';
  import { auth } from '$lib/stores/auth.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { parseServerDate, relativeTimeAgo } from '$lib/utils/datetime';

  interface Secret {
    id: number;
    key_name: string;
    description: string;
    category: string;
    created_at: string;
    updated_at: string;
    last_accessed_at: string;
    access_count: number;
    agent_access_level: 'available' | 'ask' | 'manual';
  }

  interface MissingSecret {
    key_name: string;
    request_count: number;
    last_requested: string;
  }

  interface AgentGrant {
    id: number;
    key_name: string;
    run_id: number | null;
    requested_by: string;
    reason: string;
    status: string;
    requested_at: string;
    expires_at: string | null;
    read_count: number;
    max_reads: number;
  }

  interface ProjectBinding {
    id: number;
    secret_id: number;
    key_name: string | null;
    agent_access_level: string | null;
    project_slug: string;
    env_name: string;
    target_registry_id: number | null;
    active: boolean;
  }

  interface ExternalAgentConnection {
    id: string;
    org_id: string;
    owner_user_id: string;
    display_name: string;
    agent_kind: string;
    transport: string;
    status: string;
    endpoint_url: string | null;
    remote_agent_id: string | null;
    remote_session_key: string | null;
    remote_agent_card: Record<string, any>;
    capabilities: Record<string, any>;
    last_seen_at: string | null;
    last_tested_at: string | null;
    last_error: string | null;
    metadata: Record<string, any>;
    disabled_at: string | null;
    created_at: string | null;
    updated_at: string | null;
  }

  interface ExternalAgentTokenRead {
    id: string;
    connection_id: string;
    token_prefix: string;
    name: string;
    scopes: string[];
    created_at: string | null;
    last_used_at: string | null;
    expires_at: string | null;
    revoked_at: string | null;
    token: string | null;
  }

  interface VaultUnlockResponse {
    token: string;
    expires_at?: string | null;
  }

  interface StoredVaultSession {
    token: string;
    expiresAt: string;
    savedAt: string;
  }

  type VaultTab = 'library' | 'mcp';
  type PillTone = 'muted' | 'warning' | 'success' | 'danger' | 'info';
  type DevVaultSample = {
    key_name: string;
    description: string;
    category: string;
  };
  type VaultInitialCreatePrefill = {
    id?: string | null;
    keyName?: string | null;
    description?: string | null;
    category?: string | null;
  };
  type VaultInitialAgentGrantPrompt = {
    id?: string | null;
    grantId: number;
    keyName?: string | null;
    reason?: string | null;
  };
  type VaultRow =
    | { id: string; kind: 'secret'; secret: Secret }
    | { id: string; kind: 'grant'; grant: AgentGrant }
    | { id: string; kind: 'missing'; missing: MissingSecret }
    | { id: string; kind: 'pin' }
    | { id: string; kind: 'dev_sample'; sample: DevVaultSample };

  const CATEGORIES = ['general', 'api', 'aws', 'auth', 'analytics', 'database', 'messaging', 'monitoring', 'payments', 'service'];
  const AGENT_KIND_OPTIONS = [
    { key: 'hermes', label: 'Hermes' },
    { key: 'codex', label: 'Codex' },
    { key: 'openclaw', label: 'OpenClaw' },
    { key: 'custom', label: 'Custom MCP client' },
  ];
  const VAULT_SESSION_STORAGE_PREFIX = 'illo:vault:unlock:v1';
  const VAULT_SESSION_EXPIRY_SKEW_MS = 5000;
  const VAULT_TABS = [
    { key: 'library', label: 'Library' },
    { key: 'mcp', label: 'MCP' },
  ];
  const DEV_VAULT_SAMPLE: DevVaultSample = {
    key_name: 'OPENAI_EMBEDDING_API_KEY',
    description: 'Dev-only preview key for memory embeddings.',
    category: 'api',
  };
  const ACCESS_LEVELS = [
    { key: 'ask', label: 'Ask Each Run' },
    { key: 'available', label: 'Agent Available' },
    { key: 'manual', label: 'Manual Only' },
  ];
  const CATEGORY_SELECT_OPTIONS = CATEGORIES.map((category) => ({ value: category, label: category }));
  const AGENT_KIND_SELECT_OPTIONS = AGENT_KIND_OPTIONS.map((option) => ({
    value: option.key,
    label: option.label,
  }));
  const ACCESS_LEVEL_SELECT_OPTIONS = ACCESS_LEVELS.map((level) => ({
    value: level.key,
    label: level.label,
  }));

  let {
    embedded = false,
    initialCreatePrefill = null,
    initialAgentGrantPrompt = null,
    onInitialCreateSaved,
    onInitialAgentGrantHandled,
  }: {
    embedded?: boolean;
    initialCreatePrefill?: VaultInitialCreatePrefill | null;
    initialAgentGrantPrompt?: VaultInitialAgentGrantPrompt | null;
    onInitialCreateSaved?: (prefillId?: string | null) => void;
    onInitialAgentGrantHandled?: (promptId?: string | null) => void;
  } = $props();

  let secrets = $state<Secret[]>([]);
  let missing = $state<MissingSecret[]>([]);
  let agentGrants = $state<AgentGrant[]>([]);
  let projectBindings = $state<ProjectBinding[]>([]);
  let agentConnections = $state<ExternalAgentConnection[]>([]);
  let loading = $state(true);
  let filterText = $state('');
  let activeVaultTab = $state<VaultTab>('library');
  let selectedRowId = $state<string | null>(null);
  let approvalModalGrantId = $state<number | null>(null);
  let initialAgentGrantFocusAppliedFor = $state<string | null>(null);
  let initialAgentGrantRefreshRequestedFor = $state<string | null>(null);

  const workspacePageModalContext = getContext<ConstellationPageFrameModalContext | undefined>(
    CONSTELLATION_PAGE_FRAME_MODAL_CONTEXT,
  );

  $effect(() => {
    return workspacePageModalContext?.registerRefreshAction({
      label: loading ? 'Loading vault' : 'Refresh vault',
      disabled: loading,
      onclick: refreshVault,
    });
  });

  // PIN state
  let hasPin = $state(false);
  let vaultLocked = $state(false);
  let vaultToken = $state<string | null>(null);
  let pinInput = $state('');
  let pinAttempts = $state(0);
  let vaultLockedUntil = $state<string | null>(null);
  let showPinInput = $state(false);
  let showPinSetup = $state(false);
  let newPin = $state('');
  let confirmPin = $state('');
  let currentPin = $state('');
  let pinSaving = $state(false);

  // Create form
  let showCreateModal = $state(false);
  let formKeyName = $state('');
  let formValue = $state('');
  let formDescription = $state('');
  let formCategory = $state('general');
  let formAgentAccessLevel = $state<'available' | 'ask' | 'manual'>('ask');
  let formSaving = $state(false);
  let showPassword = $state(false);
  let initialCreatePrefillApplied = $state(false);
  let appliedInitialCreatePrefillSignature = $state<string | null>(null);

  // Edit form
  let showEditModal = $state(false);
  let editKey = $state('');
  let editValue = $state('');
  let editDescription = $state('');
  let editCategory = $state('general');
  let editAgentAccessLevel = $state<'available' | 'ask' | 'manual'>('ask');
  let editSaving = $state(false);
  let showEditPassword = $state(false);

  // Project binding state
  let showBindModal = $state(false);
  let bindSecretId = $state(0);
  let bindSecretName = $state('');
  let bindProjectSlug = $state('');
  let bindEnvName = $state('');
  let bindSaving = $state(false);

  // Personal agent connection state
  let showAgentConnectionModal = $state(false);
  let agentFormDisplayName = $state('Hermes');
  let agentFormKind = $state('hermes');
  let agentConnectionSaving = $state(false);
  let mintedAgentToken = $state<ExternalAgentTokenRead | null>(null);
  let mintedAgentTokenConnection = $state<ExternalAgentConnection | null>(null);
  let agentConnectionTokens = $state<Record<string, ExternalAgentTokenRead[]>>({});
  let revokingAgentTokenIds = $state<string[]>([]);
  let deletingAgentConnectionIds = $state<string[]>([]);

  // Reveal state
  let revealed = $state<Record<string, string>>({});
  let revealTimers: Record<string, ReturnType<typeof setTimeout>> = {};

  // Clipboard feedback
  let copiedKey = $state('');

  const isVaultPreview = $derived(dev && $page.url.searchParams.get('preview') === '1');
  const frameClassName = $derived(
    ['vault-constellation-frame', embedded ? 'is-embedded' : ''].filter(Boolean).join(' '),
  );
  const frameContentClassName = $derived(
    ['vault-page', embedded ? 'is-embedded' : ''].filter(Boolean).join(' '),
  );
  const vaultSessionStorageKey = $derived(
    auth.user?.org_id && auth.user?.id
      ? `${VAULT_SESSION_STORAGE_PREFIX}:${String(auth.user.org_id)}:${String(auth.user.id)}`
      : '',
  );
  const vaultLockoutMessage = $derived(
    vaultLockedUntil ? `Too many attempts. Try again ${relativeTime(vaultLockedUntil)}.` : '',
  );
  const hostedMcpUrl = $derived.by(
    () => (typeof window === 'undefined' ? '/mcp' : `${window.location.origin}/mcp`),
  );
  const pendingAgentGrants = $derived.by(() => agentGrants.filter((grant) => grant.status === 'pending'));
  const approvalModalGrant = $derived.by(() =>
    pendingAgentGrants.find((grant) => grant.id === approvalModalGrantId) ?? null,
  );
  const staleSecrets = $derived.by(() => secrets.filter((secret) => secretAgeNumber(secret.updated_at) >= 180));
  const vaultRows = $derived.by<VaultRow[]>(() => [
    ...(!hasPin ? [{ id: 'pin:setup', kind: 'pin' as const }] : []),
    ...pendingAgentGrants.map((grant) => ({ id: `grant:${grant.id}`, kind: 'grant' as const, grant })),
    ...missing.map((item) => ({ id: `missing:${item.key_name}`, kind: 'missing' as const, missing: item })),
    ...secrets.map((secret) => ({ id: `secret:${secret.key_name}`, kind: 'secret' as const, secret })),
  ]);
  const devVaultSampleRows = $derived.by<VaultRow[]>(() => {
    const shouldShow =
      dev &&
      !loading &&
      hasPin &&
      secrets.length === 0 &&
      missing.length === 0 &&
      pendingAgentGrants.length === 0;

    return shouldShow ? [{ id: `dev:${DEV_VAULT_SAMPLE.key_name}`, kind: 'dev_sample', sample: DEV_VAULT_SAMPLE }] : [];
  });
  const libraryRows = $derived.by<VaultRow[]>(() => [...vaultRows, ...devVaultSampleRows]);
  const filteredRows = $derived.by(() => {
    const needle = filterText.trim().toLowerCase();
    return needle ? libraryRows.filter((row) => rowSearchText(row).includes(needle)) : libraryRows;
  });
  const postureLabel = $derived.by(() => {
    if (!hasPin) return 'Setup needed';
    if (pendingAgentGrants.length > 0) return 'Approval waiting';
    if (missing.length > 0) return 'Missing keys';
    if (staleSecrets.length > 0) return 'Rotation review';
    return 'Ready';
  });
  const postureItems = $derived.by(() => [
    {
      label: 'PIN',
      ok: hasPin,
      detail: hasPin ? 'Configured' : 'Not configured',
    },
    {
      label: 'Agent grants',
      ok: pendingAgentGrants.length === 0,
      detail: pendingAgentGrants.length === 0 ? 'No approvals waiting' : `${pendingAgentGrants.length} waiting`,
    },
    {
      label: 'Missing keys',
      ok: missing.length === 0,
      detail: missing.length === 0 ? 'None requested' : `${missing.length} requested`,
    },
    {
      label: 'Rotation',
      ok: staleSecrets.length === 0,
      detail: staleSecrets.length === 0 ? 'No stale keys' : `${staleSecrets.length} older than 180d`,
    },
  ]);
  onMount(async () => {
    if (isVaultPreview) {
      loadPreviewData();
      maybeApplyInitialCreatePrefill();
      return;
    }
    await checkPin();
    if (!vaultLocked) {
      await loadData();
    }
  });

  onDestroy(() => {
    Object.values(revealTimers).forEach(clearTimeout);
  });

  $effect(() => {
    if (!initialCreatePrefill?.keyName || loading || vaultLocked) return;
    maybeApplyInitialCreatePrefill();
  });

  $effect(() => {
    const grantId = Number(initialAgentGrantPrompt?.grantId ?? 0);
    const promptId = initialAgentGrantPrompt?.id || (grantId > 0 ? `grant:${grantId}` : '');
    if (!Number.isSafeInteger(grantId) || grantId <= 0) {
      initialAgentGrantFocusAppliedFor = null;
      initialAgentGrantRefreshRequestedFor = null;
      return;
    }
    if (loading || vaultLocked) return;

    const grant = pendingAgentGrants.find((candidate) => candidate.id === grantId);
    if (!grant) {
      if (initialAgentGrantRefreshRequestedFor !== promptId) {
        initialAgentGrantRefreshRequestedFor = promptId;
        void loadData();
      }
      return;
    }
    if (initialAgentGrantFocusAppliedFor === promptId) return;

    initialAgentGrantFocusAppliedFor = promptId;
    activeVaultTab = 'library';
    filterText = '';
    selectedRowId = `grant:${grant.id}`;
    approvalModalGrantId = grant.id;
  });

  function previewIso(daysAgo: number, hoursAgo = 0): string {
    return new Date(Date.now() - daysAgo * 86400000 - hoursAgo * 3600000).toISOString();
  }

  function loadPreviewData() {
    hasPin = true;
    vaultLocked = false;
    vaultToken = null;
    secrets = [
      {
        id: 101,
        key_name: 'OPENAI_API_KEY',
        description: 'Model access for approved agent runs.',
        category: 'api',
        created_at: previewIso(112),
        updated_at: previewIso(12),
        last_accessed_at: previewIso(0, 3),
        access_count: 34,
        agent_access_level: 'available',
      },
      {
        id: 102,
        key_name: 'SUPABASE_SERVICE_ROLE',
        description: 'Database maintenance key kept behind one-use grants.',
        category: 'database',
        created_at: previewIso(280),
        updated_at: previewIso(214),
        last_accessed_at: previewIso(42),
        access_count: 6,
        agent_access_level: 'ask',
      },
      {
        id: 103,
        key_name: 'STRIPE_WEBHOOK_SECRET',
        description: 'Webhook verification for billing events.',
        category: 'payments',
        created_at: previewIso(74),
        updated_at: previewIso(23),
        last_accessed_at: previewIso(7),
        access_count: 11,
        agent_access_level: 'manual',
      },
    ];
    projectBindings = [
      {
        id: 701,
        secret_id: 101,
        key_name: 'OPENAI_API_KEY',
        agent_access_level: 'available',
        project_slug: 'example-repo',
        env_name: 'OPENAI_API_KEY',
        target_registry_id: null,
        active: true,
      },
    ];
    agentConnections = [
      {
        id: 'preview-hermes',
        org_id: 'preview-org',
        owner_user_id: 'preview-user',
        display_name: 'Hermes',
        agent_kind: 'hermes',
        transport: 'hosted_mcp',
        status: 'configured',
        endpoint_url: hostedMcpUrl,
        remote_agent_id: null,
        remote_session_key: null,
        remote_agent_card: {},
        capabilities: { mcp: true, hosted_mcp: true },
        last_seen_at: previewIso(0, 2),
        last_tested_at: previewIso(0, 2),
        last_error: null,
        metadata: { source: 'vault_preview' },
        disabled_at: null,
        created_at: previewIso(3),
        updated_at: previewIso(0, 2),
      },
    ];
    missing = [
      {
        key_name: 'BRAVE_SEARCH_API_KEY',
        request_count: 5,
        last_requested: previewIso(0, 1),
      },
    ];
    agentGrants = [
      {
        id: 501,
        key_name: 'GITHUB_TOKEN',
        run_id: 942,
        requested_by: 'deployment-agent',
        reason: 'Needs one read to inspect release permissions for the active deploy.',
        status: 'pending',
        requested_at: previewIso(0, 0.25),
        expires_at: null,
        read_count: 0,
        max_reads: 1,
      },
      {
        id: 502,
        key_name: 'OPENAI_API_KEY',
        run_id: 921,
        requested_by: 'coding-agent',
        reason: 'Used to run a model capability smoke test.',
        status: 'used',
        requested_at: previewIso(2),
        expires_at: previewIso(1),
        read_count: 1,
        max_reads: 1,
      },
      {
        id: 503,
        key_name: 'OPENAI_API_KEY',
        run_id: 918,
        requested_by: 'browser-agent',
        reason: 'Not needed after task scope changed.',
        status: 'denied',
        requested_at: previewIso(5),
        expires_at: null,
        read_count: 0,
        max_reads: 1,
      },
    ];
    selectedRowId = 'secret:OPENAI_API_KEY';
    loading = false;
  }

  function previewSecretValue(keyName: string): string {
    return `preview_${keyName.toLowerCase()}_not_a_real_secret`;
  }

  function previewToast() {
    ui.toast('Preview mode: no backend user or real secrets attached.', 'info');
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
      // Storage can be unavailable in hardened browser modes.
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

  function persistVaultSession(unlocked: VaultUnlockResponse) {
    const storage = getVaultSessionStorage();
    if (!storage || !vaultSessionStorageKey || !unlocked.token || !unlocked.expires_at) return;
    const expiresAt = String(unlocked.expires_at);
    try {
      storage.setItem(
        vaultSessionStorageKey,
        JSON.stringify({
          token: unlocked.token,
          expiresAt,
          savedAt: new Date().toISOString(),
        } satisfies StoredVaultSession),
      );
    } catch {
      // Unlock should still succeed if browser storage is blocked.
    }
  }

  async function checkPin() {
    try {
      const status = await api.pinStatus();
      hasPin = status.has_pin;
      pinAttempts = Number(status.failed_attempts || 0);
      vaultLockedUntil = status.locked_until ? String(status.locked_until) : null;
      if (!hasPin) {
        vaultLocked = false;
        clearPersistedVaultSession();
        return;
      }
      vaultToken = readPersistedVaultSession();
      vaultLocked = !vaultToken;
    } catch {
      // PIN check failed — assume no PIN
    }
  }

  async function unlockVault() {
    if (!pinInput) return;
    try {
      const unlocked: VaultUnlockResponse = await api.vaultUnlock(pinInput);
      vaultToken = unlocked.token;
      persistVaultSession(unlocked);
      vaultLocked = false;
      pinInput = '';
      pinAttempts = 0;
      vaultLockedUntil = null;
      await loadData();
    } catch (err: any) {
      try {
        const status = await api.pinStatus();
        pinAttempts = Number(status.failed_attempts || pinAttempts + 1);
        vaultLockedUntil = status.locked_until ? String(status.locked_until) : null;
      } catch {
        pinAttempts++;
      }
      pinInput = '';
      showPinInput = false;
      ui.toast(vaultLockoutMessage || err?.detail || `Incorrect PIN (attempt ${pinAttempts})`, 'error');
    }
  }

  async function setupPin() {
    if (isVaultPreview) {
      previewToast();
      showPinSetup = false;
      return;
    }
    if (newPin.length < 4) {
      ui.toast('PIN must be at least 4 characters', 'error');
      return;
    }
    if (newPin !== confirmPin) {
      ui.toast('PINs do not match', 'error');
      return;
    }
    pinSaving = true;
    try {
      const data: { new_pin: string; current_pin?: string } = { new_pin: newPin };
      if (hasPin) data.current_pin = currentPin;
      await api.vaultSetupPin(data);
      const unlocked: VaultUnlockResponse = await api.vaultUnlock(newPin);
      vaultToken = unlocked.token;
      persistVaultSession(unlocked);
      hasPin = true;
      vaultLocked = false;
      vaultLockedUntil = null;
      showPinSetup = false;
      newPin = '';
      confirmPin = '';
      currentPin = '';
      await loadData();
      ui.toast('PIN configured', 'success');
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to set PIN', 'error');
    } finally {
      pinSaving = false;
    }
  }

  function clearRevealedSecrets() {
    revealed = {};
    Object.values(revealTimers).forEach(clearTimeout);
    revealTimers = {};
  }

  function markVaultLocked(showToast = false) {
    vaultToken = null;
    clearPersistedVaultSession();
    vaultLocked = true;
    clearRevealedSecrets();
    if (showToast) {
      ui.toast('Vault locked. Unlock to continue.', 'error');
    }
  }

  function handleVaultError(err: any, fallback: string) {
    if (err?.status === 423) {
      if (err?.detail === 'Vault PIN setup required') {
        hasPin = false;
        vaultLocked = false;
        vaultToken = null;
        clearPersistedVaultSession();
        showPinSetup = true;
        ui.toast('Set your Vault PIN before changing protected secrets.', 'error');
        return;
      }
      markVaultLocked(true);
      return;
    }
    ui.toast(err?.detail || fallback, 'error');
  }

  function handleOptionalVaultDataError(err: any, fallback: string): boolean {
    if (err?.status === 403) {
      return false;
    }
    handleVaultError(err, fallback);
    return true;
  }

  async function lockVault() {
    if (isVaultPreview) {
      previewToast();
      return;
    }
    if (vaultToken) {
      api.vaultLock(vaultToken).catch(() => undefined);
    }
    markVaultLocked();
  }

  async function loadData() {
    if (isVaultPreview) {
      loadPreviewData();
      return;
    }
    loading = true;
    try {
      secrets = await api.listSecrets(undefined, vaultToken);

      const [missingResult, grantsResult, bindingsResult, connectionsResult] = await Promise.allSettled([
        api.missingSecrets(vaultToken),
        api.vaultAgentGrants(vaultToken, 'pending,approved,used,denied'),
        api.vaultProjectBindings(vaultToken),
        api.listAgentConnections(),
      ]);

      if (missingResult.status === 'fulfilled') {
        missing = missingResult.value;
      } else if (!handleOptionalVaultDataError(missingResult.reason, 'Failed to load missing vault keys')) {
        missing = [];
      }

      if (grantsResult.status === 'fulfilled') {
        agentGrants = grantsResult.value;
      } else if (!handleOptionalVaultDataError(grantsResult.reason, 'Failed to load vault grants')) {
        agentGrants = [];
      }

      if (bindingsResult.status === 'fulfilled') {
        projectBindings = bindingsResult.value;
      } else if (!handleOptionalVaultDataError(bindingsResult.reason, 'Failed to load project token bindings')) {
        projectBindings = [];
      }

      if (connectionsResult.status === 'fulfilled') {
        agentConnections = connectionsResult.value;
        await loadAgentConnectionTokens(agentConnections);
      } else {
        agentConnections = [];
        agentConnectionTokens = {};
      }

      maybeApplyInitialCreatePrefill();
    } catch (err: any) {
      if (err.status === 423) {
        markVaultLocked();
        return;
      }
      ui.toast(err.detail || 'Failed to load vault', 'error');
    } finally {
      loading = false;
    }
  }

  async function refreshVault() {
    if (isVaultPreview) {
      loadPreviewData();
      return;
    }
    await checkPin();
    if (!vaultLocked) {
      await loadData();
    }
  }

  function selectRow(row: VaultRow) {
    selectedRowId = selectedRowId === row.id ? null : row.id;
  }

  function setActiveVaultTab(key: string) {
    if (key === 'library' || key === 'mcp') {
      activeVaultTab = key;
    }
  }

  function secretGrantHistory(keyName: string): AgentGrant[] {
    return agentGrants.filter((grant) => grant.key_name === keyName && grant.status !== 'pending');
  }

  function secretProjectBindings(secretId: number): ProjectBinding[] {
    return projectBindings.filter((binding) => binding.secret_id === secretId && binding.active !== false);
  }

  function accessLevelLabel(level: string | undefined): string {
    return ACCESS_LEVELS.find((item) => item.key === (level || 'ask'))?.label || 'Ask Each Run';
  }

  function accessLevelVariant(level: string | undefined): PillTone {
    if (level === 'available') return 'success';
    if (level === 'manual') return 'muted';
    return 'warning';
  }

  function agentKindLabel(kind: string | undefined): string {
    return AGENT_KIND_OPTIONS.find((item) => item.key === (kind || '').toLowerCase())?.label || kind || 'Personal agent';
  }

  function rowTitle(row: VaultRow): string {
    if (row.kind === 'pin') return 'PIN not configured';
    if (row.kind === 'grant') return row.grant.key_name;
    if (row.kind === 'missing') return row.missing.key_name;
    if (row.kind === 'dev_sample') return row.sample.key_name;
    return row.secret.key_name;
  }

  function rowDescription(row: VaultRow): string {
    if (row.kind === 'pin') return 'Set a lock before storing production credentials.';
    if (row.kind === 'grant') return row.grant.reason || `${row.grant.requested_by || 'agent'} requested access.`;
    if (row.kind === 'missing') return `Requested ${row.missing.request_count}x · last seen ${timeAgo(row.missing.last_requested)}`;
    if (row.kind === 'dev_sample') return row.sample.description;
    return row.secret.description || row.secret.category || 'No description yet.';
  }

  function rowStatusLabel(row: VaultRow): string {
    if (row.kind === 'pin') return 'PIN';
    if (row.kind === 'grant') return row.grant.status;
    if (row.kind === 'missing') return 'Missing';
    if (row.kind === 'dev_sample') return 'Dev only';
    return accessLevelLabel(row.secret.agent_access_level);
  }

  function rowStatusDetail(row: VaultRow): string {
    if (row.kind === 'pin') return 'Vault can still be opened by account session.';
    if (row.kind === 'grant') return `${row.grant.requested_by || 'agent'} · run ${row.grant.run_id ?? 'unknown'}`;
    if (row.kind === 'missing') return 'Runtime requested this key.';
    if (row.kind === 'dev_sample') return `${row.sample.category} · preview row`;
    return `${row.secret.category || 'general'} · ${secretProjectBindings(row.secret.id).length} project bindings · x${row.secret.access_count || 0}`;
  }

  function rowStatusVariant(row: VaultRow): PillTone {
    if (row.kind === 'pin') return 'warning';
    if (row.kind === 'grant') return grantPillVariant(row.grant.status);
    if (row.kind === 'missing') return 'warning';
    if (row.kind === 'dev_sample') return 'info';
    return accessLevelVariant(row.secret.agent_access_level);
  }

  function rowSearchText(row: VaultRow): string {
    if (row.kind === 'pin') return 'pin lock setup configured'.toLowerCase();
    if (row.kind === 'grant') {
      return [row.grant.key_name, row.grant.reason, row.grant.requested_by, row.grant.status]
        .join(' ')
        .toLowerCase();
    }
    if (row.kind === 'missing') {
      return [row.missing.key_name, 'missing requested required runtime'].join(' ').toLowerCase();
    }
    if (row.kind === 'dev_sample') {
      return [row.sample.key_name, row.sample.category, row.sample.description, 'development preview']
        .join(' ')
        .toLowerCase();
    }
    return [row.secret.key_name, row.secret.category, row.secret.description].join(' ').toLowerCase();
  }

  async function revealSecret(keyName: string) {
    if (revealed[keyName]) {
      delete revealed[keyName];
      revealed = { ...revealed };
      if (revealTimers[keyName]) {
        clearTimeout(revealTimers[keyName]);
        delete revealTimers[keyName];
      }
      return;
    }
    if (isVaultPreview) {
      revealed[keyName] = previewSecretValue(keyName);
      revealed = { ...revealed };
      revealTimers[keyName] = setTimeout(() => {
        delete revealed[keyName];
        revealed = { ...revealed };
        delete revealTimers[keyName];
      }, 10000);
      return;
    }
    try {
      const data = await api.revealSecret(keyName, vaultToken);
      revealed[keyName] = data.value;
      revealed = { ...revealed };
      revealTimers[keyName] = setTimeout(() => {
        delete revealed[keyName];
        revealed = { ...revealed };
        delete revealTimers[keyName];
      }, 10000);
    } catch (err: any) {
      handleVaultError(err, 'Reveal failed');
    }
  }

  async function copySecret(keyName: string) {
    // First reveal if needed, then copy
    let value = revealed[keyName];
    if (isVaultPreview && !value) {
      value = previewSecretValue(keyName);
    }
    if (!value) {
      try {
        const data = await api.revealSecret(keyName, vaultToken);
        value = data.value;
      } catch (err: any) {
        handleVaultError(err, 'Copy failed');
        return;
      }
    }
    try {
      await navigator.clipboard.writeText(value);
      copiedKey = keyName;
      setTimeout(() => { copiedKey = ''; }, 2000);
    } catch {
      ui.toast('Clipboard access denied', 'error');
    }
  }

  async function deleteSecret(keyName: string) {
    if (isVaultPreview) {
      secrets = secrets.filter((secret) => secret.key_name !== keyName);
      selectedRowId = null;
      ui.toast('Preview secret removed from the mock list', 'success');
      return;
    }
    if (!confirm(`Delete secret "${keyName}"? This cannot be undone.`)) return;
    try {
      await api.deleteSecret(keyName, vaultToken);
      ui.toast('Secret deleted', 'success');
      await loadData();
    } catch (err: any) {
      handleVaultError(err, 'Delete failed');
    }
  }

  function openCreate(
    prefillKey = '',
    prefill: { description?: string | null; category?: string | null } = {},
  ) {
    formKeyName = prefillKey;
    formValue = '';
    formDescription = prefill.description || '';
    formCategory = CATEGORIES.includes(prefill.category || '')
      ? prefill.category || 'general'
      : 'general';
    formAgentAccessLevel = 'ask';
    showPassword = false;
    showCreateModal = true;
  }

  function maybeApplyInitialCreatePrefill() {
    if (vaultLocked) return;
    const propKeyName = (initialCreatePrefill?.keyName || '').trim();
    if (propKeyName) {
      const signature = JSON.stringify([
        propKeyName,
        initialCreatePrefill?.description ?? '',
        initialCreatePrefill?.category ?? '',
      ]);
      if (appliedInitialCreatePrefillSignature === signature) return;
      appliedInitialCreatePrefillSignature = signature;
      openCreate(propKeyName, {
        description: initialCreatePrefill?.description,
        category: initialCreatePrefill?.category,
      });
      return;
    }

    if (initialCreatePrefillApplied) return;
    const params = $page.url.searchParams;
    const keyName = (params.get('add_secret') || params.get('key_name') || '').trim();
    if (!keyName) return;
    initialCreatePrefillApplied = true;
    openCreate(keyName, {
      description: params.get('description'),
      category: params.get('category'),
    });
  }

  function notifyInitialCreateSaved(keyName: string) {
    const prefillKeyName = (initialCreatePrefill?.keyName || '').trim();
    if (!prefillKeyName || prefillKeyName !== keyName.trim()) return;
    onInitialCreateSaved?.(initialCreatePrefill?.id ?? null);
  }

  async function syncRuntimeSecret(keyName: string, value: string) {
    const normalized = keyName.trim().toUpperCase();
    try {
      if (normalized === 'OPENAI_API_KEY') {
        await api.connectRuntimeOpenAIKey({ api_key: value });
        return true;
      }
      if (normalized === 'OPENAI_EMBEDDING_API_KEY') {
        await api.connectRuntimeOpenAIEmbeddingKey({ api_key: value });
        return true;
      }
      if (normalized === 'GEMINI_API_KEY') {
        await api.connectRuntimeGeminiKey({ api_key: value });
        return true;
      }
    } catch (err: any) {
      ui.toast(err?.message || 'Secret saved, but runtime did not accept the key.', 'error');
    }
    return false;
  }

  async function submitCreate() {
    if (!formKeyName.trim() || !formValue) {
      ui.toast('Key name and value are required', 'error');
      return;
    }
    if (isVaultPreview) {
      const now = new Date().toISOString();
      const keyName = formKeyName.trim();
      secrets = [
        {
          id: Math.max(0, ...secrets.map((secret) => secret.id)) + 1,
          key_name: keyName,
          description: formDescription.trim(),
          category: formCategory,
          created_at: now,
          updated_at: now,
          last_accessed_at: '',
          access_count: 0,
          agent_access_level: formAgentAccessLevel,
        },
        ...secrets,
      ];
      missing = missing.filter((item) => item.key_name !== keyName);
      selectedRowId = `secret:${keyName}`;
      showCreateModal = false;
      notifyInitialCreateSaved(keyName);
      ui.toast('Preview secret added', 'success');
      return;
    }
    formSaving = true;
    try {
      const keyName = formKeyName.trim();
      await api.createSecret({
        key_name: keyName,
        value: formValue,
        description: formDescription.trim(),
        category: formCategory,
        agent_access_level: formAgentAccessLevel,
      }, vaultToken);
      const runtimeSynced = await syncRuntimeSecret(keyName, formValue);
      ui.toast(runtimeSynced ? 'Secret created and runtime updated' : 'Secret created', 'success');
      showCreateModal = false;
      notifyInitialCreateSaved(keyName);
      await loadData();
    } catch (err: any) {
      handleVaultError(err, 'Create failed');
    } finally {
      formSaving = false;
    }
  }

  function openEdit(secret: Secret) {
    editKey = secret.key_name;
    editValue = '';
    editDescription = secret.description || '';
    editCategory = secret.category || 'general';
    editAgentAccessLevel = secret.agent_access_level || 'ask';
    showEditPassword = false;
    showEditModal = true;
  }

  async function submitEdit() {
    if (isVaultPreview) {
      secrets = secrets.map((secret) =>
        secret.key_name === editKey
          ? {
              ...secret,
              description: editDescription,
              category: editCategory,
              agent_access_level: editAgentAccessLevel,
              updated_at: new Date().toISOString(),
            }
          : secret,
      );
      showEditModal = false;
      ui.toast('Preview secret updated', 'success');
      return;
    }
    editSaving = true;
    try {
      const data: any = {};
      if (editValue) data.value = editValue;
      if (editDescription !== undefined) data.description = editDescription;
      data.category = editCategory;
      data.agent_access_level = editAgentAccessLevel;
      await api.updateSecret(editKey, data, vaultToken);
      ui.toast('Secret updated', 'success');
      showEditModal = false;
      await loadData();
    } catch (err: any) {
      handleVaultError(err, 'Update failed');
    } finally {
      editSaving = false;
    }
  }

  function openBind(secret: Secret) {
    bindSecretId = secret.id;
    bindSecretName = secret.key_name;
    bindProjectSlug = '';
    bindEnvName = secret.key_name;
    showBindModal = true;
  }

  async function submitProjectBinding() {
    if (!bindProjectSlug.trim() || !bindEnvName.trim()) {
      ui.toast('Project and env name are required', 'error');
      return;
    }
    if (isVaultPreview) {
      const binding: ProjectBinding = {
        id: Math.max(0, ...projectBindings.map((item) => item.id)) + 1,
        secret_id: bindSecretId,
        key_name: bindSecretName,
        agent_access_level: secrets.find((secret) => secret.id === bindSecretId)?.agent_access_level || 'ask',
        project_slug: bindProjectSlug.trim().toLowerCase(),
        env_name: bindEnvName.trim(),
        target_registry_id: null,
        active: true,
      };
      projectBindings = [
        binding,
        ...projectBindings.filter((item) => item.id !== binding.id),
      ];
      showBindModal = false;
      ui.toast('Preview project binding added', 'success');
      return;
    }
    bindSaving = true;
    try {
      await api.vaultBindProjectSecret(bindSecretId, {
        project_slug: bindProjectSlug.trim(),
        env_name: bindEnvName.trim(),
      }, vaultToken);
      ui.toast(`Bound "${bindSecretName}" to project`, 'success');
      showBindModal = false;
      await loadData();
    } catch (err: any) {
      handleVaultError(err, 'Project binding failed');
    } finally {
      bindSaving = false;
    }
  }

  async function deleteProjectBinding(bindingId: number) {
    if (isVaultPreview) {
      projectBindings = projectBindings.filter((binding) => binding.id !== bindingId);
      ui.toast('Preview binding removed', 'success');
      return;
    }
    try {
      await api.vaultDeleteProjectBinding(bindingId, vaultToken);
      ui.toast('Project binding removed', 'success');
      await loadData();
    } catch (err: any) {
      handleVaultError(err, 'Failed to remove project binding');
    }
  }

  function openAgentConnection() {
    agentFormDisplayName = 'Hermes';
    agentFormKind = 'hermes';
    showAgentConnectionModal = true;
  }

  function mcpConnectionFacts(connection: ExternalAgentConnection): string {
    const parts = [
      agentKindLabel(connection.agent_kind),
      connection.last_seen_at ? `last used ${timeAgo(connection.last_seen_at)}` : 'never used',
    ];
    if (connection.created_at) parts.push(`created ${timeAgo(connection.created_at)}`);
    if (connection.last_error) parts.push('error');
    return parts.join(' · ');
  }

  function mcpConnectionTokenFacts(token: ExternalAgentTokenRead): string {
    const parts = [
      token.token_prefix ? `prefix ${token.token_prefix}` : 'token saved',
      token.last_used_at ? `last used ${timeAgo(token.last_used_at)}` : 'never used',
    ];
    if (token.created_at) parts.push(`created ${timeAgo(token.created_at)}`);
    if (token.expires_at) parts.push(`expires ${relativeTime(token.expires_at)}`);
    return parts.join(' · ');
  }

  async function loadAgentConnectionTokens(connections: ExternalAgentConnection[]) {
    if (isVaultPreview || !connections.length) {
      agentConnectionTokens = {};
      return;
    }

    const results = await Promise.allSettled(
      connections.map(async (connection) => ({
        connectionId: connection.id,
        tokens: await api.listAgentConnectionTokens(connection.id),
      })),
    );
    const next: Record<string, ExternalAgentTokenRead[]> = {};
    for (const result of results) {
      if (result.status === 'fulfilled') {
        next[result.value.connectionId] = result.value.tokens;
      }
    }
    agentConnectionTokens = next;
  }

  function previewAgentConnection(displayName: string, kind: string): ExternalAgentConnection {
    const now = new Date().toISOString();
    return {
      id: `preview-${kind}-${Date.now()}`,
      org_id: 'preview-org',
      owner_user_id: 'preview-user',
      display_name: displayName,
      agent_kind: kind,
      transport: 'hosted_mcp',
      status: 'configured',
      endpoint_url: hostedMcpUrl,
      remote_agent_id: null,
      remote_session_key: null,
      remote_agent_card: {},
      capabilities: { mcp: true, hosted_mcp: true },
      last_seen_at: null,
      last_tested_at: now,
      last_error: null,
      metadata: { source: 'vault_preview' },
      disabled_at: null,
      created_at: now,
      updated_at: now,
    };
  }

  function previewAgentToken(connection: ExternalAgentConnection): ExternalAgentTokenRead {
    const now = new Date().toISOString();
    return {
      id: `preview-token-${Date.now()}`,
      connection_id: connection.id,
      token_prefix: 'illo_conn_preview',
      name: `${connection.display_name} MCP token`,
      scopes: [],
      created_at: now,
      last_used_at: null,
      expires_at: null,
      revoked_at: null,
      token: `illo_conn_preview_${connection.agent_kind}_${Math.random().toString(36).slice(2, 12)}`,
    };
  }

  function showMintedAgentToken(connection: ExternalAgentConnection, token: ExternalAgentTokenRead) {
    mintedAgentToken = token;
    mintedAgentTokenConnection = connection;
  }

  function rememberConnectionToken(connectionId: string, token: ExternalAgentTokenRead) {
    agentConnectionTokens = {
      ...agentConnectionTokens,
      [connectionId]: [
        token,
        ...(agentConnectionTokens[connectionId] || []).filter((item) => item.id !== token.id),
      ],
    };
  }

  async function submitAgentConnection() {
    const displayName = agentFormDisplayName.trim();
    if (!displayName) {
      ui.toast('Agent name is required', 'error');
      return;
    }
    if (isVaultPreview) {
      const connection = previewAgentConnection(displayName, agentFormKind);
      const token = previewAgentToken(connection);
      agentConnections = [
        connection,
        ...agentConnections.filter((item) => item.id !== connection.id),
      ];
      rememberConnectionToken(connection.id, token);
      showMintedAgentToken(connection, token);
      showAgentConnectionModal = false;
      ui.toast('Preview MCP token created', 'success');
      return;
    }
    agentConnectionSaving = true;
    try {
      const connection: ExternalAgentConnection = await api.createAgentConnection({
        display_name: displayName,
        agent_kind: agentFormKind,
        transport: 'hosted_mcp',
        endpoint_url: hostedMcpUrl,
        capabilities: { mcp: true, hosted_mcp: true },
        metadata: { created_from: 'vault_personal_agents' },
      });
      const token: ExternalAgentTokenRead = await api.mintAgentConnectionToken(connection.id, {
        name: `${connection.display_name} MCP token`,
      });
      agentConnections = [
        connection,
        ...agentConnections.filter((item) => item.id !== connection.id),
      ];
      rememberConnectionToken(connection.id, token);
      showMintedAgentToken(connection, token);
      showAgentConnectionModal = false;
      ui.toast('MCP token created', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to create MCP token', 'error');
    } finally {
      agentConnectionSaving = false;
    }
  }

  async function mintTokenForConnection(connection: ExternalAgentConnection) {
    if (isVaultPreview) {
      const token = previewAgentToken(connection);
      rememberConnectionToken(connection.id, token);
      showMintedAgentToken(connection, token);
      ui.toast('Preview MCP token created', 'success');
      return;
    }
    try {
      const token: ExternalAgentTokenRead = await api.mintAgentConnectionToken(connection.id, {
        name: `${connection.display_name} MCP token`,
      });
      rememberConnectionToken(connection.id, token);
      showMintedAgentToken(connection, token);
      ui.toast('MCP token created', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to mint token', 'error');
    }
  }

  async function revokeTokenForConnection(connection: ExternalAgentConnection, token: ExternalAgentTokenRead) {
    if (!confirm(`Revoke token "${token.name}" for ${connection.display_name}? This token will stop working.`)) return;

    revokingAgentTokenIds = [...revokingAgentTokenIds, token.id];
    try {
      if (!isVaultPreview) {
        await api.revokeAgentConnectionToken(connection.id, token.id);
      }
      agentConnectionTokens = {
        ...agentConnectionTokens,
        [connection.id]: (agentConnectionTokens[connection.id] || []).filter((item) => item.id !== token.id),
      };
      if (mintedAgentToken?.id === token.id) {
        mintedAgentToken = null;
        mintedAgentTokenConnection = null;
      }
      ui.toast('MCP token revoked', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to revoke token', 'error');
    } finally {
      revokingAgentTokenIds = revokingAgentTokenIds.filter((id) => id !== token.id);
    }
  }

  async function removeAgentConnection(connection: ExternalAgentConnection) {
    if (!confirm(`Remove ${connection.display_name}? Active tokens for this connection will stop working.`)) return;

    deletingAgentConnectionIds = [...deletingAgentConnectionIds, connection.id];
    try {
      if (!isVaultPreview) {
        await api.deleteAgentConnection(connection.id);
      }
      agentConnections = agentConnections.filter((item) => item.id !== connection.id);
      const remainingTokens = { ...agentConnectionTokens };
      delete remainingTokens[connection.id];
      agentConnectionTokens = remainingTokens;
      if (mintedAgentTokenConnection?.id === connection.id) {
        mintedAgentToken = null;
        mintedAgentTokenConnection = null;
      }
      ui.toast('MCP connection removed', 'success');
    } catch (err: any) {
      ui.toast(err?.detail || 'Failed to remove MCP connection', 'error');
    } finally {
      deletingAgentConnectionIds = deletingAgentConnectionIds.filter((id) => id !== connection.id);
    }
  }

  async function copyAgentText(value: string, key: string, message: string) {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      copiedKey = key;
      setTimeout(() => { copiedKey = ''; }, 2000);
      ui.toast(message, 'success');
    } catch {
      ui.toast('Clipboard access denied', 'error');
    }
  }

  function clearInitialAgentGrantPrompt(grantId: number) {
    approvalModalGrantId = null;
    if (Number(initialAgentGrantPrompt?.grantId ?? 0) === grantId) {
      onInitialAgentGrantHandled?.(initialAgentGrantPrompt?.id ?? null);
    }
  }

  async function approveAgentGrant(grantId: number) {
    if (isVaultPreview) {
      agentGrants = agentGrants.map((grant) =>
        grant.id === grantId ? { ...grant, status: 'approved', expires_at: previewIso(-1), max_reads: 1 } : grant,
      );
      clearInitialAgentGrantPrompt(grantId);
      ui.toast('Preview grant approved', 'success');
      return;
    }
    try {
      await api.vaultApproveGrant(grantId, { ttl_minutes: 15, max_reads: 1 }, vaultToken);
      clearInitialAgentGrantPrompt(grantId);
      ui.toast('Agent grant approved', 'success');
      await loadData();
    } catch (err: any) {
      handleVaultError(err, 'Approval failed');
    }
  }

  async function denyAgentGrant(grantId: number) {
    if (isVaultPreview) {
      agentGrants = agentGrants.map((grant) =>
        grant.id === grantId ? { ...grant, status: 'denied' } : grant,
      );
      clearInitialAgentGrantPrompt(grantId);
      ui.toast('Preview grant denied', 'success');
      return;
    }
    try {
      await api.vaultDenyGrant(grantId, vaultToken);
      clearInitialAgentGrantPrompt(grantId);
      ui.toast('Agent grant denied', 'success');
      await loadData();
    } catch (err: any) {
      handleVaultError(err, 'Deny failed');
    }
  }

  function timeAgo(iso: string | undefined): string {
    return relativeTimeAgo(iso) || 'never';
  }

  function relativeTime(iso: string | undefined): string {
    if (!iso) return 'never';
    const time = parseServerDate(iso)?.getTime();
    if (!time) return 'unknown';
    const ms = time - Date.now();
    if (ms <= 0) return timeAgo(iso);
    const mins = Math.ceil(ms / 60000);
    if (mins < 60) return `in ${mins}m`;
    const hrs = Math.ceil(mins / 60);
    if (hrs < 24) return `in ${hrs}h`;
    return `in ${Math.ceil(hrs / 24)}d`;
  }

  function secretAgeNumber(updatedAt: string | undefined): number {
    if (!updatedAt) return Number.POSITIVE_INFINITY;
    const time = new Date(updatedAt).getTime();
    if (Number.isNaN(time)) return Number.POSITIVE_INFINITY;
    return Math.max(0, Math.floor((Date.now() - time) / 86400000));
  }

  function secretAgeDays(updatedAt: string | undefined): string {
    const days = secretAgeNumber(updatedAt);
    if (!Number.isFinite(days)) return '?';
    return `${days}d`;
  }

  function agePillVariant(updatedAt: string | undefined): 'success' | 'warning' | 'danger' {
    const days = secretAgeNumber(updatedAt);
    if (!Number.isFinite(days)) return 'danger';
    if (days < 90) return 'success';
    if (days < 180) return 'warning';
    return 'danger';
  }

  function grantPillVariant(status: string): 'success' | 'warning' | 'danger' | 'muted' | 'info' {
    if (status === 'approved') return 'info';
    if (status === 'used') return 'success';
    if (status === 'denied') return 'danger';
    if (status === 'pending') return 'warning';
    return 'muted';
  }

  function grantDescription(grant: AgentGrant): string {
    const parts = [
      grant.requested_by || 'agent',
      `run ${grant.run_id ?? 'unknown'}`,
      `requested ${timeAgo(grant.requested_at)}`,
    ];
    if (grant.expires_at) parts.push(`expires ${relativeTime(grant.expires_at)}`);
    return parts.join(' · ');
  }
</script>

<!-- Lock Screen -->
{#if vaultLocked}
  <ConstellationPageFrame
    eyebrow="Constellation Vault"
    title="Vault"
    subtitle="Protected secrets, missing runtime keys, and shared access."
    className={frameClassName}
  >
    <div class="vault-constellation-lock-panel">
      <ConstellationPanel tone="warning">
        <div class="vault-constellation-lock-shell">
          <div class="vault-constellation-lock-mark" aria-hidden="true">
            <ConstellationIcon name="lock" size={20} />
          </div>
          <div class="vault-constellation-lock-copy">
            <h2 class="vault-constellation-lock-title">Vault locked</h2>
            <p class="vault-constellation-lock-subtitle">Enter your PIN to unlock protected secrets.</p>
          </div>
          <form class="vault-constellation-lock-form" onsubmit={(e) => { e.preventDefault(); unlockVault(); }}>
            <!-- svelte-ignore a11y_autofocus -->
            <ConstellationTextInput
              type={showPinInput ? 'text' : 'password'}
              className="vault-constellation-lock-input"
              placeholder="Enter PIN"
              bind:value={pinInput}
              maxlength={32}
              autofocus
              mono
              trailingInteractive
            >
              {#snippet trailingVisual()}
                <ConstellationIconButton
                  label={showPinInput ? 'Hide PIN' : 'Show PIN'}
                  title={showPinInput ? 'Hide PIN' : 'Show PIN'}
                  size="sm"
                  variant="quiet"
                  className="vault-constellation-lock-reveal"
                  onclick={(event) => {
                    event.preventDefault();
                    showPinInput = !showPinInput;
                  }}
                >
                  <ConstellationIcon name={showPinInput ? 'eye-off' : 'eye'} />
                </ConstellationIconButton>
              {/snippet}
            </ConstellationTextInput>
            {#if pinAttempts > 0}
              <p class="vault-constellation-lock-attempts">
                {vaultLockoutMessage || `${pinAttempts} failed attempt${pinAttempts !== 1 ? 's' : ''}`}
              </p>
            {/if}
            <ConstellationButton type="submit" variant="secondary">Unlock</ConstellationButton>
          </form>
        </div>
      </ConstellationPanel>
    </div>
  </ConstellationPageFrame>
{:else}
  <ConstellationPageFrame
    eyebrow="Constellation Vault"
    title="Vault"
    subtitle="Manage secrets and agent access."
    className={frameClassName}
    contentClassName={frameContentClassName}
  >
    {#snippet actions()}
      {#if hasPin}
        <ConstellationButton variant="quiet" size="sm" onclick={lockVault}>
          {#snippet leadingVisual()}
            <ConstellationIcon name="lock-open" />
          {/snippet}
          Lock vault
        </ConstellationButton>
      {/if}
      <ConstellationButton variant="quiet" size="sm" onclick={() => (showPinSetup = true)}>
        {hasPin ? 'Change PIN' : 'Setup PIN'}
      </ConstellationButton>
    {/snippet}

    {#snippet tabs()}
      <ConstellationPageTabs
        options={VAULT_TABS}
        activeKey={activeVaultTab}
        onActiveKeyChange={setActiveVaultTab}
        ariaLabel="Vault sections"
      />
    {/snippet}

    <section class="workspace">
      {#if activeVaultTab === 'mcp'}
        <div
          class="vault-tab-panel agent-pane"
          id="mcp-panel"
          role="tabpanel"
          aria-labelledby="mcp-tab"
        >
          <div class="mcp-tab-toolbar">
            <div class="mcp-endpoint">
              <div class="mcp-endpoint-copy">
                <span class="mcp-endpoint-label">Endpoint</span>
                <span class="mcp-endpoint-value">{hostedMcpUrl}</span>
              </div>
              <ConstellationIconButton
                label={copiedKey === 'mcp-endpoint' ? 'Endpoint copied' : 'Copy endpoint'}
                title={copiedKey === 'mcp-endpoint' ? 'Copied' : 'Copy endpoint'}
                size="sm"
                onclick={() => copyAgentText(hostedMcpUrl, 'mcp-endpoint', 'MCP endpoint copied')}
              >
                <ConstellationIcon name={copiedKey === 'mcp-endpoint' ? 'check' : 'copy'} />
              </ConstellationIconButton>
            </div>
          </div>

          <section class="mcp-token-section" aria-label="MCP connections">
            {#if agentConnections.length}
              <div class="mcp-token-heading">
                <h2>MCP connections</h2>
                <ConstellationButton variant="secondary" size="sm" onclick={openAgentConnection}>
                  New connection token
                </ConstellationButton>
              </div>
            {/if}

            {#if mintedAgentToken?.token}
              <div class="mcp-token-reveal" aria-label="One-time MCP connection token">
                <ConstellationNotice
                  title="Copy this token before leaving"
                  description="Illo only shows the raw MCP token once."
                  tone="warning"
                  compact
                />
                <div class="mcp-token-secret">
                  <div>
                    <strong>{mintedAgentTokenConnection?.display_name || 'Personal agent'}</strong>
                    <code>{mintedAgentToken.token}</code>
                  </div>
                  <ConstellationIconButton
                    label={copiedKey === 'agent-token' ? 'Token copied' : 'Copy token'}
                    title={copiedKey === 'agent-token' ? 'Copied' : 'Copy token'}
                    size="sm"
                    onclick={() => copyAgentText(mintedAgentToken?.token || '', 'agent-token', 'Token copied')}
                  >
                    <ConstellationIcon name={copiedKey === 'agent-token' ? 'check' : 'copy'} />
                  </ConstellationIconButton>
                </div>
              </div>
            {/if}

            {#if agentConnections.length}
              <div class="mcp-token-list">
                {#each agentConnections as connection (connection.id)}
                  <div class="mcp-token-row">
                    <div class="mcp-token-row-main">
                      <div>
                        <strong>{connection.display_name}</strong>
                        <span>{mcpConnectionFacts(connection)}</span>
                      </div>
                      <div class="mcp-token-actions">
                        <ConstellationButton variant="quiet" size="sm" onclick={() => mintTokenForConnection(connection)}>
                          Mint token
                        </ConstellationButton>
                        <ConstellationButton
                          variant="quiet"
                          size="sm"
                          disabled={deletingAgentConnectionIds.includes(connection.id)}
                          onclick={() => removeAgentConnection(connection)}
                        >
                          Remove
                        </ConstellationButton>
                      </div>
                    </div>
                    <div class="mcp-token-metadata-list" aria-label={`Active tokens for ${connection.display_name}`}>
                      {#if (agentConnectionTokens[connection.id] || []).length}
                        {#each agentConnectionTokens[connection.id] || [] as token (token.id)}
                          <div class="mcp-token-metadata-row">
                            <div>
                              <strong>{token.name}</strong>
                              <span>{mcpConnectionTokenFacts(token)}</span>
                            </div>
                            <ConstellationButton
                              variant="quiet"
                              size="sm"
                              disabled={revokingAgentTokenIds.includes(token.id)}
                              onclick={() => revokeTokenForConnection(connection, token)}
                            >
                              Revoke
                            </ConstellationButton>
                          </div>
                        {/each}
                      {:else}
                        <span class="mcp-token-empty">No active tokens</span>
                      {/if}
                    </div>
                  </div>
                {/each}
              </div>
            {:else}
              <ConstellationEmptyState
                title="No MCP connections"
                description="Create a connection token so a personal agent can use this endpoint."
                size="sm"
                surface="plain"
              >
                {#snippet actions()}
                  <ConstellationButton variant="secondary" size="sm" onclick={openAgentConnection}>
                    New connection token
                  </ConstellationButton>
                {/snippet}
              </ConstellationEmptyState>
            {/if}
          </section>
        </div>
      {:else}
        <div
          class="vault-tab-panel inventory-pane"
          id="library-panel"
          role="tabpanel"
          aria-labelledby="library-tab"
        >
          <div class="vault-tab-toolbar inventory-tools">
            <ConstellationSearchField bind:value={filterText} placeholder="Search vault..." aria-label="Search vault" />
            <div class="vault-tab-actions">
              <ConstellationButton variant="secondary" size="sm" onclick={() => openCreate()}>
                Add secret
              </ConstellationButton>
            </div>
          </div>

          <div class="vault-list" aria-label="Vault entries">
            {#if loading}
              {#each Array(9) as _}
                <div class="vault-row-skeleton"></div>
              {/each}
            {:else if filteredRows.length === 0}
              <ConstellationEmptyState
                title="No matching vault entries"
                description="Try a different search or add a new secret."
                size="sm"
                surface="plain"
              />
            {:else}
              {#each filteredRows as row (row.id)}
                <article class="vault-item" class:is-expanded={selectedRowId === row.id}>
                    <button
                      type="button"
                      class="vault-row"
                      class:is-selected={selectedRowId === row.id}
                      onclick={() => selectRow(row)}
                    >
                      <span class="vault-row-main">
                        <strong>{rowTitle(row)}</strong>
                        <small>{rowDescription(row)}</small>
                      </span>
                      <span class="vault-row-side">
                        <ConstellationPill variant={rowStatusVariant(row)} leadingDot>{rowStatusLabel(row)}</ConstellationPill>
                        <small>{rowStatusDetail(row)}</small>
                      </span>
                    </button>

                    {#if selectedRowId === row.id}
                      <div class="vault-expanded">
                        {#if row.kind === 'pin'}
                          <div class="expanded-toolbar">
                            <div class="expanded-facts">
                              <span>setup needed</span>
                              <span>{secrets.length} secrets</span>
                              <span>{pendingAgentGrants.length} grants</span>
                            </div>
                            <div class="expanded-actions">
                              <ConstellationButton variant="secondary" size="sm" onclick={() => (showPinSetup = true)}>
                                Setup PIN
                              </ConstellationButton>
                            </div>
                          </div>
                          <details class="vault-region" open>
                            <summary>
                              <span>Why it matters</span>
                              <small>Vault unlock boundary</small>
                            </summary>
                            <p class="empty-inline">A PIN adds a second local unlock step before the browser can reveal or mutate secrets.</p>
                          </details>
                        {:else if row.kind === 'grant'}
                          <div class="expanded-toolbar">
                            <div class="expanded-facts">
                              <span>{row.grant.requested_by || 'agent'}</span>
                              <span>run {row.grant.run_id ?? 'unknown'}</span>
                              <span>{row.grant.read_count}/{row.grant.max_reads} reads</span>
                            </div>
                            <div class="expanded-actions">
                              <ConstellationButton variant="quiet" size="sm" onclick={() => denyAgentGrant(row.grant.id)}>
                                Deny
                              </ConstellationButton>
                              <ConstellationButton variant="secondary" size="sm" onclick={() => approveAgentGrant(row.grant.id)}>
                                Approve once
                              </ConstellationButton>
                            </div>
                          </div>
                          <details class="vault-region" open>
                            <summary>
                              <span>Request reason</span>
                              <small>{timeAgo(row.grant.requested_at)}</small>
                            </summary>
                            <p class="empty-inline">{row.grant.reason}</p>
                          </details>
                        {:else if row.kind === 'missing'}
                          <div class="expanded-toolbar">
                            <div class="expanded-facts">
                              <span>{row.missing.request_count} requests</span>
                              <span>{timeAgo(row.missing.last_requested)}</span>
                              <span>runtime key</span>
                            </div>
                            <div class="expanded-actions">
                              <ConstellationButton variant="secondary" size="sm" onclick={() => openCreate(row.missing.key_name)}>
                                Add secret
                              </ConstellationButton>
                            </div>
                          </div>
                          <details class="vault-region" open>
                            <summary>
                              <span>Missing key</span>
                              <small>Requested by recent work</small>
                            </summary>
                            <p class="empty-inline">Add this key when the active task should be able to use it through approved vault access.</p>
                          </details>
                        {:else if row.kind === 'dev_sample'}
                          <div class="expanded-toolbar">
                            <div class="expanded-facts">
                              <span>development only</span>
                              <span>{row.sample.category}</span>
                              <span>not stored</span>
                            </div>
                            <div class="expanded-actions">
                              <ConstellationButton
                                variant="secondary"
                                size="sm"
                                onclick={() =>
                                  openCreate(row.sample.key_name, {
                                    description: row.sample.description,
                                    category: row.sample.category,
                                  })}
                              >
                                Add real secret
                              </ConstellationButton>
                            </div>
                          </div>
                          <details class="vault-region" open>
                            <summary>
                              <span>Preview key</span>
                              <small>Empty dev library</small>
                            </summary>
                            <p class="empty-inline">
                              This sample appears only in development when the vault library has no entries.
                            </p>
                          </details>
                        {:else}
                          <div class="expanded-toolbar">
                            <div class="expanded-facts" aria-label="Secret facts">
                              <span>{row.secret.category || 'general'}</span>
                              <span>{secretAgeDays(row.secret.updated_at)} old</span>
                              <span>{row.secret.access_count || 0} reads</span>
                              <span>{accessLevelLabel(row.secret.agent_access_level)}</span>
                              <span>{secretGrantHistory(row.secret.key_name).length} grants</span>
                            </div>
                            <div class="expanded-actions">
                              <ConstellationButton
                                variant="quiet"
                                size="sm"
                                onclick={() => revealSecret(row.secret.key_name)}
                              >
                                {revealed[row.secret.key_name] ? 'Hide' : 'Reveal'}
                              </ConstellationButton>
                              <ConstellationButton
                                variant="quiet"
                                size="sm"
                                onclick={() => copySecret(row.secret.key_name)}
                              >
                                {copiedKey === row.secret.key_name ? 'Copied' : 'Copy'}
                              </ConstellationButton>
                              <ConstellationButton variant="secondary" size="sm" onclick={() => openEdit(row.secret)}>
                                Edit
                              </ConstellationButton>
                              <ConstellationButton variant="quiet" size="sm" onclick={() => openBind(row.secret)}>
                                Bind project
                              </ConstellationButton>
                              <ConstellationButton
                                variant="destructive"
                                size="sm"
                                onclick={() => deleteSecret(row.secret.key_name)}
                              >
                                Delete
                              </ConstellationButton>
                            </div>
                          </div>

                          {#if revealed[row.secret.key_name]}
                            <div class="vault-revealed minimal">
                              <code>{revealed[row.secret.key_name]}</code>
                            </div>
                          {/if}

                          <details class="vault-region" open>
                            <summary>
                              <span>Analytics</span>
                              <small>{timeAgo(row.secret.last_accessed_at)} / x{row.secret.access_count || 0}</small>
                            </summary>
                            <dl class="metadata-list">
                              <div><dt>Category</dt><dd>{row.secret.category || 'general'}</dd></div>
                              <div><dt>Access count</dt><dd>{row.secret.access_count || 0}</dd></div>
                              <div><dt>Last accessed</dt><dd>{timeAgo(row.secret.last_accessed_at)}</dd></div>
                              <div><dt>Created</dt><dd>{timeAgo(row.secret.created_at)}</dd></div>
                              <div><dt>Updated</dt><dd>{timeAgo(row.secret.updated_at)}</dd></div>
                              <div><dt>Rotation age</dt><dd>{secretAgeDays(row.secret.updated_at)}</dd></div>
                              <div><dt>Agent policy</dt><dd>{accessLevelLabel(row.secret.agent_access_level)}</dd></div>
                            </dl>
                          </details>

                          <details class="vault-region">
                            <summary>
                              <span>Agent access</span>
                              <small>{secretProjectBindings(row.secret.id).length} project bindings / {secretGrantHistory(row.secret.key_name).length} grants</small>
                            </summary>
                            {#if secretProjectBindings(row.secret.id).length}
                              <div class="advisory-list">
                                {#each secretProjectBindings(row.secret.id) as binding (binding.id)}
                                  <div class="advisory-row">
                                    <ConstellationPill variant="info">{binding.env_name}</ConstellationPill>
                                    <span>{binding.project_slug}</span>
                                    <button type="button" class="inline-action" onclick={() => deleteProjectBinding(binding.id)}>Remove</button>
                                  </div>
                                {/each}
                              </div>
                            {:else}
                              <p class="empty-inline">No project bindings for this key yet.</p>
                            {/if}
                            {#if secretGrantHistory(row.secret.key_name).length}
                              <div class="advisory-list">
                                {#each secretGrantHistory(row.secret.key_name).slice(0, 8) as grant (grant.id)}
                                  <div class="advisory-row">
                                    <ConstellationPill variant={grantPillVariant(grant.status)}>{grant.status}</ConstellationPill>
                                    <span>{grantDescription(grant)}</span>
                                  </div>
                                {/each}
                              </div>
                            {:else}
                              <p class="empty-inline">No decided grants for this key yet.</p>
                            {/if}
                          </details>

                          <details class="vault-region">
                            <summary>
                              <span>Vault readiness</span>
                              <small>{postureLabel}</small>
                            </summary>
                            <div class="advisory-list">
                              {#each postureItems as item}
                                <div class="advisory-row">
                                  <ConstellationPill variant={item.ok ? 'success' : 'warning'}>{item.label}</ConstellationPill>
                                  <span>{item.detail}</span>
                                </div>
                              {/each}
                            </div>
                          </details>
                        {/if}
                      </div>
                    {/if}
                </article>
              {/each}
            {/if}
          </div>
        </div>
      {/if}
    </section>
  </ConstellationPageFrame>
{/if}

<!-- Personal Agent Connection Modal -->
{#if showAgentConnectionModal}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={() => (showAgentConnectionModal = false)} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">Connect Personal Agent</span>
        <ConstellationIconButton label="Close" title="Close" size="sm" onclick={() => (showAgentConnectionModal = false)}>
          <ConstellationIcon name="x" />
        </ConstellationIconButton>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitAgentConnection(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="agent-name">Agent Name</label>
          <ConstellationTextInput id="agent-name" type="text" placeholder="Hermes" bind:value={agentFormDisplayName} required />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="agent-kind">Agent Type</label>
          <ConstellationSelect id="agent-kind" options={AGENT_KIND_SELECT_OPTIONS} bind:value={agentFormKind} />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="agent-mcp-url">MCP Endpoint</label>
          <ConstellationTextInput id="agent-mcp-url" type="text" value={hostedMcpUrl} readonly mono />
        </div>
        <ConstellationNotice
          title="Token appears once"
          description="The next screen gives you the bearer token and MCP client config."
          tone="info"
          compact
          className="agent-modal-notice"
        />
        <div class="modal-actions">
          <ConstellationButton variant="secondary" onclick={() => (showAgentConnectionModal = false)}>Cancel</ConstellationButton>
          <ConstellationButton type="submit" loading={agentConnectionSaving} loadingLabel="Creating">
            Create token
          </ConstellationButton>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Agent Grant Approval Modal -->
{#if approvalModalGrant}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={() => (approvalModalGrantId = null)} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal approval-modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">Approve Vault Access</span>
        <ConstellationIconButton label="Close" title="Close" size="sm" onclick={() => (approvalModalGrantId = null)}>
          <ConstellationIcon name="x" />
        </ConstellationIconButton>
      </div>

      <div class="approval-modal-body">
        <div class="approval-key-block">
          <span>Secret</span>
          <code>{approvalModalGrant.key_name}</code>
        </div>
        <div class="expanded-facts">
          <span>{approvalModalGrant.requested_by || 'agent'}</span>
          <span>run {approvalModalGrant.run_id ?? 'unknown'}</span>
          <span>requested {timeAgo(approvalModalGrant.requested_at)}</span>
        </div>
        <div class="vault-region approval-reason">
          <div class="approval-reason-header">
            <span>Request reason</span>
          </div>
          <p class="empty-inline">{approvalModalGrant.reason}</p>
        </div>
      </div>

      <div class="modal-actions">
        <ConstellationButton variant="secondary" onclick={() => denyAgentGrant(approvalModalGrant.id)}>
          Deny
        </ConstellationButton>
        <ConstellationButton onclick={() => approveAgentGrant(approvalModalGrant.id)}>
          Approve once
        </ConstellationButton>
      </div>
    </div>
  </div>
{/if}

<!-- Create Modal -->
{#if showCreateModal}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={() => (showCreateModal = false)} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">Add Secret</span>
        <ConstellationIconButton label="Close" title="Close" size="sm" onclick={() => (showCreateModal = false)}>
          <ConstellationIcon name="x" />
        </ConstellationIconButton>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitCreate(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-key">Key Name</label>
          <ConstellationTextInput id="vault-key" type="text" placeholder="e.g. OPENAI_API_KEY" bind:value={formKeyName} required mono />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-val">Value</label>
          <ConstellationTextInput
            id="vault-val"
            type={showPassword ? 'text' : 'password'}
            placeholder="Secret value"
            bind:value={formValue}
            required
            mono
            trailingInteractive
          >
            {#snippet trailingVisual()}
              <ConstellationIconButton
                label={showPassword ? 'Hide value' : 'Show value'}
                title={showPassword ? 'Hide value' : 'Show value'}
                size="sm"
                variant="quiet"
                onclick={(event) => {
                  event.preventDefault();
                  showPassword = !showPassword;
                }}
              >
                <ConstellationIcon name={showPassword ? 'eye-off' : 'eye'} />
              </ConstellationIconButton>
            {/snippet}
          </ConstellationTextInput>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-desc">Description</label>
          <ConstellationTextarea id="vault-desc" rows={2} placeholder="Optional description" bind:value={formDescription} />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-cat">Category</label>
          <ConstellationSelect id="vault-cat" options={CATEGORY_SELECT_OPTIONS} bind:value={formCategory} />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-agent-access">Agent Access</label>
          <ConstellationSelect id="vault-agent-access" options={ACCESS_LEVEL_SELECT_OPTIONS} bind:value={formAgentAccessLevel} />
        </div>
        <div class="modal-actions">
          <ConstellationButton variant="secondary" onclick={() => (showCreateModal = false)}>Cancel</ConstellationButton>
          <ConstellationButton type="submit" loading={formSaving} loadingLabel="Saving">Save</ConstellationButton>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Edit Modal -->
{#if showEditModal}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={() => (showEditModal = false)} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">Edit: {editKey}</span>
        <ConstellationIconButton label="Close" title="Close" size="sm" onclick={() => (showEditModal = false)}>
          <ConstellationIcon name="x" />
        </ConstellationIconButton>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitEdit(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-val">New Value (leave blank to keep)</label>
          <ConstellationTextInput
            id="edit-val"
            type={showEditPassword ? 'text' : 'password'}
            placeholder="New secret value (optional)"
            bind:value={editValue}
            mono
            trailingInteractive
          >
            {#snippet trailingVisual()}
              <ConstellationIconButton
                label={showEditPassword ? 'Hide value' : 'Show value'}
                title={showEditPassword ? 'Hide value' : 'Show value'}
                size="sm"
                variant="quiet"
                onclick={(event) => {
                  event.preventDefault();
                  showEditPassword = !showEditPassword;
                }}
              >
                <ConstellationIcon name={showEditPassword ? 'eye-off' : 'eye'} />
              </ConstellationIconButton>
            {/snippet}
          </ConstellationTextInput>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-desc">Description</label>
          <ConstellationTextarea id="edit-desc" rows={2} bind:value={editDescription} />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-cat">Category</label>
          <ConstellationSelect id="edit-cat" options={CATEGORY_SELECT_OPTIONS} bind:value={editCategory} />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-agent-access">Agent Access</label>
          <ConstellationSelect id="edit-agent-access" options={ACCESS_LEVEL_SELECT_OPTIONS} bind:value={editAgentAccessLevel} />
        </div>
        <div class="modal-actions">
          <ConstellationButton variant="secondary" onclick={() => (showEditModal = false)}>Cancel</ConstellationButton>
          <ConstellationButton type="submit" loading={editSaving} loadingLabel="Saving">Update</ConstellationButton>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Project Binding Modal -->
{#if showBindModal}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={() => (showBindModal = false)} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">Bind Project: {bindSecretName}</span>
        <ConstellationIconButton label="Close" title="Close" size="sm" onclick={() => (showBindModal = false)}>
          <ConstellationIcon name="x" />
        </ConstellationIconButton>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitProjectBinding(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="bind-project">Project</label>
          <ConstellationTextInput id="bind-project" type="text" placeholder="e.g. example-repo" bind:value={bindProjectSlug} required />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="bind-env">Env Name</label>
          <ConstellationTextInput id="bind-env" type="text" placeholder="GITHUB_TOKEN" bind:value={bindEnvName} required mono />
        </div>
        <div class="modal-actions">
          <ConstellationButton variant="secondary" onclick={() => (showBindModal = false)}>Cancel</ConstellationButton>
          <ConstellationButton type="submit" loading={bindSaving} loadingLabel="Binding">Bind</ConstellationButton>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- PIN Setup Modal -->
{#if showPinSetup}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={() => (showPinSetup = false)} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">{hasPin ? 'Change PIN' : 'Setup PIN'}</span>
        <ConstellationIconButton label="Close" title="Close" size="sm" onclick={() => (showPinSetup = false)}>
          <ConstellationIcon name="x" />
        </ConstellationIconButton>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); setupPin(); }}>
        {#if hasPin}
          <div class="form-field" style="margin-bottom: var(--sp-3)">
            <label class="form-label" for="cur-pin">Current PIN</label>
            <ConstellationTextInput id="cur-pin" type="password" placeholder="Current PIN" bind:value={currentPin} required mono />
          </div>
        {/if}
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="new-pin">New PIN (min 4 chars)</label>
          <ConstellationTextInput id="new-pin" type="password" placeholder="New PIN" bind:value={newPin} minlength={4} required mono />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="confirm-pin">Confirm PIN</label>
          <ConstellationTextInput id="confirm-pin" type="password" placeholder="Confirm PIN" bind:value={confirmPin} minlength={4} required mono />
        </div>
        <div class="modal-actions">
          <ConstellationButton variant="secondary" onclick={() => (showPinSetup = false)}>Cancel</ConstellationButton>
          <ConstellationButton type="submit" loading={pinSaving} loadingLabel="Saving">Set PIN</ConstellationButton>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .vault-constellation-lock-panel {
    width: min(100%, 360px);
    margin: 0 auto;
  }

  .vault-constellation-lock-shell {
    display: grid;
    gap: 16px;
    justify-items: center;
    text-align: center;
  }

  .vault-constellation-lock-mark {
    display: grid;
    place-items: center;
    width: 42px;
    height: 42px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 999px;
    background: var(--constellation-surface-nested-background);
    color: var(--constellation-color-text-primary);
  }

  .vault-constellation-lock-copy {
    display: grid;
    gap: 8px;
    max-width: 36ch;
  }

  .vault-constellation-lock-title {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 18px;
    font-weight: 560;
    line-height: 1.3;
  }

  .vault-constellation-lock-subtitle {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 13px;
    line-height: 1.55;
  }

  .vault-constellation-lock-form {
    display: grid;
    gap: 12px;
    width: min(100%, 280px);
  }

  :global(.vault-constellation-lock-input) {
    min-height: 40px;
    width: 100%;
  }

  :global(.vault-constellation-lock-input .constellation-text-input-control) {
    text-align: left;
    font-size: 14px;
  }

  :global(.vault-constellation-lock-input .constellation-text-input-trailing) {
    color: var(--constellation-color-text-secondary);
  }

  :global(.vault-constellation-lock-reveal) {
    width: 24px;
    height: 24px;
  }

  .vault-constellation-lock-attempts {
    margin: 0;
    color: var(--constellation-control-pill-danger-text);
    font-size: 11px;
    line-height: 1.45;
  }

  :global(.vault-page) {
    gap: 14px;
  }

  .workspace {
    display: grid;
    grid-template-columns: 1fr;
    align-items: start;
    min-height: 0;
  }

  .vault-tab-panel {
    display: grid;
    gap: 18px;
    min-width: 0;
    min-height: 0;
  }

  .vault-tab-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
    padding: 0 0 16px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .mcp-tab-toolbar {
    display: grid;
    min-width: 0;
  }

  .inventory-tools :global(.constellation-search-field) {
    flex: 1 1 260px;
  }

  .vault-tab-actions {
    display: flex;
    flex: 0 0 auto;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
  }

  .mcp-endpoint {
    display: flex;
    align-items: center;
    gap: 12px;
    justify-content: flex-start;
    min-width: 0;
    min-height: 34px;
  }

  .mcp-endpoint-copy {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: baseline;
    gap: 10px;
    min-width: 0;
  }

  .mcp-endpoint-label {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    letter-spacing: 0.14em;
    line-height: 1;
    text-transform: uppercase;
  }

  .mcp-endpoint-value {
    min-width: 0;
    overflow: hidden;
    color: var(--content-code-text);
    background: var(--content-code-background);
    border: 1px solid var(--content-code-border);
    border-radius: var(--radius-xs);
    font-family: var(--constellation-font-mono);
    font-size: 12px;
    line-height: 1.45;
    padding: 1px 4px;
    text-overflow: ellipsis;
    white-space: nowrap;
    width: fit-content;
    max-width: 100%;
  }

  .vault-list {
    display: grid;
    align-content: start;
    gap: 6px;
    min-height: 0;
    overflow: visible;
    padding: 0;
  }

  .vault-item {
    display: grid;
    min-width: 0;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
    overflow: hidden;
  }

  .vault-item.is-expanded {
    border-color: var(--constellation-control-focus-ring);
  }

  .vault-row,
  .vault-row-skeleton {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    min-height: 58px;
    width: 100%;
    min-width: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    color: var(--constellation-color-text-primary);
    padding: 10px 12px;
    text-align: left;
  }

  .vault-row {
    cursor: pointer;
    transition:
      border-color var(--constellation-motion-settle-duration) ease,
      background-color var(--constellation-motion-settle-duration) ease,
      transform var(--constellation-motion-hover-duration) ease;
  }

  .vault-row:hover,
  .vault-row.is-selected {
    background: var(--constellation-control-button-secondary-background-hover);
  }

  .vault-row-skeleton {
    background:
      linear-gradient(90deg, transparent, var(--constellation-skeleton-row-shimmer), transparent),
      var(--constellation-skeleton-row-background);
    background-size: 200% 100%;
    animation: vault-pulse 1.4s ease-in-out infinite;
  }

  .vault-row-main,
  .vault-row-side {
    display: grid;
    min-width: 0;
    gap: 7px;
  }

  .vault-row-main strong,
  .vault-row-main small,
  .vault-row-side small {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .vault-row-main strong {
    font-size: 13px;
    font-weight: 560;
    letter-spacing: 0;
  }

  .vault-row-main small,
  .vault-row-side small,
  .empty-inline {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.5;
  }

  .vault-row-side {
    justify-items: end;
  }

  .vault-expanded {
    display: grid;
    grid-template-columns: 1fr;
    gap: 10px;
    padding: 0 12px 12px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .expanded-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding-top: 10px;
  }

  .expanded-facts,
  .expanded-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .expanded-facts span {
    padding: 2px 7px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    border-radius: 999px;
    background: var(--constellation-control-button-secondary-background);
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0;
  }

  .vault-region {
    display: grid;
    min-width: 0;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-surface-nested-background) 82%, transparent);
  }

  .vault-region summary {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    min-height: 42px;
    padding: 0 12px;
    color: var(--constellation-color-text-primary);
    cursor: pointer;
    list-style: none;
  }

  .vault-region summary::-webkit-details-marker {
    display: none;
  }

  .vault-region summary span {
    font-size: 13px;
    font-weight: 560;
  }

  .vault-region summary small {
    min-width: 0;
    overflow: hidden;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    text-align: right;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .vault-region[open] summary {
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .vault-region > :not(summary) {
    margin: 12px;
  }

  .vault-revealed {
    padding: 10px 12px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-surface-nested-background) 72%, transparent);
    animation: fadeIn var(--duration-fast) var(--ease-out);
  }

  .vault-revealed code {
    display: block;
    min-width: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-mono);
    font-size: 12px;
    line-height: 1.5;
  }

  .metadata-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin: 0;
    padding: 0;
  }

  .metadata-list div {
    display: grid;
    gap: 4px;
    min-width: 0;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .metadata-list dt {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    text-transform: uppercase;
    letter-spacing: 0.14em;
  }

  .metadata-list dd {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    line-height: 1.45;
  }

  .advisory-list {
    display: grid;
    gap: 8px;
  }

  .advisory-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    min-width: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .advisory-row span {
    min-width: 0;
    overflow-wrap: anywhere;
  }

  .mcp-token-section {
    display: grid;
    gap: 12px;
    min-width: 0;
  }

  .mcp-token-heading {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .mcp-token-heading h2 {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-size: 18px;
    font-weight: 560;
    letter-spacing: 0;
    line-height: 1.25;
  }

  .mcp-token-reveal {
    display: grid;
    gap: 10px;
    min-width: 0;
  }

  .mcp-token-secret {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 12px;
    min-width: 0;
    padding: 10px 12px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
  }

  .mcp-token-secret div,
  .mcp-token-row-main div,
  .mcp-token-metadata-row div {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .mcp-token-secret strong,
  .mcp-token-row-main strong,
  .mcp-token-metadata-row strong {
    min-width: 0;
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    font-weight: 560;
    line-height: 1.35;
  }

  .mcp-token-secret code,
  .mcp-token-row-main span,
  .mcp-token-metadata-row span,
  .mcp-token-empty {
    min-width: 0;
    overflow: hidden;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.45;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .mcp-token-secret code {
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-mono);
  }

  .mcp-token-list {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .mcp-token-row {
    display: grid;
    gap: 12px;
    min-width: 0;
    padding: 10px 12px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
  }

  .mcp-token-row-main,
  .mcp-token-metadata-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .mcp-token-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
    min-width: 0;
  }

  .mcp-token-metadata-list {
    display: grid;
    gap: 8px;
    min-width: 0;
    padding-top: 10px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .mcp-token-row-main strong,
  .mcp-token-metadata-row strong {
    overflow: hidden;
    color: var(--constellation-color-text-primary);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  :global(.agent-modal-notice) {
    margin-bottom: var(--sp-3);
  }

  .approval-modal {
    max-width: min(100%, 440px);
  }

  .approval-modal-body {
    display: grid;
    gap: 12px;
    min-width: 0;
  }

  .approval-key-block {
    display: grid;
    gap: 6px;
    min-width: 0;
    padding: 10px 12px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-nested-background);
  }

  .approval-key-block span,
  .approval-reason-header {
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 600;
    letter-spacing: 0.14em;
    line-height: 1;
    text-transform: uppercase;
  }

  .approval-key-block code {
    min-width: 0;
    overflow: hidden;
    color: var(--content-code-text);
    font-family: var(--constellation-font-mono);
    font-size: 13px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .approval-reason {
    gap: 8px;
    padding: 10px 12px;
  }

  .inline-action {
    border: 0;
    background: transparent;
    color: var(--constellation-color-text-secondary);
    cursor: pointer;
    font-size: 12px;
    font-family: inherit;
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .inline-action:hover {
    color: var(--constellation-color-text-primary);
  }

  .empty-inline {
    margin: 0;
  }

  @keyframes vault-pulse {
    from {
      background-position: 200% 0;
    }
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 760px) {
    .vault-row {
      grid-template-columns: 1fr;
    }

    .vault-row-side {
      justify-items: start;
    }

    .vault-region summary {
      grid-template-columns: 1fr;
      gap: 4px;
      padding: 9px 12px;
    }

    .vault-region summary small {
      text-align: left;
    }

    .metadata-list {
      grid-template-columns: 1fr;
    }

    .vault-tab-toolbar {
      align-items: stretch;
    }

    .vault-tab-actions {
      justify-content: flex-start;
    }

    .mcp-endpoint {
      grid-template-columns: 1fr;
      gap: 5px;
    }

    .mcp-endpoint-copy,
    .mcp-token-secret,
    .mcp-token-row,
    .mcp-token-row-main,
    .mcp-token-metadata-row {
      grid-template-columns: 1fr;
    }

    .mcp-token-row,
    .mcp-token-row-main,
    .mcp-token-metadata-row {
      align-items: start;
    }

    .mcp-token-actions {
      justify-content: flex-start;
    }
  }
</style>
