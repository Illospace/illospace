import test from 'node:test';
import assert from 'node:assert/strict';

import {
  hasWorkingIdeas,
  normalizeIdea,
  normalizeIdeaStatus,
} from '../features/cortex/domain/ideaReducer.ts';
import { DONE_IDEA_STATUSES, WORKING_IDEA_STATUSES } from '../constants/statuses.ts';

test('does not treat active ideas without runs as working', () => {
  assert.equal(normalizeIdeaStatus('active'), 'idle');
  assert.equal(normalizeIdea({ id: 'idea-1', status: 'active' }).status, 'idle');
  assert.equal(hasWorkingIdeas([{ id: 'idea-1', status: 'active' }]), false);
});

test('keeps queued and running run states working', () => {
  assert.equal(normalizeIdeaStatus('queued'), 'working');
  assert.equal(normalizeIdeaStatus('working'), 'working');
  assert.equal(normalizeIdeaStatus('running'), 'working');
  assert.equal(hasWorkingIdeas([{ id: 'idea-1', status: 'queued' }]), true);
});

test('normalizes idea statuses from the shared frontend groups', () => {
  for (const status of WORKING_IDEA_STATUSES) {
    assert.equal(normalizeIdeaStatus(status), 'working');
  }
  for (const status of DONE_IDEA_STATUSES) {
    assert.equal(normalizeIdeaStatus(status), 'done');
  }
});
