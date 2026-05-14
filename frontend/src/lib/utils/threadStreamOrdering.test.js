import test from 'node:test';
import assert from 'node:assert/strict';

import { orderQueuedThreadStreamItems } from './threadStreamOrdering.ts';

test('places queued follow-up messages after the run reply they are waiting for', () => {
  const ordered = orderQueuedThreadStreamItems([
    { type: 'message', id: 'prompt-1', role: 'user', content: 'Build the app' },
    { type: 'run', id: 'run-1', run_id: 1, status: 'completed' },
    {
      type: 'message',
      id: 'queued-message',
      role: 'user',
      content: 'Add JSONPlaceholder sync',
      metadata: { queued_after_run: true, queued_after_run_id: 1 },
    },
    {
      type: 'run',
      id: 'run-2',
      run_id: 2,
      status: 'completed',
      metadata: { queued_after_run_id: 1, thread_message_id: 'queued-message' },
    },
    {
      type: 'message',
      id: 'run-1-final',
      role: 'illo',
      content: 'Built it.',
      metadata: { run_id: 1 },
    },
    {
      type: 'message',
      id: 'run-2-final',
      role: 'illo',
      content: 'Wired the sync button.',
      metadata: { run_id: 2 },
    },
  ]);

  assert.deepEqual(ordered.map((item) => item.id), [
    'prompt-1',
    'run-1',
    'run-1-final',
    'queued-message',
    'run-2',
    'run-2-final',
  ]);
});

test('keeps queued follow-ups after the active run card until a final reply exists', () => {
  const ordered = orderQueuedThreadStreamItems([
    { type: 'message', id: 'prompt-1', role: 'user', content: 'Build the app' },
    {
      type: 'message',
      id: 'queued-message',
      role: 'user',
      content: 'Add JSONPlaceholder sync',
      metadata: { queued_after_run: true, queued_after_run_id: 1 },
    },
    { type: 'run', id: 'run-1', run_id: 1, status: 'running' },
    {
      type: 'run',
      id: 'run-2',
      run_id: 2,
      status: 'queued',
      metadata: { queued_after_run_id: 1, thread_message_id: 'queued-message' },
    },
  ]);

  assert.deepEqual(ordered.map((item) => item.id), [
    'prompt-1',
    'run-1',
    'queued-message',
    'run-2',
  ]);
});

test('preserves unanchored queued messages in timestamp order', () => {
  const ordered = orderQueuedThreadStreamItems([
    { type: 'message', id: 'prompt-1', role: 'user', content: 'Build the app' },
    {
      type: 'message',
      id: 'queued-message',
      role: 'user',
      content: 'Add JSONPlaceholder sync',
      metadata: { queued_after_run: true, queued_after_run_id: 99 },
    },
    { type: 'run', id: 'run-1', run_id: 1, status: 'completed' },
  ]);

  assert.deepEqual(ordered.map((item) => item.id), ['prompt-1', 'queued-message', 'run-1']);
});
