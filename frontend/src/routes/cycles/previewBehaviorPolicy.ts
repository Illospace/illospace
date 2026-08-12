import type {
  CyclePolicyChangeRead,
  CyclePolicyConfigurationRead,
  CyclePolicyDiffEntryRead,
  CyclePolicyFieldSourceRead,
  CyclePolicyHistoryRead,
  CyclePolicyPreviewRead,
  CyclePolicyProposal,
  CyclePolicySnapshotRead,
  CycleRead,
  CycleRunRead,
  EffectiveCyclePolicyRead,
} from '$lib/api/client';

import { clonePlainData } from '../../lib/utils/postMessageClone.ts';

import {
  POLICY_FIELD_SCHEMA,
  type CyclePolicyEditorApi,
  type CyclePolicyFieldKey,
} from '../../lib/features/cycles/domain/effectivePolicy.ts';

type PreviewPolicyClientOptions = {
  cycleId: number;
  getPolicy: () => EffectiveCyclePolicyRead;
  historyItems: readonly CyclePolicyChangeRead[];
  historyPageSize?: number;
  commit: (policy: EffectiveCyclePolicyRead, history: CyclePolicyHistoryRead) => void;
  scheduleLabel: (scheduleExpression: string, timezone: string) => string;
  now?: () => string;
};

type PendingPreview = {
  preview: CyclePolicyPreviewRead;
  revertedFromId: number | null;
};

type PreviewBehaviorPolicyFixtureOptions = {
  humanChangedAt: string;
  agentChangedAt: string;
  originatingAgentRunId: number;
};

function historyPage(
  historyItems: readonly CyclePolicyChangeRead[],
  limit: number,
  offset = 0,
): CyclePolicyHistoryRead {
  const items = historyItems.slice(offset, offset + limit);
  const nextOffset = offset + items.length;
  return {
    items: clonePlainData(items),
    pagination: {
      limit,
      offset,
      has_more: nextOffset < historyItems.length,
      next_offset: nextOffset < historyItems.length ? nextOffset : null,
    },
  };
}

export function createPreviewBehaviorPolicyFixture(
  cycle: CycleRead,
  options: PreviewBehaviorPolicyFixtureOptions,
): {
  policy: EffectiveCyclePolicyRead;
  history: CyclePolicyHistoryRead;
  historyItems: CyclePolicyChangeRead[];
} {
  const configuration: CyclePolicyConfigurationRead = {
    name: cycle.name,
    prompt: cycle.prompt,
    schedule_expr: cycle.schedule_expr,
    schedule_human: cycle.schedule_human,
    timezone: cycle.timezone,
    enabled: cycle.enabled,
    max_concurrency: 1,
    timeout_seconds: null,
    retry_policy: { max_attempts: 2 },
    model_override: cycle.model_override,
    thinking_override: cycle.thinking_override,
    execution_policy_key: cycle.execution_policy_key,
    target_idea_id: cycle.target_idea_id,
  };
  const priorConfiguration = {
    ...configuration,
    prompt: 'Review current priorities, summarize the most important items, and continue in the planning thread.',
  };
  const activeGuidance = [
    'Use the current workspace state as the source of truth.',
    'Keep the result concise and name any blocker that needs attention.',
  ];
  const retiredGuidance = 'Use the legacy priority list before reviewing the workspace.';
  const initialChangeId = 5000 + cycle.id;
  const humanChangeId = 5100 + cycle.id;
  const agentChangeId = 5200 + cycle.id;
  const initialRevisionId = 4000 + cycle.id;
  const humanRevisionId = 4100 + cycle.id;
  const agentRevisionId = 4200 + cycle.id;
  const humanSource = {
    version: 2,
    cycle_revision_id: humanRevisionId,
    actor_type: 'user',
    actor_id: 'preview-user',
    source_reference: `api:/cycles/${cycle.id}/behavior-policy`,
    rationale: 'Replace guidance that used an old priority list.',
    changed_at: options.humanChangedAt,
    change_id: humanChangeId,
  } satisfies CyclePolicyFieldSourceRead;
  const agentSource = {
    version: 3,
    cycle_revision_id: agentRevisionId,
    actor_type: 'agent',
    actor_id: String(options.originatingAgentRunId),
    source_reference: `agent:${options.originatingAgentRunId}`,
    rationale: 'Focused the request after reviewing the prior Cycle run.',
    changed_at: options.agentChangedAt,
    change_id: agentChangeId,
  } satisfies CyclePolicyFieldSourceRead;
  const fieldSources = Object.fromEntries(
    [...Object.keys(configuration), 'guidance'].map((field) => [field, { ...humanSource }]),
  ) as Record<string, CyclePolicyFieldSourceRead>;
  fieldSources.prompt = { ...agentSource };

  const humanChange: CyclePolicyChangeRead = {
    id: humanChangeId,
    version: 2,
    actor_type: humanSource.actor_type,
    actor_id: humanSource.actor_id,
    source_reference: humanSource.source_reference,
    rationale: humanSource.rationale,
    changed_fields: ['guidance'],
    applied_at: options.humanChangedAt,
    reverted_from_id: null,
    workspace_id: 'preview-org',
    policy_kind: 'cycle',
    target_type: 'cycle',
    target_id: String(cycle.id),
    before_snapshot: {
      configuration: clonePlainData(priorConfiguration),
      guidance: [...activeGuidance, retiredGuidance],
    },
    after_snapshot: {
      configuration: clonePlainData(priorConfiguration),
      guidance: [...activeGuidance],
    },
    cycle_revision_id: humanRevisionId,
  };
  const initialChange: CyclePolicyChangeRead = {
    id: initialChangeId,
    version: 1,
    actor_type: 'system',
    actor_id: 'cycle-import',
    source_reference: `cycle:${cycle.id}:initial`,
    rationale: 'Created the initial Cycle behavior.',
    changed_fields: [
      'prompt',
      'schedule_expr',
      'timezone',
      'enabled',
      'model_override',
      'thinking_override',
      'guidance',
    ],
    applied_at: cycle.created_at,
    reverted_from_id: null,
    workspace_id: 'preview-org',
    policy_kind: 'cycle',
    target_type: 'cycle',
    target_id: String(cycle.id),
    before_snapshot: {
      configuration: clonePlainData(priorConfiguration),
      guidance: [],
    },
    after_snapshot: {
      configuration: clonePlainData(priorConfiguration),
      guidance: [...activeGuidance, retiredGuidance],
    },
    cycle_revision_id: initialRevisionId,
  };
  const agentChange: CyclePolicyChangeRead = {
    id: agentChangeId,
    version: 3,
    actor_type: agentSource.actor_type,
    actor_id: agentSource.actor_id,
    source_reference: agentSource.source_reference,
    rationale: agentSource.rationale,
    changed_fields: ['prompt'],
    applied_at: options.agentChangedAt,
    reverted_from_id: null,
    workspace_id: 'preview-org',
    policy_kind: 'cycle',
    target_type: 'cycle',
    target_id: String(cycle.id),
    before_snapshot: {
      configuration: clonePlainData(priorConfiguration),
      guidance: [...activeGuidance],
    },
    after_snapshot: {
      configuration: clonePlainData(configuration),
      guidance: [...activeGuidance],
    },
    cycle_revision_id: agentRevisionId,
  };
  const policy: EffectiveCyclePolicyRead = {
    workspace_id: 'preview-org',
    policy_kind: 'cycle',
    target_type: 'cycle',
    target_id: String(cycle.id),
    version: 3,
    revision_id: agentRevisionId,
    configuration,
    guidance: activeGuidance,
    editable_fields: [
      'prompt',
      'schedule_expr',
      'timezone',
      'enabled',
      'model_override',
      'thinking_override',
      'guidance',
    ],
    output_targets: [
      {
        id: 6100 + cycle.id,
        target_type: 'cycle_ledger',
        target_id: String(cycle.id),
        label: 'Cycle ledger',
        config: { format: 'summary' },
        source_type: 'system',
        source_id: 'cycle-defaults',
        rationale: 'Keep a durable result for later review.',
        created_at: cycle.created_at,
        updated_at: options.agentChangedAt,
      },
    ],
    output_targets_read_only: true,
    source: {
      revision_id: agentRevisionId,
      actor_type: agentSource.actor_type,
      actor_id: agentSource.actor_id,
      rationale: agentSource.rationale,
      source_reference: agentSource.source_reference,
      changed_at: options.agentChangedAt,
    },
    field_sources: fieldSources,
    latest_change: {
      id: agentChange.id,
      version: agentChange.version,
      actor_type: agentChange.actor_type,
      actor_id: agentChange.actor_id,
      source_reference: agentChange.source_reference,
      rationale: agentChange.rationale,
      changed_fields: [...agentChange.changed_fields],
      applied_at: agentChange.applied_at,
      reverted_from_id: agentChange.reverted_from_id,
    },
  };
  const historyItems = [agentChange, humanChange, initialChange];
  return {
    policy,
    history: historyPage(historyItems, 2),
    historyItems,
  };
}

function encodedPreviewPolicySnapshot(snapshot: CyclePolicySnapshotRead) {
  const { schedule_human: _scheduleHuman, ...configuration } = snapshot.configuration;
  return {
    snapshot_version: 1,
    ...clonePlainData(configuration),
    guidance: [...snapshot.guidance],
  };
}

export function createPreviewCycleRunPolicyData(
  cycleRunId: number,
  change: CyclePolicyChangeRead,
): Pick<
  CycleRunRead,
  | 'revision_id'
  | 'guidance_snapshot'
  | 'output_targets_snapshot'
  | 'context_snapshot'
  | 'self_review_summary'
> {
  const configuration = change.after_snapshot.configuration;
  return {
    revision_id: change.cycle_revision_id,
    guidance_snapshot: change.after_snapshot.guidance.map((guidance, index) => ({
      id: cycleRunId * 10 + index,
      cycle_id: Number(change.target_id),
      revision_id: change.cycle_revision_id,
      source_type: change.actor_type,
      source_id: change.actor_id,
      guidance,
      rationale: change.rationale,
      is_active: true,
      created_at: change.applied_at,
    })),
    output_targets_snapshot: [],
    context_snapshot: {
      revision: {
        id: change.cycle_revision_id,
        cycle_id: Number(change.target_id),
        revision_number: change.version,
        source_type: change.actor_type,
        source_id: change.actor_id,
        rationale: change.rationale,
        name: configuration.name,
        prompt: configuration.prompt,
        schedule_expr: configuration.schedule_expr,
        timezone: configuration.timezone,
        enabled: configuration.enabled,
        model_override: configuration.model_override,
        thinking_override: configuration.thinking_override,
        execution_policy_key: configuration.execution_policy_key,
        target_idea_id: configuration.target_idea_id,
        context_policy: {},
        created_at: change.applied_at,
      },
      behavior_change: {
        id: change.id,
        workspace_id: change.workspace_id,
        policy_kind: change.policy_kind,
        target_type: change.target_type,
        target_id: change.target_id,
        version: change.version,
        actor_type: change.actor_type,
        actor_id: change.actor_id,
        source_reference: change.source_reference,
        rationale: change.rationale,
        before_snapshot: encodedPreviewPolicySnapshot(change.before_snapshot),
        after_snapshot: encodedPreviewPolicySnapshot(change.after_snapshot),
        changed_fields: [...change.changed_fields],
        cycle_revision_id: change.cycle_revision_id,
        applied_at: change.applied_at,
        reverted_from_id: change.reverted_from_id,
      },
      launch_context: {
        origin: 'cycle_scheduler',
        source: 'cycle_scheduler',
        run_kind: 'scheduled_digest',
      },
    },
    self_review_summary: 'Completed the review with the admitted policy unchanged.',
  };
}

function snapshot(policy: EffectiveCyclePolicyRead): CyclePolicySnapshotRead {
  return {
    configuration: clonePlainData(policy.configuration),
    guidance: [...policy.guidance],
  };
}

function snapshotField(
  policySnapshot: CyclePolicySnapshotRead,
  field: CyclePolicyFieldKey,
): unknown {
  if (field === 'guidance') return policySnapshot.guidance;
  return policySnapshot.configuration[field];
}

function sameValue(before: unknown, after: unknown): boolean {
  return JSON.stringify(before) === JSON.stringify(after);
}

function scheduleDiffValue(configuration: CyclePolicyConfigurationRead) {
  return {
    schedule_expr: configuration.schedule_expr,
    schedule_human: configuration.schedule_human,
    timezone: configuration.timezone,
  };
}

function semanticDiff(
  before: CyclePolicySnapshotRead,
  after: CyclePolicySnapshotRead,
): { changedFields: CyclePolicyFieldKey[]; diff: CyclePolicyDiffEntryRead[] } {
  const changedFields = POLICY_FIELD_SCHEMA
    .filter((field) => !sameValue(snapshotField(before, field.key), snapshotField(after, field.key)))
    .map((field) => field.key);
  const diff: CyclePolicyDiffEntryRead[] = [];

  if (changedFields.includes('schedule_expr') || changedFields.includes('timezone')) {
    diff.push({
      field: 'schedule_expr',
      kind: 'schedule',
      before: scheduleDiffValue(before.configuration),
      after: scheduleDiffValue(after.configuration),
      added: null,
      removed: null,
    });
  }

  for (const field of changedFields) {
    if (field === 'schedule_expr' || field === 'timezone') continue;
    const beforeValue = snapshotField(before, field);
    const afterValue = snapshotField(after, field);
    if (field === 'guidance') {
      const beforeGuidance = before.guidance;
      const afterGuidance = after.guidance;
      diff.push({
        field,
        kind: 'collection',
        before: beforeGuidance,
        after: afterGuidance,
        added: afterGuidance.filter((item) => !beforeGuidance.includes(item)),
        removed: beforeGuidance.filter((item) => !afterGuidance.includes(item)),
      });
      continue;
    }
    diff.push({
      field,
      kind: 'value',
      before: beforeValue as CyclePolicyDiffEntryRead['before'],
      after: afterValue as CyclePolicyDiffEntryRead['after'],
      added: null,
      removed: null,
    });
  }
  return { changedFields, diff };
}

function conflict(policy: EffectiveCyclePolicyRead): never {
  throw {
    status: 409,
    detail: {
      reason: 'version_conflict',
      latest_effective_policy: clonePlainData(policy),
    },
  };
}

export function createPreviewBehaviorPolicyClient(
  options: PreviewPolicyClientOptions,
): CyclePolicyEditorApi {
  let previewSerial = 0;
  let historyItems = clonePlainData(options.historyItems);
  const historyPageSize = options.historyPageSize ?? 50;
  const pending = new Map<string, PendingPreview>();

  function nextDigest(): string {
    previewSerial += 1;
    return String(previewSerial).padStart(64, '0');
  }

  function afterProposal(
    policy: EffectiveCyclePolicyRead,
    proposal: CyclePolicyProposal,
  ): CyclePolicySnapshotRead {
    const after = snapshot(policy);
    const configuration = after.configuration;
    const configurationFields = configuration as unknown as Record<string, unknown>;
    for (const field of POLICY_FIELD_SCHEMA) {
      if (field.key === 'guidance') {
        if (proposal.guidance !== undefined) after.guidance = [...(proposal.guidance ?? [])];
        continue;
      }
      const value = proposal[field.key];
      if (value !== undefined) configurationFields[field.key] = value;
    }
    configuration.schedule_human = options.scheduleLabel(
      configuration.schedule_expr,
      configuration.timezone,
    );
    return after;
  }

  function createPreview(
    policy: EffectiveCyclePolicyRead,
    after: CyclePolicySnapshotRead,
    revertedFromId: number | null,
  ): CyclePolicyPreviewRead {
    const before = snapshot(policy);
    const { changedFields, diff } = semanticDiff(before, after);
    const preview: CyclePolicyPreviewRead = {
      expected_version: policy.version,
      preview_digest: nextDigest(),
      before,
      after: clonePlainData(after),
      changed_fields: changedFields,
      diff,
      warnings: [{
        code: 'admitted_runs_unchanged',
        message: 'Active runs are unchanged.',
      }],
      affected_runs: {
        admitted_runs: 'unchanged',
        future_runs: 'use_proposed_policy_after_apply',
      },
      reverted_from_id: revertedFromId,
    };
    pending.set(preview.preview_digest, { preview, revertedFromId });
    return clonePlainData(preview);
  }

  function applyPending(
    expectedVersion: number,
    previewDigest: string,
    rationale: string,
  ) {
    const policy = options.getPolicy();
    if (policy.version !== expectedVersion) conflict(policy);
    const pendingPreview = pending.get(previewDigest);
    if (!pendingPreview || pendingPreview.preview.expected_version !== expectedVersion) {
      throw new Error('Review this behavior change again before apply.');
    }

    const appliedAt = options.now?.() ?? new Date().toISOString();
    const version = policy.version + 1;
    const revisionId = (policy.revision_id ?? 0) + 1;
    const changeId = Math.max(0, ...historyItems.map((change) => change.id)) + 1;
    const actor = {
      actor_type: 'human',
      actor_id: 'preview-user',
      source_reference: `preview:/cycles/${options.cycleId}/behavior-policy`,
    };
    const after = clonePlainData(pendingPreview.preview.after);
    const change: CyclePolicyChangeRead = {
      id: changeId,
      version,
      ...actor,
      rationale: rationale.trim(),
      changed_fields: [...pendingPreview.preview.changed_fields],
      applied_at: appliedAt,
      reverted_from_id: pendingPreview.revertedFromId,
      workspace_id: policy.workspace_id,
      policy_kind: policy.policy_kind,
      target_type: policy.target_type,
      target_id: policy.target_id,
      before_snapshot: clonePlainData(pendingPreview.preview.before),
      after_snapshot: after,
      cycle_revision_id: revisionId,
    };
    const fieldSources = { ...policy.field_sources };
    for (const field of change.changed_fields) {
      fieldSources[field] = {
        version,
        cycle_revision_id: revisionId,
        ...actor,
        rationale: rationale.trim(),
        changed_at: appliedAt,
        change_id: changeId,
      };
    }
    const nextPolicy: EffectiveCyclePolicyRead = {
      ...policy,
      version,
      revision_id: revisionId,
      configuration: clonePlainData(after.configuration),
      guidance: [...after.guidance],
      source: {
        revision_id: revisionId,
        ...actor,
        rationale: rationale.trim(),
        changed_at: appliedAt,
      },
      field_sources: fieldSources,
      latest_change: {
        id: change.id,
        version,
        ...actor,
        rationale: change.rationale,
        changed_fields: [...change.changed_fields],
        applied_at: appliedAt,
        reverted_from_id: change.reverted_from_id,
      },
    };
    historyItems = [change, ...historyItems.filter((item) => item.id !== change.id)];
    const nextHistory = historyPage(historyItems, historyPageSize);
    options.commit(nextPolicy, nextHistory);
    pending.delete(previewDigest);
    return { effective_policy: clonePlainData(nextPolicy), change: clonePlainData(change) };
  }

  const client: CyclePolicyEditorApi = {
    getCycleBehaviorPolicy: async () => clonePlainData(options.getPolicy()),
    getCycleBehaviorPolicyHistory: async (_cycleId, limit = 50, offset = 0) => {
      return historyPage(historyItems, limit, offset);
    },
    previewCycleBehaviorPolicy: async (_cycleId, { proposal }) => {
      const policy = options.getPolicy();
      return createPreview(policy, afterProposal(policy, proposal), null);
    },
    applyCycleBehaviorPolicy: async (_cycleId, request) => applyPending(
      request.expected_version,
      request.preview_digest,
      request.rationale,
    ),
    previewCycleBehaviorPolicyRevert: async (_cycleId, changeId) => {
      const policy = options.getPolicy();
      const change = historyItems.find((item) => item.id === changeId);
      if (!change) throw new Error('The selected history entry is not available.');
      return createPreview(policy, clonePlainData(change.before_snapshot), changeId);
    },
    applyCycleBehaviorPolicyRevert: async (_cycleId, _changeId, request) => applyPending(
      request.expected_version,
      request.preview_digest,
      request.rationale,
    ),
  };
  return client;
}
