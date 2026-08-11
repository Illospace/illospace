<script lang="ts">
  import { beforeNavigate } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';

  import {
    api,
    type CyclePolicyChangeRead,
    type CyclePolicyConflictDetail,
    type CyclePolicyHistoryRead,
    type EffectiveCyclePolicyRead,
  } from '$lib/api/client';
  import {
    ConstellationButton,
    ConstellationNotice,
    ConstellationPill,
    ConstellationSelect,
    ConstellationTextInput,
    ConstellationTextarea,
  } from '$lib/components/constellation';
  import {
    applyPolicyReview,
    clonePolicyDraft,
    formatPolicyDateTime,
    hydratePolicyDraft,
    isPolicyDraftDirty,
    policyConfigurationEntries,
    policyFieldLabel,
    policyFieldSource,
    policySourceLabel,
    policyValueLabel,
    presentedPolicyDiff,
    recoverPolicyDraftAfterConflict,
    retiredGuidance,
    reviewPolicyDraft,
    reviewPolicyRevert,
    shouldConfirmPolicyDraftDiscard,
    type CyclePolicyDraft,
    type CyclePolicyDraftErrors,
    type CyclePolicyReview,
  } from '$lib/features/cycles/domain/effectivePolicy';
  import type { RuntimeModelCatalogEntry } from '$lib/types/runtimeSettings';

  type EditorStage = 'view' | 'edit' | 'review';

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

  let policy = $state<EffectiveCyclePolicyRead | null>(null);
  let history = $state<CyclePolicyChangeRead[]>([]);
  let pagination = $state<CyclePolicyHistoryRead['pagination'] | null>(null);
  let loading = $state(true);
  let historyLoading = $state(false);
  let reviewing = $state(false);
  let applying = $state(false);
  let revertingChangeId = $state<number | null>(null);
  let error = $state('');
  let historyError = $state('');
  let editorError = $state('');
  let editorNotice = $state('');
  let stage = $state<EditorStage>('view');
  let draft = $state<CyclePolicyDraft | null>(null);
  let draftErrors = $state<CyclePolicyDraftErrors>({});
  let review = $state<CyclePolicyReview | null>(null);
  let rationale = $state('');
  let modelCatalog = $state<RuntimeModelCatalogEntry[]>([]);
  let resolvedTimezone = $state(
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  );
  let requestSerial = 0;
  let loadedCycleId: number | null = null;

  const configurationEntries = $derived(
    policy ? policyConfigurationEntries(policy.configuration) : [],
  );
  const guidanceSource = $derived(
    policy ? policyFieldSource(policy.field_sources, 'guidance') : undefined,
  );
  const dirty = $derived(isPolicyDraftDirty(draft, policy));
  const diffEntries = $derived(review ? presentedPolicyDiff(review.preview) : []);
  const modelOptions = $derived([
    { value: '', label: 'Workspace default', description: 'Use the workspace model' },
    ...modelCatalog.map((entry) => ({
      value: entry.id,
      label: entry.label,
      description: entry.description,
    })),
    ...(draft?.model_override
      && !modelCatalog.some((entry) => entry.id === draft?.model_override)
      ? [{
          value: draft.model_override,
          label: draft.model_override,
          description: 'Current model selection',
        }]
      : []),
  ]);
  const thinkingOptions = [
    { value: '', label: 'Workspace default' },
    { value: 'none', label: 'None' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'xHigh' },
  ];
  const applyDisabled = $derived(
    applying || !review || !review.preview.changed_fields.length || !rationale.trim(),
  );

  function errorMessage(value: unknown, fallback: string): string {
    if (value && typeof value === 'object' && 'detail' in value) {
      const detail = (value as { detail?: unknown }).detail;
      return typeof detail === 'string' ? detail : fallback;
    }
    return value instanceof Error ? value.message : fallback;
  }

  function conflictDetail(value: unknown): CyclePolicyConflictDetail | null {
    if (!value || typeof value !== 'object') return null;
    const candidate = value as { status?: unknown; detail?: unknown };
    if (candidate.status !== 409 || !candidate.detail || typeof candidate.detail !== 'object') return null;
    const detail = candidate.detail as Partial<CyclePolicyConflictDetail>;
    if (!detail.latest_effective_policy || typeof detail.reason !== 'string') return null;
    return detail as CyclePolicyConflictDetail;
  }

  function resetEditor(nextPolicy: EffectiveCyclePolicyRead, nextStage: EditorStage = 'view') {
    draft = hydratePolicyDraft(nextPolicy);
    draftErrors = {};
    review = null;
    rationale = '';
    editorError = '';
    stage = nextStage;
  }

  async function loadPolicy(selectedCycleId: number) {
    if (loadedCycleId !== selectedCycleId) editorNotice = '';
    loadedCycleId = selectedCycleId;
    const serial = ++requestSerial;
    loading = true;
    error = '';
    historyError = '';
    policy = null;
    history = [];
    pagination = null;
    try {
      const [nextPolicy, nextHistory, runtime] = await Promise.all([
        api.getCycleBehaviorPolicy(selectedCycleId),
        api.getCycleBehaviorPolicyHistory(selectedCycleId),
        api.runtimeSettings().catch(() => null),
      ]);
      if (serial !== requestSerial) return;
      policy = nextPolicy;
      history = nextHistory.items;
      pagination = nextHistory.pagination;
      modelCatalog = runtime?.models.catalog ?? [];
      resolvedTimezone = displayTimezone
        || runtime?.display?.display_timezone
        || Intl.DateTimeFormat().resolvedOptions().timeZone
        || 'UTC';
      resetEditor(nextPolicy);
    } catch (loadError) {
      if (serial !== requestSerial) return;
      error = errorMessage(loadError, 'Effective behavior failed to load.');
    } finally {
      if (serial === requestSerial) loading = false;
    }
  }

  async function loadMoreHistory() {
    if (!pagination?.has_more || pagination.next_offset === null || historyLoading) return;
    const selectedCycleId = cycleId;
    const serial = requestSerial;
    historyLoading = true;
    historyError = '';
    try {
      const nextHistory = await api.getCycleBehaviorPolicyHistory(
        selectedCycleId,
        pagination.limit,
        pagination.next_offset,
      );
      if (serial !== requestSerial || selectedCycleId !== cycleId) return;
      history = [...history, ...nextHistory.items];
      pagination = nextHistory.pagination;
    } catch (loadError) {
      if (serial !== requestSerial) return;
      historyError = errorMessage(loadError, 'Older history failed to load.');
    } finally {
      if (serial === requestSerial) historyLoading = false;
    }
  }

  function startEditing() {
    if (!policy) return;
    resetEditor(policy, 'edit');
    editorNotice = '';
  }

  function cancelEditing() {
    if (!policy) return;
    if (
      shouldConfirmPolicyDraftDiscard(draft, policy)
      && !window.confirm('Discard this behavior draft?')
    ) return;
    resetEditor(policy);
    editorNotice = '';
  }

  function addGuidance() {
    if (!draft) return;
    draft.guidance.push('');
    draftErrors = { ...draftErrors, guidance: undefined };
  }

  function removeGuidance(index: number) {
    if (!draft) return;
    draft.guidance.splice(index, 1);
    draftErrors = { ...draftErrors, guidance: undefined };
  }

  function toggleDraftEnabled() {
    if (draft) draft.enabled = !draft.enabled;
  }

  async function reviewDraft() {
    if (!draft || !policy) return;
    reviewing = true;
    editorError = '';
    editorNotice = '';
    try {
      const result = await reviewPolicyDraft(
        api,
        cycleId,
        clonePolicyDraft(draft),
        modelCatalog,
        policy.configuration.model_override,
      );
      draftErrors = result.errors;
      if (!result.review) return;
      review = result.review;
      rationale = '';
      stage = 'review';
    } catch (reviewError) {
      editorError = errorMessage(reviewError, 'The change could not be reviewed.');
    } finally {
      reviewing = false;
    }
  }

  async function beginRevert(changeId: number) {
    revertingChangeId = changeId;
    editorError = '';
    editorNotice = '';
    try {
      review = await reviewPolicyRevert(api, cycleId, changeId);
      rationale = '';
      stage = 'review';
    } catch (revertError) {
      historyError = errorMessage(revertError, 'The revert could not be reviewed.');
    } finally {
      revertingChangeId = null;
    }
  }

  function leaveReview() {
    const reviewKind = review?.kind;
    review = null;
    rationale = '';
    editorError = '';
    stage = reviewKind === 'edit' ? 'edit' : 'view';
  }

  async function handleConflict(detail: CyclePolicyConflictDetail) {
    if (draft && review?.kind === 'edit') {
      const recovered = recoverPolicyDraftAfterConflict(draft, detail.latest_effective_policy);
      draft = recovered.draft;
      policy = recovered.policy;
      stage = 'edit';
      editorNotice = 'Another person changed this Cycle. Your draft is safe. Review it against the latest version and try again.';
    } else {
      policy = detail.latest_effective_policy;
      resetEditor(detail.latest_effective_policy);
      editorNotice = 'Another person changed this Cycle. Review the latest version before you try again.';
    }
    review = null;
    try {
      const nextHistory = await api.getCycleBehaviorPolicyHistory(cycleId);
      history = nextHistory.items;
      pagination = nextHistory.pagination;
    } catch {
      historyError = 'History could not be refreshed after the conflict.';
    }
  }

  async function applyReviewedChange() {
    if (applyDisabled) return;
    const reviewKind = review?.kind;
    applying = true;
    editorError = '';
    editorNotice = '';
    try {
      const result = await applyPolicyReview(
        api,
        cycleId,
        review,
        rationale,
        () => window.confirm('Apply this revert as a new behavior version?'),
      );
      if (!result) return;
      policy = result.policy;
      history = result.history.items;
      pagination = result.history.pagination;
      resetEditor(result.policy);
      editorNotice = reviewKind === 'revert'
        ? 'Revert applied as a new behavior version.'
        : 'Behavior applied for future runs.';
      await onPolicyApplied?.(result.policy);
    } catch (applyError) {
      const conflict = conflictDetail(applyError);
      if (conflict) await handleConflict(conflict);
      else editorError = errorMessage(applyError, 'The change could not be applied.');
    } finally {
      applying = false;
    }
  }

  beforeNavigate(({ cancel }) => {
    if (
      shouldConfirmPolicyDraftDiscard(draft, policy)
      && !window.confirm('Leave this page and discard the behavior draft?')
    ) cancel();
  });

  onMount(() => {
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!shouldConfirmPolicyDraftDiscard(draft, policy)) return;
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
    if (displayTimezone) resolvedTimezone = displayTimezone;
    if (suppliedPolicy) {
      if (loadedCycleId !== selectedCycleId) editorNotice = '';
      loadedCycleId = selectedCycleId;
      requestSerial += 1;
      policy = suppliedPolicy;
      history = suppliedHistory?.items ?? [];
      pagination = suppliedHistory?.pagination ?? null;
      loading = false;
      error = '';
      historyError = '';
      resetEditor(suppliedPolicy);
      return;
    }
    void loadPolicy(selectedCycleId);
  });

  onDestroy(() => {
    requestSerial += 1;
    onDirtyChange?.(false);
  });
</script>

<section class="effective-policy" class:compact aria-label="Effective cycle behavior">
  <header class="active-heading">
    <div>
      <span class="section-kicker">Effective behavior</span>
      <h3>Active now</h3>
      <p>What Illo will use on the next run.</p>
    </div>
    {#if policy}
      <div class="active-actions">
        <div class="active-status">
          <ConstellationPill variant={policy.configuration.enabled ? 'success' : 'muted'} leadingDot>
            {policy.configuration.enabled ? 'Enabled' : 'Paused'}
          </ConstellationPill>
          <span>Version {policy.version}</span>
        </div>
        {#if editable && stage === 'view'}
          <ConstellationButton variant="secondary" size="sm" onclick={startEditing}>
            Edit behavior
          </ConstellationButton>
        {/if}
      </div>
    {/if}
  </header>

  {#if editorNotice}
    <div class="editor-message">
      <ConstellationNotice
        title={stage === 'edit' ? 'Draft preserved' : 'Behavior updated'}
        description={editorNotice}
        tone={stage === 'edit' ? 'warning' : 'success'}
        compact
      />
    </div>
  {/if}

  {#if loading}
    <div class="policy-loading" aria-label="Loading effective behavior">
      <span></span>
      <span></span>
      <span></span>
    </div>
  {:else if error}
    <div class="policy-error" role="alert">
      <span>{error}</span>
      <ConstellationButton variant="secondary" size="sm" onclick={() => loadPolicy(cycleId)}>
        Retry
      </ConstellationButton>
    </div>
  {:else if policy}
    {#if stage === 'edit' && draft}
      <section class="policy-editor" aria-label="Edit cycle behavior">
        <header class="editor-heading">
          <div>
            <span class="section-kicker">Draft</span>
            <h4>Prepare behavior change</h4>
          </div>
          <p>Nothing changes until you review and apply.</p>
        </header>

        {#if editorError}
          <p class="editor-error" role="alert">{editorError}</p>
        {/if}

        <div class="editor-fields">
          <div class="editor-field editor-field-full">
            <label for={`cycle-${cycleId}-policy-prompt`}>Mission prompt</label>
            <ConstellationTextarea
              id={`cycle-${cycleId}-policy-prompt`}
              bind:value={draft.prompt}
              rows={7}
              aria-invalid={draftErrors.prompt ? 'true' : undefined}
            />
            {#if draftErrors.prompt}<small class="field-error">{draftErrors.prompt}</small>{/if}
          </div>

          <div class="editor-field">
            <label for={`cycle-${cycleId}-policy-schedule`}>Stored schedule</label>
            <ConstellationTextInput
              id={`cycle-${cycleId}-policy-schedule`}
              bind:value={draft.schedule_expr}
              mono
              placeholder="0 9 * * *"
              aria-invalid={draftErrors.schedule_expr ? 'true' : undefined}
            />
            {#if draftErrors.schedule_expr}<small class="field-error">{draftErrors.schedule_expr}</small>{/if}
          </div>

          <div class="editor-field">
            <label for={`cycle-${cycleId}-policy-timezone`}>Timezone</label>
            <ConstellationTextInput
              id={`cycle-${cycleId}-policy-timezone`}
              bind:value={draft.timezone}
              placeholder="America/Toronto"
              aria-invalid={draftErrors.timezone ? 'true' : undefined}
            />
            {#if draftErrors.timezone}<small class="field-error">{draftErrors.timezone}</small>{/if}
          </div>

          <div class="editor-field">
            <span class="editor-field-label">Status</span>
            <button
              type="button"
              class="policy-status-toggle"
              class:is-enabled={draft.enabled}
              aria-pressed={draft.enabled}
              onclick={toggleDraftEnabled}
            >
              <span aria-hidden="true"></span>
              <strong>{draft.enabled ? 'Enabled' : 'Paused'}</strong>
            </button>
          </div>

          <div class="editor-field">
            <span class="editor-field-label">Model override</span>
            <ConstellationSelect
              bind:value={draft.model_override}
              options={modelOptions}
              ariaLabel="Model override"
            />
            {#if draftErrors.model_override}<small class="field-error">{draftErrors.model_override}</small>{/if}
          </div>

          <div class="editor-field">
            <span class="editor-field-label">Thinking override</span>
            <ConstellationSelect
              bind:value={draft.thinking_override}
              options={thinkingOptions}
              ariaLabel="Thinking override"
            />
            {#if draftErrors.thinking_override}<small class="field-error">{draftErrors.thinking_override}</small>{/if}
          </div>

          <section class="guidance-editor editor-field-full" aria-labelledby={`cycle-${cycleId}-guidance-editor`}>
            <div class="guidance-editor-heading">
              <div>
                <span class="editor-field-label" id={`cycle-${cycleId}-guidance-editor`}>Active guidance</span>
                <small>Changing text retires the old guidance and adds a new entry.</small>
              </div>
              <ConstellationButton variant="quiet" size="sm" onclick={addGuidance}>
                Add guidance
              </ConstellationButton>
            </div>
            {#if draft.guidance.length}
              <ol class="guidance-editor-list">
                {#each draft.guidance as guidance, index}
                  <li>
                    <ConstellationTextarea
                      bind:value={draft.guidance[index]}
                      rows={2}
                      aria-label={`Guidance ${index + 1}`}
                    />
                    <ConstellationButton
                      variant="quiet"
                      size="sm"
                      aria-label={`Remove guidance ${index + 1}`}
                      onclick={() => removeGuidance(index)}
                    >
                      Remove
                    </ConstellationButton>
                  </li>
                {/each}
              </ol>
            {:else}
              <p class="empty-policy-value">No active guidance.</p>
            {/if}
            {#if draftErrors.guidance}<small class="field-error">{draftErrors.guidance}</small>{/if}
          </section>
        </div>

        <div class="editor-actions">
          <ConstellationButton variant="quiet" size="sm" onclick={cancelEditing}>Cancel</ConstellationButton>
          <ConstellationButton
            variant="primary"
            size="sm"
            loading={reviewing}
            disabled={!dirty}
            onclick={reviewDraft}
          >
            Review change
          </ConstellationButton>
        </div>
      </section>
    {:else if stage === 'review' && review}
      <section class="policy-review" aria-label="Review cycle behavior change">
        <header class="editor-heading">
          <div>
            <span class="section-kicker">Review</span>
            <h4>{review.kind === 'revert' ? 'Review revert' : 'Review behavior change'}</h4>
          </div>
          <p>Active runs are unchanged. Future runs use this policy only after apply.</p>
        </header>

        {#if review.preview.changed_fields.length}
          <div class="diff-list">
            {#each diffEntries as entry (entry.key)}
              <article class="diff-entry">
                <h5>{entry.label}</h5>
                {#if entry.kind === 'schedule'}
                  <div class="diff-columns">
                    <div>
                      <span>Before</span>
                      <strong>{entry.before.schedule_human}</strong>
                      <code>{entry.before.schedule_expr}</code>
                      <small>{entry.before.timezone.replaceAll('_', ' ')}</small>
                    </div>
                    <div>
                      <span>After</span>
                      <strong>{entry.after.schedule_human}</strong>
                      <code>{entry.after.schedule_expr}</code>
                      <small>{entry.after.timezone.replaceAll('_', ' ')}</small>
                    </div>
                  </div>
                {:else if entry.kind === 'guidance'}
                  <div class="guidance-diff">
                    {#if entry.retired.length}
                      <div class="guidance-retire">
                        <span>Retire</span>
                        {#each entry.retired as item}<p>{item}</p>{/each}
                      </div>
                    {/if}
                    {#if entry.added.length}
                      <div class="guidance-add">
                        <span>Add</span>
                        {#each entry.added as item}<p>{item}</p>{/each}
                      </div>
                    {/if}
                  </div>
                {:else}
                  <div class="diff-columns">
                    <div><span>Before</span><p>{entry.before}</p></div>
                    <div><span>After</span><p>{entry.after}</p></div>
                  </div>
                {/if}
              </article>
            {/each}
          </div>
        {:else}
          <ConstellationNotice
            title="No effective change"
            description="This draft matches the current behavior. There is nothing to apply."
            tone="neutral"
            compact
          />
        {/if}

        {#each review.preview.warnings as warning (warning.code)}
          <p class="review-warning">{warning.message}</p>
        {/each}

        <div class="editor-field editor-field-full rationale-field">
          <label for={`cycle-${cycleId}-policy-rationale`}>Rationale <span>Required</span></label>
          <ConstellationTextarea
            id={`cycle-${cycleId}-policy-rationale`}
            bind:value={rationale}
            rows={3}
            maxlength={5000}
            placeholder="Why should this behavior change?"
          />
        </div>

        {#if editorError}<p class="editor-error" role="alert">{editorError}</p>{/if}

        <div class="editor-actions">
          <ConstellationButton variant="quiet" size="sm" onclick={leaveReview}>
            {review.kind === 'edit' ? 'Back to draft' : 'Cancel'}
          </ConstellationButton>
          <ConstellationButton
            variant="primary"
            size="sm"
            loading={applying}
            disabled={applyDisabled}
            onclick={applyReviewedChange}
          >
            {review.kind === 'revert' ? 'Apply revert' : 'Apply change'}
          </ConstellationButton>
        </div>
      </section>
    {:else}
      <div class="policy-content">
      <section class="policy-section" aria-labelledby={`cycle-${cycleId}-configuration`}>
        <div class="policy-section-heading">
          <h4 id={`cycle-${cycleId}-configuration`}>Mission and settings</h4>
          <span>{resolvedTimezone.replaceAll('_', ' ')}</span>
        </div>

        <dl class="policy-values">
          {#each configurationEntries as entry (entry.key)}
            {@const source = policyFieldSource(policy.field_sources, entry.key)}
            <div class="policy-value" class:prose-value={entry.key === 'prompt'}>
              <dt>{policyFieldLabel(entry.key)}</dt>
              <dd class:mono-value={entry.key.endsWith('_expr') || typeof entry.value === 'object'}>
                {policyValueLabel(entry.value)}
              </dd>
              <dd class="value-source" title={source?.rationale || undefined}>
                <span>From {source ? policySourceLabel(source) : policySourceLabel(policy.source)}</span>
                <time datetime={source?.changed_at || policy.source.changed_at || undefined}>
                  Changed {formatPolicyDateTime(source?.changed_at || policy.source.changed_at, resolvedTimezone)}
                </time>
              </dd>
            </div>
          {/each}
        </dl>
      </section>

      <section class="policy-section active-guidance" aria-labelledby={`cycle-${cycleId}-guidance`}>
        <div class="policy-section-heading">
          <div>
            <span class="live-marker">Live</span>
            <h4 id={`cycle-${cycleId}-guidance`}>Active guidance</h4>
          </div>
          <span>{policy.guidance.length} active</span>
        </div>

        {#if policy.guidance.length}
          <ol class="guidance-list">
            {#each policy.guidance as guidance}
              <li>
                <p>{guidance}</p>
                <div class="value-source" title={guidanceSource?.rationale || undefined}>
                  <span>From {guidanceSource ? policySourceLabel(guidanceSource) : policySourceLabel(policy.source)}</span>
                  <time datetime={guidanceSource?.changed_at || policy.source.changed_at || undefined}>
                    Changed {formatPolicyDateTime(guidanceSource?.changed_at || policy.source.changed_at, resolvedTimezone)}
                  </time>
                </div>
              </li>
            {/each}
          </ol>
        {:else}
          <p class="empty-policy-value">No active guidance.</p>
        {/if}
      </section>

      <section class="policy-section" aria-labelledby={`cycle-${cycleId}-outputs`}>
        <div class="policy-section-heading">
          <h4 id={`cycle-${cycleId}-outputs`}>Output targets</h4>
          <ConstellationPill variant="muted">Read only</ConstellationPill>
        </div>

        {#if policy.output_targets.length}
          <div class="output-list">
            {#each policy.output_targets as target (target.id)}
              <article class="output-target">
                <div class="output-target-heading">
                  <strong>{target.label || policyFieldLabel(target.target_type)}</strong>
                  <span>{target.target_type}</span>
                </div>
                {#if target.target_id}<p>Target {target.target_id}</p>{/if}
                {#if Object.keys(target.config).length}
                  <pre>{policyValueLabel(target.config)}</pre>
                {/if}
                {#if target.rationale}<p>{target.rationale}</p>{/if}
                <div class="value-source">
                  <span>From {[target.source_type, target.source_id].filter(Boolean).join(' · ')}</span>
                  <time datetime={target.updated_at}>
                    Changed {formatPolicyDateTime(target.updated_at, resolvedTimezone)}
                  </time>
                </div>
              </article>
            {/each}
          </div>
        {:else}
          <p class="empty-policy-value">No output targets.</p>
        {/if}
      </section>
      </div>
    {/if}
  {/if}

  {#if !loading && !error}
    <details class="history-region">
      <summary>
        <span>
          <strong>History</strong>
          <small>Prior versions are not active.</small>
        </span>
        <span>{history.length} changes</span>
      </summary>

      <div class="history-content">
        <div class="history-warning">
          <strong>Historical only</strong>
          <span>Text below does not guide the next run.</span>
        </div>

        {#if history.length}
          <ol class="history-list">
            {#each history as change (change.id)}
              {@const retired = retiredGuidance(change)}
              <li class="history-change">
                <header>
                  <div>
                    <strong>Version {change.version}</strong>
                    <time datetime={change.applied_at}>
                      {formatPolicyDateTime(change.applied_at, resolvedTimezone)}
                    </time>
                  </div>
                  <div class="history-change-actions">
                    <span>{change.changed_fields.map(policyFieldLabel).join(', ')}</span>
                    {#if editable}
                      <ConstellationButton
                        variant="quiet"
                        size="sm"
                        loading={revertingChangeId === change.id}
                        disabled={stage !== 'view'}
                        onclick={() => beginRevert(change.id)}
                      >
                        Revert
                      </ConstellationButton>
                    {/if}
                  </div>
                </header>
                <p>{change.rationale}</p>
                <div class="history-source">
                  From {policySourceLabel(change)}
                  {#if change.reverted_from_id !== null}
                    · Reverted change {change.reverted_from_id}
                  {/if}
                </div>

                {#if retired.length}
                  <section class="retired-guidance" aria-label="Retired guidance">
                    <span>Retired guidance · not active</span>
                    {#each retired as guidance}
                      <blockquote>{guidance}</blockquote>
                    {/each}
                  </section>
                {/if}
              </li>
            {/each}
          </ol>
        {:else}
          <p class="empty-policy-value">No policy changes yet.</p>
        {/if}

        {#if historyError}
          <p class="history-error" role="alert">{historyError}</p>
        {/if}
        {#if pagination?.has_more}
          <ConstellationButton
            variant="quiet"
            size="sm"
            loading={historyLoading}
            onclick={loadMoreHistory}
          >
            Load older changes
          </ConstellationButton>
        {/if}
      </div>
    </details>
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

  .active-heading,
  .policy-section-heading,
  .output-target-heading,
  .history-change header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .active-heading {
    padding: 14px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-color-success) 7%, transparent);
  }

  .active-heading h3,
  .active-heading p,
  .policy-section-heading h4,
  .guidance-list p,
  .output-target p,
  .history-change p,
  .history-change blockquote {
    margin: 0;
  }

  .active-heading h3 {
    margin-top: 3px;
    font-size: 16px;
    font-weight: 650;
    letter-spacing: -0.01em;
  }

  .active-heading p {
    margin-top: 5px;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .section-kicker,
  .policy-value dt,
  .live-marker,
  .retired-guidance > span {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.12em;
    line-height: 1.2;
    text-transform: uppercase;
  }

  .active-status {
    display: grid;
    justify-items: end;
    gap: 6px;
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
  }

  .active-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .editor-message {
    padding: 12px 14px 0;
  }

  .policy-editor,
  .policy-review {
    display: grid;
    gap: 16px;
    min-width: 0;
    padding: 16px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-color-info) 3%, transparent);
  }

  .editor-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  .editor-heading h4,
  .editor-heading p,
  .diff-entry h5,
  .diff-entry p,
  .guidance-editor-heading small,
  .review-warning,
  .editor-error {
    margin: 0;
  }

  .editor-heading h4 {
    margin-top: 4px;
    font-size: 15px;
    font-weight: 650;
  }

  .editor-heading p {
    max-width: 360px;
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.5;
    text-align: right;
  }

  .editor-fields {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }

  .editor-field {
    display: grid;
    align-content: start;
    gap: 7px;
    min-width: 0;
  }

  .editor-field-full {
    grid-column: 1 / -1;
  }

  .editor-field > label,
  .editor-field-label {
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    font-weight: 650;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .editor-field > label span {
    color: var(--constellation-color-warning);
    letter-spacing: 0;
    text-transform: none;
  }

  .editor-field :global(.constellation-text-input),
  .editor-field :global(.constellation-textarea),
  .editor-field :global(.constellation-select),
  .guidance-editor-list :global(.constellation-textarea) {
    width: 100%;
  }

  .field-error,
  .editor-error {
    color: var(--constellation-color-danger);
    font-size: 11px;
    line-height: 1.4;
  }

  .policy-status-toggle {
    appearance: none;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 9px;
    min-height: 40px;
    padding: 6px 10px;
    border: 1px solid var(--constellation-control-field-border);
    border-radius: 8px;
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    cursor: pointer;
  }

  .policy-status-toggle > span {
    width: 10px;
    height: 10px;
    border-radius: 999px;
    background: var(--constellation-color-text-muted);
  }

  .policy-status-toggle.is-enabled > span {
    background: var(--constellation-color-success);
    box-shadow: 0 0 10px color-mix(in srgb, var(--constellation-color-success) 55%, transparent);
  }

  .policy-status-toggle strong {
    font-size: 12px;
    font-weight: 600;
    text-align: left;
  }

  .policy-status-toggle:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .guidance-editor {
    display: grid;
    gap: 10px;
    min-width: 0;
    padding-top: 2px;
  }

  .guidance-editor-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .guidance-editor-heading > div {
    display: grid;
    gap: 4px;
  }

  .guidance-editor-heading small {
    color: var(--constellation-color-text-muted);
    font-size: 10px;
    line-height: 1.4;
  }

  .guidance-editor-list {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .guidance-editor-list li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: 8px;
  }

  .editor-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 12px;
    border-top: 1px solid var(--constellation-section-divider);
  }

  .diff-list {
    display: grid;
    gap: 10px;
  }

  .diff-entry {
    display: grid;
    gap: 9px;
    padding: 12px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 7px;
    background: var(--constellation-surface-nested-background);
  }

  .diff-entry h5 {
    font-size: 12px;
    font-weight: 650;
  }

  .diff-columns,
  .guidance-diff {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }

  .diff-columns > div,
  .guidance-diff > div {
    display: grid;
    align-content: start;
    gap: 6px;
    min-width: 0;
    padding: 9px;
    border-left: 2px solid var(--constellation-color-text-muted);
    background: color-mix(in srgb, var(--constellation-color-text-muted) 4%, transparent);
  }

  .diff-columns span,
  .guidance-diff span {
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 9px;
    font-weight: 650;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .diff-columns strong,
  .diff-columns p,
  .guidance-diff p {
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  .diff-columns code {
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
  }

  .diff-columns small {
    color: var(--constellation-color-text-muted);
    font-size: 10px;
  }

  .guidance-retire {
    border-left-color: var(--constellation-color-warning) !important;
  }

  .guidance-add {
    border-left-color: var(--constellation-color-success) !important;
  }

  .review-warning {
    padding: 8px 10px;
    border-left: 2px solid var(--constellation-color-info);
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.45;
  }

  .rationale-field {
    padding-top: 2px;
  }

  .policy-content {
    display: grid;
  }

  .policy-section {
    display: grid;
    gap: 12px;
    min-width: 0;
    padding: 14px;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .policy-section-heading {
    align-items: center;
  }

  .policy-section-heading > div {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .policy-section-heading h4 {
    font-size: 13px;
    font-weight: 600;
  }

  .policy-section-heading > span {
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
  }

  .live-marker {
    padding: 3px 6px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--constellation-color-success) 14%, transparent);
    color: var(--constellation-color-success);
  }

  .policy-values {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0;
    margin: 0;
    border-top: 1px solid var(--constellation-section-divider);
  }

  .policy-value {
    display: grid;
    align-content: start;
    gap: 7px;
    min-width: 0;
    padding: 12px 10px;
    border-bottom: 1px solid var(--constellation-section-divider);
  }

  .policy-value:nth-child(odd) {
    border-right: 1px solid var(--constellation-section-divider);
  }

  .policy-value.prose-value {
    grid-column: 1 / -1;
    border-right: 0;
  }

  .policy-value dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  .policy-value.prose-value > dd:not(.value-source) {
    font-size: 14px;
  }

  .mono-value,
  .output-target pre {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .value-source {
    display: flex;
    flex-wrap: wrap;
    gap: 3px 10px;
    color: var(--constellation-color-text-muted) !important;
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px !important;
    line-height: 1.4 !important;
  }

  .active-guidance {
    border-left: 3px solid var(--constellation-color-success);
    background: color-mix(in srgb, var(--constellation-color-success) 3%, transparent);
  }

  .guidance-list,
  .output-list,
  .history-list {
    display: grid;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .guidance-list {
    counter-reset: active-guidance;
  }

  .guidance-list li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 6px 10px;
    padding: 10px 0;
    border-bottom: 1px solid var(--constellation-section-divider);
    counter-increment: active-guidance;
  }

  .guidance-list li::before {
    content: counter(active-guidance, decimal-leading-zero);
    color: var(--constellation-color-success);
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
    font-size: 10px;
    line-height: 1.7;
  }

  .guidance-list li:last-child {
    border-bottom: 0;
  }

  .guidance-list .value-source {
    grid-column: 2;
  }

  .guidance-list p {
    font-size: 13px;
    line-height: 1.55;
  }

  .output-target {
    display: grid;
    gap: 7px;
    padding: 11px;
    border: 1px solid var(--constellation-surface-nested-border);
    border-radius: 7px;
    background: var(--constellation-surface-nested-background);
  }

  .output-target-heading strong {
    font-size: 13px;
    font-weight: 600;
  }

  .output-target-heading span,
  .output-target p,
  .output-target pre {
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
    line-height: 1.45;
  }

  .output-target-heading span {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .output-target pre {
    max-width: 100%;
    margin: 0;
    overflow: auto;
    white-space: pre-wrap;
  }

  .empty-policy-value {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .history-region summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 46px;
    padding: 0 14px;
    background: color-mix(in srgb, var(--constellation-color-text-muted) 5%, transparent);
    cursor: pointer;
    list-style: none;
  }

  .history-region summary::-webkit-details-marker {
    display: none;
  }

  .history-region summary > span:first-child {
    display: grid;
    gap: 2px;
  }

  .history-region summary strong {
    font-size: 13px;
    font-weight: 600;
  }

  .history-region summary small,
  .history-region summary > span:last-child,
  .history-source,
  .history-change header span,
  .history-change time {
    color: var(--constellation-color-text-muted);
    font-size: 10px;
  }

  .history-content {
    display: grid;
    gap: 12px;
    padding: 14px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
    background: color-mix(in srgb, var(--constellation-color-text-muted) 3%, transparent);
  }

  .history-warning {
    display: flex;
    flex-wrap: wrap;
    gap: 5px 10px;
    padding: 8px 10px;
    border: 1px dashed var(--constellation-color-text-muted);
    border-radius: 6px;
    color: var(--constellation-color-text-secondary);
    font-size: 11px;
  }

  .history-warning strong,
  .retired-guidance > span {
    color: var(--constellation-color-text-primary);
  }

  .history-change {
    display: grid;
    gap: 7px;
    padding: 11px 0;
    border-bottom: 1px solid var(--constellation-section-divider);
    opacity: 0.82;
  }

  .history-change:last-child {
    border-bottom: 0;
  }

  .history-change header > div {
    display: grid;
    gap: 3px;
  }

  .history-change-actions {
    justify-items: end;
  }

  .history-change-actions > span {
    max-width: 50%;
    text-align: right;
  }

  .history-change p {
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
  }

  .history-source {
    font-family: var(--constellation-font-mono, 'IBM Plex Mono', monospace);
  }

  .retired-guidance {
    display: grid;
    gap: 7px;
    margin-top: 3px;
    padding: 10px;
    border: 1px dashed var(--constellation-color-text-muted);
    border-radius: 6px;
    background: color-mix(in srgb, var(--constellation-color-text-muted) 6%, transparent);
  }

  .retired-guidance blockquote {
    padding-left: 10px;
    border-left: 2px solid var(--constellation-color-text-muted);
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    font-style: italic;
    line-height: 1.5;
  }

  .history-error,
  .policy-error {
    color: var(--constellation-color-danger);
    font-size: 12px;
  }

  .policy-error {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px;
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

  .effective-policy.compact .policy-values {
    grid-template-columns: 1fr;
  }

  .effective-policy.compact .editor-fields,
  .effective-policy.compact .diff-columns,
  .effective-policy.compact .guidance-diff {
    grid-template-columns: 1fr;
  }

  .effective-policy.compact .editor-heading {
    flex-direction: column;
  }

  .effective-policy.compact .editor-heading p {
    max-width: none;
    text-align: left;
  }

  .effective-policy.compact .policy-value,
  .effective-policy.compact .policy-value:nth-child(odd) {
    border-right: 0;
  }

  @keyframes policy-pulse {
    from { background-position: 200% 0; }
    to { background-position: -200% 0; }
  }

  @media (max-width: 720px) {
    .policy-values {
      grid-template-columns: 1fr;
    }

    .policy-value,
    .policy-value:nth-child(odd) {
      border-right: 0;
    }

    .active-heading,
    .active-actions,
    .editor-heading,
    .history-change header {
      align-items: flex-start;
      flex-direction: column;
    }

    .active-status {
      justify-items: start;
    }

    .editor-heading p {
      max-width: none;
      text-align: left;
    }

    .editor-fields,
    .diff-columns,
    .guidance-diff {
      grid-template-columns: 1fr;
    }

    .guidance-editor-heading,
    .guidance-editor-list li {
      grid-template-columns: 1fr;
    }

    .guidance-editor-heading {
      align-items: flex-start;
      flex-direction: column;
    }

    .history-change-actions {
      justify-items: start;
    }

    .history-change-actions > span {
      max-width: none;
      text-align: left;
    }
  }
</style>
