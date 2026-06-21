import test from 'node:test';
import assert from 'node:assert/strict';

import { cloneForPostMessage } from './postMessageClone.ts';

test('cloneForPostMessage strips proxy wrappers before browser postMessage', () => {
  const proxiedState = new Proxy(
    {
      state: {
        votes: {
          'user:1': {
            value: 'decision-room',
          },
        },
        notes: [{ body: 'Keep the decision with the thread context.' }],
      },
    },
    {},
  );

  assert.throws(() => structuredClone(proxiedState), { name: 'DataCloneError' });

  const clone = cloneForPostMessage(proxiedState);

  assert.deepEqual(clone, {
    state: {
      votes: {
        'user:1': {
          value: 'decision-room',
        },
      },
      notes: [{ body: 'Keep the decision with the thread context.' }],
    },
  });
  assert.deepEqual(structuredClone(clone), clone);
});

test('cloneForPostMessage keeps bridge payloads JSON-shaped when generated apps pass non-data values', () => {
  const cyclic = { name: 'payload' };
  cyclic.self = cyclic;

  const clone = cloneForPostMessage({
    eventType: 'vote.cast',
    payload: {
      optionId: 'decision-room',
      callback: () => {},
      bigint: 42n,
      cyclic,
    },
  });

  assert.deepEqual(clone, {
    eventType: 'vote.cast',
    payload: {
      optionId: 'decision-room',
      bigint: '42',
      cyclic: {
        name: 'payload',
      },
    },
  });
  assert.deepEqual(structuredClone(clone), clone);
});
