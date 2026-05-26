import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CORTEX_THREAD_STAGE_RUN_STATUSES,
  FAST_TRANSCRIPT_VISIBLE_RUN_STATUSES,
  normalizeAgentRunStatus,
} from '../constants/statuses.ts';

test('normalizes every backend run status deliberately', () => {
  const expected = {
    queued: 'queued',
    starting: 'starting',
    running: 'running',
    paused: 'running',
    verifying: 'running',
    completed: 'completed',
    failed: 'failed',
    canceled: 'canceled',
    expired: 'expired',
    cancelled: 'canceled',
    error: 'failed',
    blocked: 'failed',
    timeout: 'failed',
    superseded: 'canceled',
  };

  for (const [status, normalized] of Object.entries(expected)) {
    assert.equal(normalizeAgentRunStatus(status), normalized);
  }
  assert.equal(normalizeAgentRunStatus('unknown'), 'queued');
});

test('frontend run presentation covers terminal backend statuses', () => {
  assert.ok(CORTEX_THREAD_STAGE_RUN_STATUSES.includes('expired'));
  assert.ok(FAST_TRANSCRIPT_VISIBLE_RUN_STATUSES.includes('expired'));
});
