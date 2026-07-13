import test from 'node:test';
import assert from 'node:assert/strict';

import {
  canShowEarlierThreadHistory,
  ownsThreadHistoryOperation,
} from '../features/threads/domain/threadTranscriptAdapter.ts';
import { buildThreadStreamWindow } from './threadStreamOrdering.ts';

const messages = (start, count) => Array.from(
  { length: count }, (_, index) => ({ type: 'message', id: String(start + index) }),
);

const ids = (window) => window.items.map((item) => item.id);

test('a deferred stale operation cannot clear the next thread owner', async () => {
  let resolveThreadA;
  const threadAResponse = new Promise((resolve) => { resolveThreadA = resolve; });
  let currentToken = 0;
  const threadA = { threadId: 'thread-a', token: ++currentToken };
  const threadAOwnsOnSettle = threadAResponse.then(() =>
    ownsThreadHistoryOperation(threadA, 'thread-b', currentToken));
  currentToken += 1;
  const threadB = { threadId: 'thread-b', token: ++currentToken };
  resolveThreadA();

  assert.equal(await threadAOwnsOnSettle, false);
  assert.equal(ownsThreadHistoryOperation(threadB, 'thread-b', currentToken), true);
});

test('remote history remains reachable without local transcript items', () => {
  assert.equal(canShowEarlierThreadHistory(null, true), true);
  assert.equal(canShowEarlierThreadHistory(null, false), false);
});

test('pre-arming advances each prepended server page without changing the current window', () => {
  let stream = messages(401, 200);
  let window = buildThreadStreamWindow(stream);

  for (const start of [201, 1]) {
    const cursor = window.startCursor;
    assert.deepEqual(ids(buildThreadStreamWindow(stream, cursor)), ids(window));
    stream = [...messages(start, 200), ...stream];
    window = buildThreadStreamWindow(stream, cursor);
    assert.equal(window.items.length, 601 - start);
  }
});

test('an empty window cursor reveals the first remote page', () => {
  const empty = buildThreadStreamWindow([]);
  const firstPage = messages(1, 200);
  assert.deepEqual(empty.startCursor, { index: 0, key: '' });
  assert.deepEqual(buildThreadStreamWindow(firstPage, empty.startCursor).items, firstPage);
});

test('pre-arming crosses a protected prefix when the remote page arrives', () => {
  const protectedPrefix = [
    { type: 'run', id: 'active', run_id: 'active', status: 'running' },
    ...Array.from({ length: 50 }, (_, index) => ({
      type: 'message', id: `live-${index}`, metadata: { run_id: 'active' },
    })),
  ];
  const stream = [...protectedPrefix, ...messages(201, 200)];
  const initial = buildThreadStreamWindow(stream);
  assert.equal(initial.items.length, stream.length);
  assert.deepEqual(ids(buildThreadStreamWindow(stream, initial.startCursor)), ids(initial));
  assert.equal(
    buildThreadStreamWindow([...messages(1, 200), ...stream], initial.startCursor).items.length,
    stream.length + 200,
  );
});

test('a failed pre-armed request leaves retry behavior unchanged', () => {
  const stream = messages(201, 200);
  const initial = buildThreadStreamWindow(stream);
  const failed = buildThreadStreamWindow(stream, initial.startCursor);
  assert.deepEqual(ids(failed), ids(initial));
  assert.equal(
    buildThreadStreamWindow([...messages(1, 200), ...stream], failed.startCursor).items.length,
    400,
  );
});
