<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { dev } from '$app/environment';
  import { page } from '$app/stores';
  import { api } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationEmptyState,
    ConstellationPageFrame,
    ConstellationPanel,
    ConstellationPill,
    ConstellationSearchField,
    ConstellationSectionHeader,
    ConstellationSegmentedToggle,
  } from '$lib/components/constellation';
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

  interface OrgUser {
    id: string;
    name: string;
    email: string;
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

  type FilterMode = 'all' | 'attention' | 'grants';
  type PillTone = 'muted' | 'warning' | 'success' | 'danger' | 'info';
  type VaultRow =
    | { id: string; kind: 'secret'; secret: Secret }
    | { id: string; kind: 'grant'; grant: AgentGrant }
    | { id: string; kind: 'missing'; missing: MissingSecret }
    | { id: string; kind: 'pin' };

  const CATEGORIES = ['general', 'api', 'aws', 'auth', 'analytics', 'database', 'messaging', 'monitoring', 'payments', 'service'];
  const FILTER_OPTIONS: Array<{ key: FilterMode; label: string }> = [
    { key: 'all', label: 'All' },
    { key: 'attention', label: 'Needs Work' },
    { key: 'grants', label: 'Grants' },
  ];
  const ACCESS_LEVELS = [
    { key: 'ask', label: 'Ask Each Run' },
    { key: 'available', label: 'Agent Available' },
    { key: 'manual', label: 'Manual Only' },
  ];

  let secrets = $state<Secret[]>([]);
  let missing = $state<MissingSecret[]>([]);
  let agentGrants = $state<AgentGrant[]>([]);
  let projectBindings = $state<ProjectBinding[]>([]);
  let loading = $state(true);
  let filterText = $state('');
  let filterMode = $state<FilterMode>('all');
  let selectedRowId = $state<string | null>(null);

  // PIN state
  let hasPin = $state(false);
  let vaultLocked = $state(false);
  let vaultToken = $state<string | null>(null);
  let pinInput = $state('');
  let pinAttempts = $state(0);
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

  // Edit form
  let showEditModal = $state(false);
  let editKey = $state('');
  let editValue = $state('');
  let editDescription = $state('');
  let editCategory = $state('general');
  let editAgentAccessLevel = $state<'available' | 'ask' | 'manual'>('ask');
  let editSaving = $state(false);
  let showEditPassword = $state(false);

  // Share state
  let showShareModal = $state(false);
  let shareSecretId = $state(0);
  let shareSecretName = $state('');
  let orgUsers = $state<OrgUser[]>([]);
  let shareUserId = $state('');
  let shareSaving = $state(false);

  // Project binding state
  let showBindModal = $state(false);
  let bindSecretId = $state(0);
  let bindSecretName = $state('');
  let bindProjectSlug = $state('');
  let bindEnvName = $state('');
  let bindSaving = $state(false);

  // Reveal state
  let revealed = $state<Record<string, string>>({});
  let revealTimers: Record<string, ReturnType<typeof setTimeout>> = {};

  // Clipboard feedback
  let copiedKey = $state('');

  const isVaultPreview = $derived(dev && $page.url.searchParams.get('preview') === '1');
  const categoryCount = $derived.by(
    () => new Set(secrets.map((secret) => secret.category || 'general')).size,
  );
  const pendingAgentGrants = $derived.by(() => agentGrants.filter((grant) => grant.status === 'pending'));
  const grantHistory = $derived.by(() => agentGrants.filter((grant) => grant.status !== 'pending'));
  const staleSecrets = $derived.by(() => secrets.filter((secret) => secretAgeNumber(secret.updated_at) >= 180));
  const attentionCount = $derived.by(
    () => pendingAgentGrants.length + missing.length + staleSecrets.length + (hasPin ? 0 : 1),
  );
  const vaultRows = $derived.by<VaultRow[]>(() => [
    ...(!hasPin ? [{ id: 'pin:setup', kind: 'pin' as const }] : []),
    ...pendingAgentGrants.map((grant) => ({ id: `grant:${grant.id}`, kind: 'grant' as const, grant })),
    ...missing.map((item) => ({ id: `missing:${item.key_name}`, kind: 'missing' as const, missing: item })),
    ...secrets.map((secret) => ({ id: `secret:${secret.key_name}`, kind: 'secret' as const, secret })),
  ]);
  const filteredRows = $derived.by(() => {
    const needle = filterText.trim().toLowerCase();
    return vaultRows.filter((row) => {
      if (filterMode === 'attention' && !rowNeedsAttention(row)) return false;
      if (filterMode === 'grants' && row.kind !== 'grant') return false;
      if (!needle) return true;
      return rowSearchText(row).includes(needle);
    });
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

  async function checkPin() {
    try {
      const status = await api.pinStatus();
      hasPin = status.has_pin;
      vaultLocked = hasPin;
    } catch {
      // PIN check failed — assume no PIN
    }
  }

  async function unlockVault() {
    if (!pinInput) return;
    try {
      const unlocked = await api.vaultUnlock(pinInput);
      vaultToken = unlocked.token;
      vaultLocked = false;
      pinInput = '';
      pinAttempts = 0;
      await loadData();
    } catch (err: any) {
      pinAttempts++;
      ui.toast(err?.detail || `Incorrect PIN (attempt ${pinAttempts})`, 'error');
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
      const unlocked = await api.vaultUnlock(newPin);
      vaultToken = unlocked.token;
      hasPin = true;
      vaultLocked = false;
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
    vaultLocked = true;
    clearRevealedSecrets();
    if (showToast) {
      ui.toast('Vault locked. Unlock to continue.', 'error');
    }
  }

  function handleVaultError(err: any, fallback: string) {
    if (err?.status === 423) {
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

      const [missingResult, grantsResult, bindingsResult] = await Promise.allSettled([
        api.missingSecrets(vaultToken),
        api.vaultAgentGrants(vaultToken, 'pending,approved,used,denied'),
        api.vaultProjectBindings(vaultToken),
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

  function selectRow(row: VaultRow) {
    selectedRowId = selectedRowId === row.id ? null : row.id;
  }

  function setFilter(key: string) {
    if (key === 'all' || key === 'attention' || key === 'grants') {
      filterMode = key;
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

  function rowNeedsAttention(row: VaultRow): boolean {
    if (row.kind === 'pin') return true;
    if (row.kind === 'grant') return row.grant.status === 'pending';
    if (row.kind === 'missing') return true;
    return secretAgeNumber(row.secret.updated_at) >= 180;
  }

  function rowTitle(row: VaultRow): string {
    if (row.kind === 'pin') return 'PIN not configured';
    if (row.kind === 'grant') return row.grant.key_name;
    if (row.kind === 'missing') return row.missing.key_name;
    return row.secret.key_name;
  }

  function rowDescription(row: VaultRow): string {
    if (row.kind === 'pin') return 'Set a lock before storing production credentials.';
    if (row.kind === 'grant') return row.grant.reason || `${row.grant.requested_by || 'agent'} requested access.`;
    if (row.kind === 'missing') return `Requested ${row.missing.request_count}x · last seen ${timeAgo(row.missing.last_requested)}`;
    return row.secret.description || row.secret.category || 'No description yet.';
  }

  function rowStatusLabel(row: VaultRow): string {
    if (row.kind === 'pin') return 'PIN';
    if (row.kind === 'grant') return row.grant.status;
    if (row.kind === 'missing') return 'Missing';
    return accessLevelLabel(row.secret.agent_access_level);
  }

  function rowStatusDetail(row: VaultRow): string {
    if (row.kind === 'pin') return 'Vault can still be opened by account session.';
    if (row.kind === 'grant') return `${row.grant.requested_by || 'agent'} · run ${row.grant.run_id ?? 'unknown'}`;
    if (row.kind === 'missing') return 'Runtime requested this key.';
    return `${row.secret.category || 'general'} · ${secretProjectBindings(row.secret.id).length} project bindings · x${row.secret.access_count || 0}`;
  }

  function rowStatusVariant(row: VaultRow): PillTone {
    if (row.kind === 'pin') return 'warning';
    if (row.kind === 'grant') return grantPillVariant(row.grant.status);
    if (row.kind === 'missing') return 'warning';
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
    if (initialCreatePrefillApplied || vaultLocked) return;
    const params = $page.url.searchParams;
    const keyName = (params.get('add_secret') || params.get('key_name') || '').trim();
    if (!keyName) return;
    initialCreatePrefillApplied = true;
    openCreate(keyName, {
      description: params.get('description'),
      category: params.get('category'),
    });
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
      ui.toast('Preview secret added', 'success');
      return;
    }
    formSaving = true;
    try {
      await api.createSecret({
        key_name: formKeyName.trim(),
        value: formValue,
        description: formDescription.trim(),
        category: formCategory,
        agent_access_level: formAgentAccessLevel,
      }, vaultToken);
      ui.toast('Secret created', 'success');
      showCreateModal = false;
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

  async function openShare(secret: Secret) {
    shareSecretId = secret.id;
    shareSecretName = secret.key_name;
    shareUserId = '';
    showShareModal = true;
    if (isVaultPreview) {
      orgUsers = [
        { id: 'preview-user-1', name: 'Alex', email: 'alex@example.test' },
        { id: 'preview-user-2', name: 'Deploy Agent', email: 'deploy-agent@example.test' },
      ];
      return;
    }
    try {
      orgUsers = await api.vaultOrgUsers(vaultToken);
    } catch (err: any) {
      if (err?.status === 423) {
        markVaultLocked(true);
      }
      orgUsers = [];
    }
  }

  async function submitShare() {
    if (!shareUserId) {
      ui.toast('Select a user to share with', 'error');
      return;
    }
    if (isVaultPreview) {
      showShareModal = false;
      ui.toast(`Preview share prepared for "${shareSecretName}"`, 'success');
      return;
    }
    shareSaving = true;
    try {
      await api.vaultShare(shareSecretId, { shared_with_user_id: shareUserId }, vaultToken);
      ui.toast(`Shared "${shareSecretName}" successfully`, 'success');
      showShareModal = false;
    } catch (err: any) {
      handleVaultError(err, 'Share failed');
    } finally {
      shareSaving = false;
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

  async function approveAgentGrant(grantId: number) {
    if (isVaultPreview) {
      agentGrants = agentGrants.map((grant) =>
        grant.id === grantId ? { ...grant, status: 'approved', expires_at: previewIso(-1), max_reads: 1 } : grant,
      );
      ui.toast('Preview grant approved', 'success');
      return;
    }
    try {
      await api.vaultApproveGrant(grantId, { ttl_minutes: 15, max_reads: 1 }, vaultToken);
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
      ui.toast('Preview grant denied', 'success');
      return;
    }
    try {
      await api.vaultDenyGrant(grantId, vaultToken);
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
  >
    <div class="vault-constellation-lock-panel">
      <ConstellationPanel tone="warning">
        <div class="vault-constellation-lock-shell">
          <div class="vault-constellation-lock-icon">LOCK</div>
          <div class="vault-constellation-lock-copy">
            <h2 class="vault-constellation-lock-title">Vault locked</h2>
            <p class="vault-constellation-lock-subtitle">Enter your PIN to unlock protected secrets.</p>
          </div>
          <form class="vault-constellation-lock-form" onsubmit={(e) => { e.preventDefault(); unlockVault(); }}>
            <!-- svelte-ignore a11y_autofocus -->
            <input
              type="password"
              class="input lock-input vault-constellation-lock-input"
              placeholder="Enter PIN"
              bind:value={pinInput}
              maxlength="32"
              autofocus
            />
            {#if pinAttempts > 0}
              <p class="vault-constellation-lock-attempts">
                {pinAttempts} failed attempt{pinAttempts !== 1 ? 's' : ''}
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
    subtitle={loading ? 'Loading protected secrets, missing keys, and lock state.' : `${secrets.length} secrets · ${categoryCount} categories tracked.`}
    contentClassName="vault-page"
  >
    {#snippet actions()}
      {#if hasPin}
        <ConstellationButton variant="quiet" size="sm" onclick={lockVault}>Lock vault</ConstellationButton>
      {/if}
      <ConstellationButton variant="quiet" size="sm" onclick={() => (showPinSetup = true)}>
        {hasPin ? 'Change PIN' : 'Setup PIN'}
      </ConstellationButton>
      <ConstellationButton variant="secondary" size="sm" onclick={() => openCreate()}>
        Add secret
      </ConstellationButton>
    {/snippet}

    <section class="workspace">
      <ConstellationPanel className="inventory-panel" padding="none" ariaLabel="Vault inventory">
        {#snippet header()}
          <ConstellationSectionHeader
            eyebrow="Library"
            title={loading ? 'Loading' : `${filteredRows.length} visible`}
            description={loading
              ? 'Checking lock state and protected inventory.'
              : `${attentionCount} signal${attentionCount === 1 ? '' : 's'} / ${categoryCount} categor${categoryCount === 1 ? 'y' : 'ies'} / ${grantHistory.length} grant decision${grantHistory.length === 1 ? '' : 's'}`}
            size="sm"
          />
        {/snippet}

        <div class="inventory-tools">
          <ConstellationSearchField bind:value={filterText} placeholder="Search vault..." aria-label="Search vault" />
          <ConstellationSegmentedToggle
            options={FILTER_OPTIONS}
            activeKey={filterMode}
            onActiveKeyChange={setFilter}
            ariaLabel="Vault filter"
          />
        </div>

        <div class="vault-list" aria-label="Vault entries">
          {#if loading}
            {#each Array(9) as _}
              <div class="vault-row-skeleton"></div>
            {/each}
          {:else if filteredRows.length === 0}
            <ConstellationEmptyState
              title="No matching vault entries"
              description="The active filters returned an empty vault view."
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
                            <ConstellationButton variant="quiet" size="sm" onclick={() => openShare(row.secret)}>
                              Share
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
      </ConstellationPanel>
    </section>
  </ConstellationPageFrame>
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
        <button class="modal-close" onclick={() => (showCreateModal = false)}>&times;</button>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitCreate(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-key">Key Name</label>
          <input id="vault-key" class="input" type="text" placeholder="e.g. OPENAI_API_KEY" bind:value={formKeyName} required />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-val">Value</label>
          <div style="position:relative;">
            <input
              id="vault-val"
              class="input"
              type={showPassword ? 'text' : 'password'}
              placeholder="Secret value"
              bind:value={formValue}
              required
            />
            <button
              type="button"
              class="vault-toggle-vis"
              onclick={() => (showPassword = !showPassword)}
            >{showPassword ? 'Hide' : 'Show'}</button>
          </div>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-desc">Description</label>
          <textarea id="vault-desc" class="input" rows="2" placeholder="Optional description" bind:value={formDescription}></textarea>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-cat">Category</label>
          <select id="vault-cat" class="tier-select" bind:value={formCategory}>
            {#each CATEGORIES as cat}
              <option value={cat}>{cat}</option>
            {/each}
          </select>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="vault-agent-access">Agent Access</label>
          <select id="vault-agent-access" class="tier-select" bind:value={formAgentAccessLevel}>
            {#each ACCESS_LEVELS as level}
              <option value={level.key}>{level.label}</option>
            {/each}
          </select>
        </div>
        <div class="modal-actions">
          <button type="button" class="btn" onclick={() => (showCreateModal = false)}>Cancel</button>
          <button type="submit" class="btn btn-primary" disabled={formSaving}>
            {formSaving ? 'Saving...' : 'Save'}
          </button>
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
        <button class="modal-close" onclick={() => (showEditModal = false)}>&times;</button>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitEdit(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-val">New Value (leave blank to keep)</label>
          <div style="position:relative;">
            <input
              id="edit-val"
              class="input"
              type={showEditPassword ? 'text' : 'password'}
              placeholder="New secret value (optional)"
              bind:value={editValue}
            />
            <button
              type="button"
              class="vault-toggle-vis"
              onclick={() => (showEditPassword = !showEditPassword)}
            >{showEditPassword ? 'Hide' : 'Show'}</button>
          </div>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-desc">Description</label>
          <textarea id="edit-desc" class="input" rows="2" bind:value={editDescription}></textarea>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-cat">Category</label>
          <select id="edit-cat" class="tier-select" bind:value={editCategory}>
            {#each CATEGORIES as cat}
              <option value={cat}>{cat}</option>
            {/each}
          </select>
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="edit-agent-access">Agent Access</label>
          <select id="edit-agent-access" class="tier-select" bind:value={editAgentAccessLevel}>
            {#each ACCESS_LEVELS as level}
              <option value={level.key}>{level.label}</option>
            {/each}
          </select>
        </div>
        <div class="modal-actions">
          <button type="button" class="btn" onclick={() => (showEditModal = false)}>Cancel</button>
          <button type="submit" class="btn btn-primary" disabled={editSaving}>
            {editSaving ? 'Saving...' : 'Update'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<!-- Share Modal -->
{#if showShareModal}
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="modal-overlay" onclick={() => (showShareModal = false)} role="dialog" aria-modal="true" tabindex="-1">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <span class="modal-title">Share: {shareSecretName}</span>
        <button class="modal-close" onclick={() => (showShareModal = false)}>&times;</button>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitShare(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="share-user">Share with</label>
          {#if orgUsers.length === 0}
            <p class="share-empty">No other team members available</p>
          {:else}
            <select id="share-user" class="tier-select" bind:value={shareUserId}>
              <option value="">Select a teammate...</option>
              {#each orgUsers as u}
                <option value={u.id}>{u.name} ({u.email})</option>
              {/each}
            </select>
          {/if}
        </div>
        <div class="modal-actions">
          <button type="button" class="btn" onclick={() => (showShareModal = false)}>Cancel</button>
          <button type="submit" class="btn btn-primary" disabled={shareSaving || !shareUserId}>
            {shareSaving ? 'Sharing...' : 'Share'}
          </button>
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
        <button class="modal-close" onclick={() => (showBindModal = false)}>&times;</button>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); submitProjectBinding(); }}>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="bind-project">Project</label>
          <input id="bind-project" class="input" type="text" placeholder="e.g. example-repo" bind:value={bindProjectSlug} required />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="bind-env">Env Name</label>
          <input id="bind-env" class="input" type="text" placeholder="GITHUB_TOKEN" bind:value={bindEnvName} required />
        </div>
        <div class="modal-actions">
          <button type="button" class="btn" onclick={() => (showBindModal = false)}>Cancel</button>
          <button type="submit" class="btn btn-primary" disabled={bindSaving}>
            {bindSaving ? 'Binding...' : 'Bind'}
          </button>
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
        <button class="modal-close" onclick={() => (showPinSetup = false)}>&times;</button>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); setupPin(); }}>
        {#if hasPin}
          <div class="form-field" style="margin-bottom: var(--sp-3)">
            <label class="form-label" for="cur-pin">Current PIN</label>
            <input id="cur-pin" class="input" type="password" placeholder="Current PIN" bind:value={currentPin} required />
          </div>
        {/if}
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="new-pin">New PIN (min 4 chars)</label>
          <input id="new-pin" class="input" type="password" placeholder="New PIN" bind:value={newPin} minlength="4" required />
        </div>
        <div class="form-field" style="margin-bottom: var(--sp-3)">
          <label class="form-label" for="confirm-pin">Confirm PIN</label>
          <input id="confirm-pin" class="input" type="password" placeholder="Confirm PIN" bind:value={confirmPin} minlength="4" required />
        </div>
        <div class="modal-actions">
          <button type="button" class="btn" onclick={() => (showPinSetup = false)}>Cancel</button>
          <button type="submit" class="btn btn-primary" disabled={pinSaving}>
            {pinSaving ? 'Saving...' : 'Set PIN'}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}

<style>
  .lock-input {
    text-align: center;
    font-size: var(--text-lg);
    letter-spacing: 4px;
    margin-bottom: var(--sp-3);
  }

  .vault-constellation-lock-panel {
    max-width: 520px;
    margin: 0 auto;
  }

  .vault-constellation-lock-shell {
    display: grid;
    gap: 18px;
    justify-items: center;
    text-align: center;
  }

  .vault-constellation-lock-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 78px;
    min-height: 32px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid rgba(213, 161, 77, 0.24);
    background: rgba(213, 161, 77, 0.12);
    color: rgba(250, 231, 188, 0.94);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .vault-constellation-lock-copy {
    display: grid;
    gap: 8px;
    max-width: 36ch;
  }

  .vault-constellation-lock-title {
    margin: 0;
    color: rgba(255, 255, 255, 0.96);
    font-family: var(--constellation-font-sans);
    font-size: 18px;
    font-weight: 560;
    line-height: 1.3;
  }

  .vault-constellation-lock-subtitle {
    margin: 0;
    color: rgba(240, 240, 250, 0.56);
    font-size: 13px;
    line-height: 1.55;
  }

  .vault-constellation-lock-form {
    display: grid;
    gap: 12px;
    width: min(100%, 280px);
  }

  .vault-constellation-lock-input {
    width: 100%;
  }

  .vault-constellation-lock-attempts {
    margin: 0;
    color: rgba(255, 195, 205, 0.9);
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
    gap: 14px;
    min-height: 0;
  }

  :global(.inventory-panel .constellation-panel-header) {
    padding: 18px 18px 16px;
  }

  :global(.inventory-panel .constellation-panel-content) {
    display: grid;
    gap: 0;
    min-height: 0;
    overflow: visible;
  }

  .inventory-tools {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 12px;
    padding: 16px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .inventory-tools :global(.constellation-search-field) {
    flex: 1 1 260px;
  }

  .vault-list {
    display: grid;
    align-content: start;
    gap: 6px;
    min-height: 0;
    overflow: visible;
    padding: 0 14px 14px;
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
      linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.06), transparent),
      var(--constellation-surface-nested-background);
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

  /* Form elements */
  .vault-toggle-vis {
    position: absolute;
    right: 8px;
    top: 50%;
    transform: translateY(-50%);
    background: none;
    border: none;
    color: var(--text-3);
    cursor: pointer;
    font-size: var(--text-xs);
    font-family: inherit;
  }

  .vault-toggle-vis:hover {
    color: var(--text-1);
  }

  textarea.input {
    resize: vertical;
  }

  .share-empty {
    color: var(--text-3);
    font-size: var(--text-sm);
    font-style: italic;
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
  }
</style>
