import test from 'node:test';
import assert from 'node:assert/strict';

import {
  formatPolicyDateTime,
  policyConfigurationEntries,
  policyFieldSource,
  retiredGuidance,
} from '../features/cycles/domain/effectivePolicy.ts';

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
