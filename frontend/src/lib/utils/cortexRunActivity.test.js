import test from 'node:test';
import assert from 'node:assert/strict';

import { activeRootRunCountsByIdea, isActiveRootRun } from './cortexRunActivity.ts';

test('counts active root runs by idea', () => {
  const counts = activeRootRunCountsByIdea([
    { id: 1, root_run_id: 1, idea_id: 'idea-1', status: 'running' },
    { id: 2, root_run_id: 2, idea_id: 'idea-1', status: 'queued' },
    { id: 3, root_run_id: 3, idea_id: 'idea-2', status: 'pending_approval' },
  ]);

  assert.equal(counts.get('idea-1'), 2);
  assert.equal(counts.get('idea-2'), 1);
});

test('ignores active child worker runs', () => {
  const childRun = { id: 2, root_run_id: 1, parent_run_id: 1, idea_id: 'idea-1', status: 'running' };

  assert.equal(isActiveRootRun(childRun), false);
  assert.equal(activeRootRunCountsByIdea([childRun]).has('idea-1'), false);
});

test('ignores terminal root runs', () => {
  const counts = activeRootRunCountsByIdea([
    { id: 1, root_run_id: 1, idea_id: 'idea-1', status: 'completed' },
    { id: 2, root_run_id: 2, idea_id: 'idea-2', status: 'failed' },
    { id: 3, root_run_id: 3, idea_id: 'idea-3', status: 'canceled' },
  ]);

  assert.equal(counts.size, 0);
});

test('uses thread_id as a fallback when ops snapshots omit idea_id', () => {
  const counts = activeRootRunCountsByIdea([
    { run_id: 'run-1', root_run_id: 'run-1', thread_id: 'idea-1', status: 'verifying' },
  ]);

  assert.equal(counts.get('idea-1'), 1);
});
