import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  applyPolicyReview,
  hydratePolicyDraft,
  reviewPolicyDraft,
  reviewPolicyRevert,
} from '../features/cycles/domain/effectivePolicy.ts';

function configuration(overrides = {}) {
  return {
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
    ...overrides,
  };
}

function policy(version = 1, overrides = {}) {
  return {
    workspace_id: 'workspace-1',
    policy_kind: 'cycle',
    target_type: 'cycle',
    target_id: '7',
    version,
    revision_id: 10 + version,
    configuration: configuration(overrides.configuration),
    guidance: overrides.guidance ?? ['Keep reports concise'],
    editable_fields: ['prompt', 'schedule_expr', 'timezone', 'enabled', 'model_override', 'thinking_override', 'guidance'],
    output_targets: [],
    output_targets_read_only: true,
    source: {
      revision_id: 10 + version,
      actor_type: 'human',
      actor_id: 'reviewer',
      rationale: 'Approved behavior.',
      source_reference: 'api:/cycles/7/behavior-policy',
      changed_at: '2026-08-11T13:00:00Z',
    },
    field_sources: {},
    latest_change: null,
  };
}

function snapshot(nextPolicy = policy()) {
  return {
    configuration: nextPolicy.configuration,
    guidance: nextPolicy.guidance,
  };
}

function preview(overrides = {}) {
  const beforePolicy = policy(1);
  const afterPolicy = policy(1, { configuration: { prompt: 'Review incidents and owners.' } });
  return {
    expected_version: 1,
    preview_digest: 'a'.repeat(64),
    before: snapshot(beforePolicy),
    after: snapshot(afterPolicy),
    changed_fields: ['prompt'],
    diff: [{
      field: 'prompt',
      kind: 'value',
      before: beforePolicy.configuration.prompt,
      after: afterPolicy.configuration.prompt,
      added: null,
      removed: null,
    }],
    warnings: [{ code: 'admitted_runs_unchanged', message: 'Active runs are unchanged.' }],
    affected_runs: { admitted_runs: 'unchanged', future_runs: 'use_proposed_policy_after_apply' },
    reverted_from_id: null,
    ...overrides,
  };
}

function history(items = []) {
  return {
    items,
    pagination: { limit: 50, offset: 0, has_more: false, next_offset: null },
  };
}

function client(overrides = {}) {
  return {
    previewCycleBehaviorPolicy: async () => preview(),
    applyCycleBehaviorPolicy: async () => ({ effective_policy: policy(2), change: {} }),
    previewCycleBehaviorPolicyRevert: async () => preview({ reverted_from_id: 41 }),
    applyCycleBehaviorPolicyRevert: async () => ({ effective_policy: policy(2), change: {} }),
    getCycleBehaviorPolicy: async () => policy(2),
    getCycleBehaviorPolicyHistory: async () => history([{}]),
    ...overrides,
  };
}

test('requires review before apply and refuses apply without a rationale', async () => {
  let applyCalls = 0;
  const api = client({
    applyCycleBehaviorPolicy: async () => {
      applyCalls += 1;
      return { effective_policy: policy(2), change: {} };
    },
  });

  await assert.rejects(
    applyPolicyReview(api, 7, null, 'Required reason'),
    /Review the change before applying it/,
  );
  await assert.rejects(
    applyPolicyReview(api, 7, { kind: 'edit', proposal: {}, preview: preview() }, '   '),
    /Rationale is required/,
  );
  assert.equal(applyCalls, 0);
});

test('reviews a valid draft before apply and refreshes both effective policy and history after success', async () => {
  const calls = [];
  const api = client({
    previewCycleBehaviorPolicy: async (_cycleId, body) => {
      calls.push(['preview', body]);
      return preview();
    },
    applyCycleBehaviorPolicy: async (_cycleId, body) => {
      calls.push(['apply', body]);
      return { effective_policy: policy(2), change: {} };
    },
    getCycleBehaviorPolicy: async () => {
      calls.push(['refresh-policy']);
      return policy(2);
    },
    getCycleBehaviorPolicyHistory: async () => {
      calls.push(['refresh-history']);
      return history([{}]);
    },
  });
  const draft = hydratePolicyDraft(policy());
  draft.prompt = 'Review incidents and owners.';

  const reviewed = await reviewPolicyDraft(api, 7, draft, [], null);
  assert.ok(reviewed.review);
  assert.deepEqual(calls.map(([name]) => name), ['preview']);

  const result = await applyPolicyReview(api, 7, reviewed.review, 'Keep incident ownership current.');
  assert.equal(result.policy.version, 2);
  assert.equal(result.history.items.length, 1);
  assert.deepEqual(calls.map(([name]) => name), [
    'preview',
    'apply',
    'refresh-policy',
    'refresh-history',
  ]);
});

test('client validation stops an invalid draft before the preview or apply API', async () => {
  let previewCalls = 0;
  const api = client({
    previewCycleBehaviorPolicy: async () => {
      previewCalls += 1;
      return preview();
    },
  });
  const draft = hydratePolicyDraft(policy());
  draft.schedule_expr = 'bad cron';

  const reviewed = await reviewPolicyDraft(api, 7, draft, [], null);

  assert.equal(reviewed.review, null);
  assert.match(reviewed.errors.schedule_expr, /valid five-field cron/);
  assert.equal(previewCalls, 0);
});

test('revert requires explicit confirmation before it applies a new version', async () => {
  const calls = [];
  const api = client({
    previewCycleBehaviorPolicyRevert: async (_cycleId, changeId) => {
      calls.push(['revert-preview', changeId]);
      return preview({ reverted_from_id: changeId });
    },
    applyCycleBehaviorPolicyRevert: async (_cycleId, changeId) => {
      calls.push(['revert-apply', changeId]);
      return { effective_policy: policy(2), change: {} };
    },
  });
  const reviewed = await reviewPolicyRevert(api, 7, 41);

  const cancelled = await applyPolicyReview(api, 7, reviewed, 'Restore the known-good behavior.', () => false);
  assert.equal(cancelled, null);
  assert.deepEqual(calls, [['revert-preview', 41]]);

  await applyPolicyReview(api, 7, reviewed, 'Restore the known-good behavior.', () => true);
  assert.deepEqual(calls.slice(0, 2), [['revert-preview', 41], ['revert-apply', 41]]);
});

test('the shipped editor keeps apply disabled without rationale and states the active-run boundary', () => {
  const source = readFileSync(
    new URL('../features/cycles/components/EffectiveCyclePolicyView.svelte', import.meta.url),
    'utf8',
  );

  assert.match(source, /disabled=\{applyDisabled\}/);
  assert.match(source, /Active runs are unchanged\. Future runs use this policy only after apply\./);
  assert.match(source, /beforeunload/);
  assert.match(source, /Your draft is safe/);
});

test('existing Cycle surfaces no longer expose a direct behavior update path', () => {
  const mainPage = readFileSync(
    new URL('../../routes/cycles/+page.svelte', import.meta.url),
    'utf8',
  );
  const threadPane = readFileSync(
    new URL('../features/cycles/components/ThreadCyclesPane.svelte', import.meta.url),
    'utf8',
  );

  assert.doesNotMatch(mainPage, /api\.updateCycle/);
  assert.doesNotMatch(threadPane, /api\.updateCycle/);
  assert.match(mainPage, /editable=\{!isCyclesPreview\}/);
  assert.match(threadPane, /<EffectiveCyclePolicyView[\s\S]*?editable/);
});
