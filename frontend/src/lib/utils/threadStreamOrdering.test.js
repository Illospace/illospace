import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildThreadStreamWindow,
  orderQueuedThreadStreamItems,
} from './threadStreamOrdering.ts';

function messages(count, start = 0) {
  return Array.from({ length: count }, (_, index) => ({
    type: 'message', id: `message-${start + index}`, role: 'user', content: `Message ${start + index}`,
  }));
}

const ids = (window) => window.items.map((item) => item.id);

test('windows newest 200 and repeated cursors reveal the complete stream', () => {
  const stream = messages(1000);
  let window = buildThreadStreamWindow(stream);
  const sizes = [window.items.length];
  assert.deepEqual(window.items, stream.slice(800));
  while (window.previousCursor) {
    assert.ok(window.previousCursor);
    window = buildThreadStreamWindow(stream, window.previousCursor);
    sizes.push(window.items.length);
  }
  assert.deepEqual(sizes, [200, 400, 600, 800, 1000]);
  assert.deepEqual(window.items, stream);
  assert.deepEqual(buildThreadStreamWindow(messages(80)).items, messages(80));
});

test('cursor survives clone, append, replacement, and mixed type identities', () => {
  const stream = messages(1000);
  const cursor = buildThreadStreamWindow(stream).previousCursor;
  const appended = [...stream.map((item) => ({ ...item })), ...messages(10, 1000)];
  assert.equal(buildThreadStreamWindow(appended, cursor).items[0].id, 'message-600');
  assert.equal(buildThreadStreamWindow(appended, cursor).items.at(-1).id, 'message-1009');
  const replaced = stream.map((item) => ({ ...item }));
  replaced[800] = { type: 'message', id: 'replacement' };
  assert.equal(buildThreadStreamWindow(replaced, cursor).items[0].id, 'message-600');
  const mixed = [{ type: 'run', id: 'shared', run_id: 'shared', status: 'completed' }, ...messages(400), { type: 'message', id: 'shared' }];
  assert.equal(buildThreadStreamWindow(mixed, { index: 401, key: 'message\u0000shared' }).items[0].id, 'message-200');
});

test('protects every live status, approval flag, and direct run dependency', () => {
  const statuses = ['starting', 'running', 'queued', 'paused', 'verifying', 'pending_approval', 'completed'];
  const protectedItems = statuses.flatMap((status) => {
    const runId = `run-${status}`;
    return [
      { type: 'message', id: `prompt-${status}` },
      { type: 'run', id: runId, run_id: runId, status, requires_approval: status === 'completed', metadata: { thread_message_id: `prompt-${status}` } },
      { type: 'message', id: `live-${status}`, metadata: { run_id: runId, live_agent_text: true } },
      { type: 'visual_block', id: `visual-${status}`, run_id: runId },
      { type: 'message', id: `steer-${status}`, metadata: { target_run_id: runId } },
    ];
  });
  const stream = [...protectedItems, { type: 'run', id: 'unrelated', status: 'completed' }, ...messages(220, 1000)];
  const visible = new Set(ids(buildThreadStreamWindow(stream)));
  for (const status of statuses) {
    for (const prefix of ['prompt', 'run', 'live', 'visual', 'steer']) {
      assert.equal(visible.has(`${prefix}-${status}`), true);
    }
  }
  assert.equal(visible.has('unrelated'), false);
});

test('excludes a queued run completed predecessor while retaining its direct prompt', () => {
  const stream = [
    { type: 'run', id: 'run-1', run_id: 1, status: 'completed' },
    { type: 'message', id: 'run-1-final', metadata: { run_id: 1 } },
    { type: 'message', id: 'queued-message', metadata: { queued_after_run_id: 1 } },
    { type: 'run', id: 'run-2', run_id: 2, status: 'queued', metadata: { queued_after_run_id: 1, thread_message_id: 'queued-message' } },
    ...messages(220, 1000),
  ];
  const visible = new Set(ids(buildThreadStreamWindow(stream)));
  assert.deepEqual([...visible].slice(0, 2), ['queued-message', 'run-2']);
  assert.equal(visible.has('run-1'), false);
  assert.equal(visible.has('run-1-final'), false);
});

test('one previous cursor crosses a retained protected span to reveal 200 hidden items', () => {
  const linked = Array.from({ length: 250 }, (_, index) => ({
    type: 'message', id: `live-${index}`, metadata: { run_id: 1 },
  }));
  const stream = [...messages(250), { type: 'run', id: 'run-1', run_id: 1, status: 'running' }, ...linked, ...messages(200, 1000)];
  const initial = buildThreadStreamWindow(stream);
  const expanded = buildThreadStreamWindow(stream, initial.previousCursor);
  const newlyVisible = expanded.items.filter((item) => !initial.items.includes(item) && !item.metadata?.run_id);
  assert.equal(newlyVisible.length, 200);
  assert.equal(newlyVisible[0].id, 'message-50');
  assert.ok(expanded.previousCursor);
});

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

test('places pre-tool live agent text before its active run card', () => {
  const ordered = orderQueuedThreadStreamItems([
    { type: 'message', id: 'prompt-1', role: 'user', content: 'What is the team doing?' },
    { type: 'run', id: 'run-1', run_id: 1, status: 'running' },
    {
      type: 'message',
      id: 'live-run-1',
      role: 'illo',
      content: 'I will check recent workspace activity first.',
      metadata: { run_id: 1, live_agent_text: true },
    },
  ]);

  assert.deepEqual(ordered.map((item) => item.id), ['prompt-1', 'live-run-1', 'run-1']);
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
