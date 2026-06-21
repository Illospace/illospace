import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildSyncedThreadRouteHref,
  decideThreadRouteSelection,
} from '../features/cortex/domain/threadRouteOpening.ts';

test('thread routes load through the direct thread path', () => {
  assert.deepEqual(
    decideThreadRouteSelection({
      requestedIdeaId: 'archived-thread',
      selectedIdeaId: null,
      panelOpen: false,
      lastRequestedIdeaId: null,
    }),
    { action: 'load-direct', ideaId: 'archived-thread' },
  );
});

test('thread routes stay idle when no thread is requested', () => {
  assert.deepEqual(
    decideThreadRouteSelection({
      requestedIdeaId: null,
      selectedIdeaId: null,
      panelOpen: false,
      lastRequestedIdeaId: null,
    }),
    { action: 'idle', directThreadUrlPending: false },
  );
});

test('thread routes do not reload an already open thread', () => {
  assert.deepEqual(
    decideThreadRouteSelection({
      requestedIdeaId: 'thread-1',
      selectedIdeaId: 'thread-1',
      panelOpen: true,
      lastRequestedIdeaId: null,
    }),
    { action: 'already-open', ideaId: 'thread-1', directThreadUrlPending: false },
  );
});

test('thread routes skip duplicate unresolved requests', () => {
  assert.deepEqual(
    decideThreadRouteSelection({
      requestedIdeaId: 'missing-thread',
      selectedIdeaId: null,
      panelOpen: false,
      lastRequestedIdeaId: 'missing-thread',
    }),
    { action: 'skip-repeat' },
  );
});

test('thread route synchronization preserves app deep links', () => {
  const params = new URLSearchParams({
    idea: 'legacy-thread',
    app: 'generated-app-1',
    onboarding: 'runtime-ready',
    focus: 'reply',
  });

  assert.equal(
    buildSyncedThreadRouteHref('thread-1', params),
    '/threads/thread-1?app=generated-app-1&focus=reply',
  );
});
