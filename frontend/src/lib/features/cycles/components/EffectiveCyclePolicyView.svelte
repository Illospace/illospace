<script lang="ts">
  import { beforeNavigate } from '$app/navigation';
  import { getContext, onDestroy, onMount } from 'svelte';

  import {
    api,
    type CyclePolicyHistoryRead,
    type EffectiveCyclePolicyRead,
  } from '$lib/api/client';
  import { ConstellationButton, ConstellationNotice } from '$lib/components/constellation';
  import type { CyclePolicyEditorApi } from '$lib/features/cycles/domain/effectivePolicy';
  import {
    EFFECTIVE_POLICY_CLIENT_CONTEXT,
    type EffectivePolicyClientResolver,
  } from '$lib/features/cycles/domain/effectivePolicyClientContext';
  import {
    EffectivePolicyWorkflowController,
    isPolicyWorkflowDirty,
    policyReviewProps,
    type CyclePolicyWorkflowState,
    workflowErrorMessage,
  } from '$lib/features/cycles/domain/effectivePolicyWorkflow';
  import type { RuntimeModelCatalogEntry } from '$lib/types/runtimeSettings';

  import ActiveCyclePolicyView from './ActiveCyclePolicyView.svelte';
  import CyclePolicyDraftEditor from './CyclePolicyDraftEditor.svelte';
  import CyclePolicyHistory from './CyclePolicyHistory.svelte';
  import CyclePolicyReview from './CyclePolicyReview.svelte';

  let {
    cycleId,
    previewPolicy = null,
    previewHistory = null,
    displayTimezone = null,
    compact = false,
    editable = false,
    refreshSerial = 0,
    onPolicyApplied,
    onDirtyChange,
  }: {
    cycleId: number;
    previewPolicy?: EffectiveCyclePolicyRead | null;
    previewHistory?: CyclePolicyHistoryRead | null;
    displayTimezone?: string | null;
    compact?: boolean;
    editable?: boolean;
    refreshSerial?: number;
    onPolicyApplied?: (policy: EffectiveCyclePolicyRead) => void | Promise<void>;
    onDirtyChange?: (dirty: boolean) => void;
  } = $props();

  let workflow = $state<CyclePolicyWorkflowState | null>(null);
  let loading = $state(true);
  let error = $state('');
  let modelCatalog = $state<RuntimeModelCatalogEntry[]>([]);
  let resolvedTimezone = $state(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  );
  let controller: EffectivePolicyWorkflowController | null = null;
  let requestSerial = 0;
  let loadedCycleId: number | null = null;
  let loadedPolicyVersion: number | null = null;
  const resolvePolicyClient = getContext<EffectivePolicyClientResolver | undefined>(
    EFFECTIVE_POLICY_CLIENT_CONTEXT,
  );

  const policy = $derived(workflow?.data.policy ?? null);
  const dirty = $derived(workflow ? isPolicyWorkflowDirty(workflow) : false);
  const showReadView = $derived(
    workflow?.kind === 'view'
    || workflow?.kind === 'reverting'
    || (workflow?.kind === 'conflicted' && !workflow.draft),
  );
  const canEdit = $derived(
    editable
    && (workflow?.kind === 'view' || (workflow?.kind === 'conflicted' && !workflow.draft)),
  );
  const editorState = $derived(
    workflow?.kind === 'edit'
    || workflow?.kind === 'reviewing'
    || (workflow?.kind === 'conflicted' && workflow.draft)
      ? workflow
      : null,
  );
  const reviewState = $derived(
    workflow?.kind === 'review' || workflow?.kind === 'applying' ? workflow : null,
  );
  const notice = $derived(
    workflow?.kind === 'view' || workflow?.kind === 'edit' || workflow?.kind === 'conflicted'
      ? workflow.notice
      : undefined,
  );

  function initializeController(
    selectedCycleId: number,
    client: CyclePolicyEditorApi,
    nextPolicy: EffectiveCyclePolicyRead,
    nextHistory: CyclePolicyHistoryRead,
    nextModelCatalog: readonly RuntimeModelCatalogEntry[],
  ): void {
    controller?.dispose();
    loadedCycleId = selectedCycleId;
    loadedPolicyVersion = nextPolicy.version;
    controller = new EffectivePolicyWorkflowController({
      client,
      cycleId: selectedCycleId,
      data: { policy: nextPolicy, history: nextHistory },
      modelCatalog: nextModelCatalog,
      onStateChange: (state) => {
        loadedPolicyVersion = state.data.policy.version;
        workflow = state;
      },
      onPolicyApplied,
    });
    workflow = controller.state;
  }

  async function loadPolicy(selectedCycleId: number, client: CyclePolicyEditorApi): Promise<void> {
    const serial = ++requestSerial;
    loading = true;
    error = '';
    workflow = null;
    controller?.dispose();
    controller = null;
    try {
      const [nextPolicy, nextHistory, runtime] = await Promise.all([
        client.getCycleBehaviorPolicy(selectedCycleId),
        client.getCycleBehaviorPolicyHistory(selectedCycleId),
        api.runtimeSettings().catch(() => null),
      ]);
      if (serial !== requestSerial) return;
      modelCatalog = runtime?.models.catalog ?? [];
      resolvedTimezone = displayTimezone
        || runtime?.display?.display_timezone
        || Intl.DateTimeFormat().resolvedOptions().timeZone
        || 'UTC';
      initializeController(selectedCycleId, client, nextPolicy, nextHistory, modelCatalog);
    } catch (loadError) {
      if (serial !== requestSerial) return;
      error = workflowErrorMessage(loadError, 'Effective behavior failed to load.');
    } finally {
      if (serial === requestSerial) loading = false;
    }
  }

  function cancelEditing(): void {
    controller?.cancelEditing(() => window.confirm('Discard this behavior draft?'));
  }

  function applyReviewedChange(): void {
    void controller?.applyReviewedChange(
      () => window.confirm('Apply this revert as a new behavior version?'),
    );
  }

  beforeNavigate(({ cancel }) => {
    if (dirty && !window.confirm('Leave this page and discard the behavior draft?')) cancel();
  });

  onMount(() => {
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!workflow || !isPolicyWorkflowDirty(workflow)) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  });

  $effect(() => {
    onDirtyChange?.(dirty);
  });

  $effect(() => {
    const selectedCycleId = cycleId;
    refreshSerial;
    const suppliedPolicy = previewPolicy;
    const suppliedHistory = previewHistory;
    const suppliedClient = suppliedPolicy ? (resolvePolicyClient?.(selectedCycleId) ?? api) : api;
    if (displayTimezone) resolvedTimezone = displayTimezone;
    if (suppliedPolicy) {
      requestSerial += 1;
      loading = false;
      error = '';
      if (
        loadedCycleId !== selectedCycleId
        || loadedPolicyVersion !== suppliedPolicy.version
        || !controller
      ) {
        modelCatalog = [];
        initializeController(
          selectedCycleId,
          suppliedClient,
          suppliedPolicy,
          suppliedHistory ?? {
            items: [],
            pagination: { limit: 50, offset: 0, has_more: false, next_offset: null },
          },
          modelCatalog,
        );
      }
      return;
    }
    void loadPolicy(selectedCycleId, suppliedClient);
  });

  onDestroy(() => {
    requestSerial += 1;
    controller?.dispose();
    onDirtyChange?.(false);
  });
</script>

<section class:compact class="effective-policy" aria-label="Effective cycle behavior">
  <ActiveCyclePolicyView
    {cycleId}
    {policy}
    displayTimezone={resolvedTimezone}
    showDetails={false}
    {canEdit}
    {compact}
    onEdit={() => controller?.startEditing()}
  />

  {#if notice}
    <div class="editor-message">
      <ConstellationNotice
        title={workflow?.kind === 'conflicted' || workflow?.kind === 'edit' ? 'Draft preserved' : 'Behavior updated'}
        description={notice}
        tone={workflow?.kind === 'conflicted' || workflow?.kind === 'edit' ? 'warning' : 'success'}
        compact
      />
    </div>
  {/if}

  {#if loading}
    <div class="policy-loading" aria-label="Loading effective behavior">
      <span></span><span></span><span></span>
    </div>
  {:else if error}
    <div class="policy-error" role="alert">
      <span>{error}</span>
      <ConstellationButton variant="secondary" size="sm" onclick={() => loadPolicy(cycleId, api)}>
        Retry
      </ConstellationButton>
    </div>
  {:else if workflow && policy}
    {#if editorState && editorState.draft}
      <CyclePolicyDraftEditor
        {cycleId}
        draft={editorState.draft}
        errors={editorState.kind === 'edit' ? editorState.errors : {}}
        error={editorState.kind === 'edit' ? editorState.error : undefined}
        dirty={isPolicyWorkflowDirty(editorState)}
        reviewing={editorState.kind === 'reviewing'}
        {compact}
        {modelCatalog}
        onDraftChange={(draft) => controller?.updateDraft(draft)}
        onCancel={cancelEditing}
        onReview={() => void controller?.reviewDraft()}
      />
    {:else if reviewState}
      <CyclePolicyReview
        {cycleId}
        {...policyReviewProps(reviewState)}
        {compact}
        onRationaleChange={(rationale) => controller?.setRationale(rationale)}
        onBack={() => controller?.leaveReview()}
        onApply={applyReviewedChange}
      />
    {:else if showReadView}
      <ActiveCyclePolicyView
        {cycleId}
        {policy}
        displayTimezone={resolvedTimezone}
        showHeader={false}
        showDetails
        canEdit={false}
        {compact}
        onEdit={() => {}}
      />
    {/if}

    <CyclePolicyHistory
      history={workflow.data.history}
      displayTimezone={resolvedTimezone}
      {editable}
      workflowKind={workflow.kind}
      revertingChangeId={workflow.kind === 'reverting' ? workflow.changeId : null}
      historyError={workflow.historyError}
      onRevert={(changeId) => void controller?.beginRevert(changeId)}
      onLoadMore={() => controller?.loadMoreHistory() ?? Promise.resolve()}
    />
  {/if}
</section>

<style>
  .effective-policy {
    display: grid;
    min-width: 0;
    overflow: hidden;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: var(--constellation-surface-panel-background);
    color: var(--constellation-color-text-primary);
  }

  .editor-message {
    padding: 12px 14px 0;
  }

  .policy-error {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px;
    color: var(--constellation-color-danger);
    font-size: 12px;
  }

  .policy-loading {
    display: grid;
    gap: 8px;
    padding: 14px;
  }

  .policy-loading span {
    height: 48px;
    border-radius: 6px;
    background:
      linear-gradient(90deg, transparent, var(--constellation-skeleton-row-shimmer), transparent),
      var(--constellation-skeleton-row-background);
    background-size: 200% 100%;
    animation: policy-pulse 1.4s ease-in-out infinite;
  }

  @keyframes policy-pulse {
    from { background-position: 200% 0; }
    to { background-position: -200% 0; }
  }
</style>
