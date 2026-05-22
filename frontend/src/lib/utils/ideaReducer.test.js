import test from 'node:test';
import assert from 'node:assert/strict';

import {
  hasWorkingIdeas,
  normalizeIdea,
  normalizeIdeaStatus,
} from '../features/cortex/domain/ideaReducer.ts';

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
