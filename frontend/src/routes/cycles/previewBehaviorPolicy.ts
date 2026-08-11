import type {
  CyclePolicyChangeRead,
  CyclePolicyConfigurationRead,
  CyclePolicyDiffEntryRead,
  CyclePolicyHistoryRead,
  CyclePolicyPreviewRead,
  CyclePolicyProposal,
  CyclePolicySnapshotRead,
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
  getHistory: () => CyclePolicyHistoryRead;
  commit: (policy: EffectiveCyclePolicyRead, history: CyclePolicyHistoryRead) => void;
  scheduleLabel: (scheduleExpression: string, timezone: string) => string;
  now?: () => string;
};

type PendingPreview = {
  preview: CyclePolicyPreviewRead;
  revertedFromId: number | null;
};

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
    const history = options.getHistory();
    const version = policy.version + 1;
    const revisionId = (policy.revision_id ?? 0) + 1;
    const changeId = Math.max(0, ...history.items.map((change) => change.id)) + 1;
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
    const nextHistory: CyclePolicyHistoryRead = {
      items: [change, ...history.items],
      pagination: {
        ...history.pagination,
        offset: 0,
        has_more: false,
        next_offset: null,
      },
    };
    options.commit(nextPolicy, nextHistory);
    pending.delete(previewDigest);
    return { effective_policy: clonePlainData(nextPolicy), change: clonePlainData(change) };
  }

  const client: CyclePolicyEditorApi = {
    getCycleBehaviorPolicy: async () => clonePlainData(options.getPolicy()),
    getCycleBehaviorPolicyHistory: async (_cycleId, limit = 50, offset = 0) => {
      const history = options.getHistory();
      const items = history.items.slice(offset, offset + limit);
      const nextOffset = offset + items.length;
      return {
        items: clonePlainData(items),
        pagination: {
          limit,
          offset,
          has_more: nextOffset < history.items.length,
          next_offset: nextOffset < history.items.length ? nextOffset : null,
        },
      };
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
      const change = options.getHistory().items.find((item) => item.id === changeId);
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
