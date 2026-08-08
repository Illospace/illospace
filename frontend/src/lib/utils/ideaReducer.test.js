import test from 'node:test';
import assert from 'node:assert/strict';

import {
  hasWorkingIdeas,
  normalizeIdea,
  normalizeIdeaStatus,
} from '../features/cortex/domain/ideaReducer.ts';
import { canvasOriginCue, isCanvasOccupant } from '../features/cortex/domain/canvasOccupancy.ts';
import { DONE_IDEA_STATUSES, WORKING_IDEA_STATUSES } from '../constants/statuses.ts';

test('does not treat active ideas without runs as working', () => {
  assert.equal(normalizeIdeaStatus('active'), 'idle');
  const normalized = normalizeIdea({ id: 'idea-1', status: 'active' });
  assert.equal(normalized.status, 'idle');
  assert.equal(normalized.lifecycle_status, 'active');
  assert.equal(isCanvasOccupant(normalized), true);
  assert.equal(hasWorkingIdeas([{ id: 'idea-1', status: 'active' }]), false);
});

test('keeps every canonical canvas lifecycle state after visual normalization', () => {
  for (const status of ['active', 'failed', 'needs_input', 'unread_reply', 'working']) {
    const normalized = normalizeIdea({ id: `idea-${status}`, status });
    assert.equal(normalized.lifecycle_status, status);
    assert.equal(isCanvasOccupant(normalized), true);
  }
  for (const status of ['emerged', 'completed', 'archived']) {
    assert.equal(isCanvasOccupant(normalizeIdea({ id: `idea-${status}`, status })), false);
  }
  assert.equal(isCanvasOccupant({ status: 'idle' }), true);
  assert.equal(isCanvasOccupant({ status: 'idle', archived_at: '2026-08-08T00:00:00Z' }), false);
});

test('uses clear canvas cues for live idea origins', () => {
  assert.equal(canvasOriginCue('cycle_run'), 'CYCLE');
  assert.equal(canvasOriginCue('inbound_signal'), 'INBOUND');
  assert.equal(canvasOriginCue('illo_created'), 'ILLO');
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
