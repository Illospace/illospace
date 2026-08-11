import test from 'node:test';
import assert from 'node:assert/strict';

import {
  formatPolicyDateTime,
  guidanceDiff,
  hydratePolicyDraft,
  isPolicyDraftDirty,
  policyConfigurationEntries,
  policyFieldSource,
  policyProposalFromDraft,
  presentedPolicyDiff,
  recoverPolicyDraftAfterConflict,
  retiredGuidance,
  shouldConfirmPolicyDraftDiscard,
  validatePolicyDraft,
} from '../features/cycles/domain/effectivePolicy.ts';

function effectivePolicy(overrides = {}) {
  const configuration = {
    name: 'Morning review',
    prompt: 'Review current priorities.',
    schedule_expr: '0 9 * * *',
    schedule_human: 'Every day at 9:00 AM (UTC)',
    timezone: 'UTC',
    enabled: true,
    max_concurrency: 1,
    timeout_seconds: null,
    retry_policy: {},
    model_override: null,
    thinking_override: 'high',
    execution_policy_key: null,
    target_idea_id: null,
    ...overrides.configuration,
  };
  return {
    workspace_id: 'workspace-1',
    policy_kind: 'cycle',
    target_type: 'cycle',
    target_id: '1',
    version: overrides.version ?? 1,
    revision_id: 10,
    configuration,
    guidance: overrides.guidance ?? ['Keep reports concise'],
    editable_fields: ['prompt', 'schedule_expr', 'timezone', 'enabled', 'model_override', 'thinking_override', 'guidance'],
    output_targets: [],
    output_targets_read_only: true,
    source: {
      revision_id: 10,
      actor_type: 'human',
      actor_id: 'reviewer',
      rationale: 'Initial behavior.',
      source_reference: 'api:/cycles/1/behavior-policy',
      changed_at: '2026-08-11T13:00:00Z',
    },
    field_sources: {},
    latest_change: null,
  };
}

test('renders every configuration field supplied by the typed policy object', () => {
  const configuration = {
    name: 'Morning review',
    prompt: 'Review current priorities.',
    schedule_expr: '0 9 * * *',
    schedule_human: 'Every day at 9:00 AM',
    timezone: 'UTC',
    enabled: true,
    max_concurrency: 1,
    timeout_seconds: null,
    retry_policy: {},
    model_override: null,
    thinking_override: 'high',
    execution_policy_key: null,
    target_idea_id: null,
  };

  assert.deepEqual(
    policyConfigurationEntries(configuration).map(({ key }) => key),
    Object.keys(configuration),
  );
});

test('uses the schedule rule provenance for its derived human label', () => {
  const scheduleSource = {
    version: 2,
    cycle_revision_id: 10,
    actor_type: 'human',
    actor_id: 'reviewer',
    source_reference: 'api:/cycles/1/behavior-policy',
    rationale: 'Move the morning review.',
    changed_at: '2026-08-11T13:00:00Z',
    change_id: 9,
  };

  assert.equal(policyFieldSource({ schedule_expr: scheduleSource }, 'schedule_human'), scheduleSource);
});

test('keeps removed guidance in history without treating unchanged guidance as retired', () => {
  const change = {
    before_snapshot: { guidance: ['Stay concise', 'Use old CRM copy'] },
    after_snapshot: { guidance: ['Stay concise', 'Use approved CRM copy'] },
  };

  assert.deepEqual(retiredGuidance(change), ['Use old CRM copy']);
});

test('formats UTC API timestamps in the display timezone', () => {
  assert.match(
    formatPolicyDateTime('2026-08-11T16:00:00Z', 'America/Toronto', 'en-CA'),
    /12:00 p\.m\./,
  );
});

test('hydrates an isolated editable draft from the effective policy', () => {
  const policy = effectivePolicy({
    configuration: { model_override: 'openai/gpt-5.2', enabled: false },
    guidance: ['Keep reports concise', 'Name the owner'],
  });
  const draft = hydratePolicyDraft(policy);

  assert.deepEqual(draft, {
    prompt: 'Review current priorities.',
    schedule_expr: '0 9 * * *',
    timezone: 'UTC',
    enabled: false,
    model_override: 'openai/gpt-5.2',
    thinking_override: 'high',
    guidance: ['Keep reports concise', 'Name the owner'],
  });
  draft.guidance[0] = 'Changed draft text';
  assert.equal(policy.guidance[0], 'Keep reports concise');
  assert.deepEqual(Object.keys(policyProposalFromDraft(draft)), [
    'prompt',
    'schedule_expr',
    'timezone',
    'enabled',
    'model_override',
    'thinking_override',
    'guidance',
  ]);
});

test('presents schedule evidence as stored cron plus human labels and guidance as retire plus add', () => {
  const preview = {
    diff: [
      {
        field: 'schedule_expr',
        kind: 'schedule',
        before: { schedule_expr: '0 9 * * *', schedule_human: 'Daily at 9 AM', timezone: 'UTC' },
        after: { schedule_expr: '30 10 * * 1', schedule_human: 'Mondays at 10:30 AM', timezone: 'America/Toronto' },
        added: null,
        removed: null,
      },
      {
        field: 'timezone',
        kind: 'schedule',
        before: { schedule_expr: '0 9 * * *', schedule_human: 'Daily at 9 AM', timezone: 'UTC' },
        after: { schedule_expr: '30 10 * * 1', schedule_human: 'Mondays at 10:30 AM', timezone: 'America/Toronto' },
        added: null,
        removed: null,
      },
      {
        field: 'guidance',
        kind: 'collection',
        before: ['Old wording'],
        after: ['New wording'],
        added: ['New wording'],
        removed: ['Old wording'],
      },
    ],
  };

  assert.deepEqual(presentedPolicyDiff(preview), [
    {
      key: 'schedule',
      kind: 'schedule',
      field: 'schedule',
      label: 'Schedule',
      before: { schedule_expr: '0 9 * * *', schedule_human: 'Daily at 9 AM', timezone: 'UTC' },
      after: { schedule_expr: '30 10 * * 1', schedule_human: 'Mondays at 10:30 AM', timezone: 'America/Toronto' },
    },
    {
      key: 'guidance',
      kind: 'guidance',
      field: 'guidance',
      label: 'Guidance',
      added: ['New wording'],
      retired: ['Old wording'],
    },
  ]);
  assert.deepEqual(guidanceDiff(preview.diff[2]), {
    added: ['New wording'],
    retired: ['Old wording'],
  });
});

test('keeps a dirty draft intact when conflict recovery adopts the latest live policy', () => {
  const original = effectivePolicy();
  const latest = effectivePolicy({
    version: 2,
    configuration: { prompt: 'Another person changed this.' },
  });
  const draft = hydratePolicyDraft(original);
  draft.prompt = 'My unsubmitted draft.';

  const recovered = recoverPolicyDraftAfterConflict(draft, latest);

  assert.equal(recovered.draft.prompt, 'My unsubmitted draft.');
  assert.equal(recovered.policy.version, 2);
  assert.equal(recovered.policy.configuration.prompt, 'Another person changed this.');
  assert.equal(isPolicyDraftDirty(recovered.draft, recovered.policy), true);
});

test('asks for dirty-draft protection only after a field changes', () => {
  const policy = effectivePolicy();
  const draft = hydratePolicyDraft(policy);
  assert.equal(shouldConfirmPolicyDraftDiscard(draft, policy), false);
  draft.guidance.push('New guidance');
  assert.equal(shouldConfirmPolicyDraftDiscard(draft, policy), true);
});

test('rejects invalid schedules, empty missions, unsupported models, and duplicate guidance in the client', () => {
  const draft = hydratePolicyDraft(effectivePolicy());
  draft.prompt = '   ';
  draft.schedule_expr = 'not a schedule';
  draft.model_override = 'openai/not-a-model';
  draft.guidance = ['Repeat this', ' Repeat this '];

  assert.deepEqual(validatePolicyDraft(draft, [{ id: 'openai/gpt-5.2' }]), {
    prompt: 'Mission prompt is required.',
    schedule_expr: 'Use a valid five-field cron rule or one-time at: timestamp.',
    model_override: 'Select a supported model.',
    guidance: 'Guidance entries must be unique.',
  });
});
