import test from 'node:test';
import assert from 'node:assert/strict';

import { decideThreadArtifactDeepLink } from '../features/threads/domain/threadArtifactDeepLink.ts';

test('thread artifact deep links force one refresh when the requested app is missing from cache', () => {
  assert.deepEqual(
    decideThreadArtifactDeepLink({
      requestedAppId: 'artifact-app',
      lastAutoOpenedAppId: null,
      appExists: false,
      appBelongsToCurrentThread: false,
      currentThreadLoaded: true,
      loadRequestedForAppId: null,
      appsLoading: false,
    }),
    { action: 'request-refresh', appId: 'artifact-app' },
  );
});

test('thread artifact deep links wait while a forced app refresh is already in flight', () => {
  assert.deepEqual(
    decideThreadArtifactDeepLink({
      requestedAppId: 'artifact-app',
      lastAutoOpenedAppId: null,
      appExists: false,
      appBelongsToCurrentThread: false,
      currentThreadLoaded: true,
      loadRequestedForAppId: 'artifact-app',
      appsLoading: true,
    }),
    { action: 'wait-for-app', appId: 'artifact-app' },
  );
});

test('thread artifact deep links open only after the app belongs to the current thread', () => {
  assert.deepEqual(
    decideThreadArtifactDeepLink({
      requestedAppId: 'artifact-app',
      lastAutoOpenedAppId: null,
      appExists: true,
      appBelongsToCurrentThread: true,
      currentThreadLoaded: true,
      loadRequestedForAppId: 'artifact-app',
      appsLoading: false,
    }),
    { action: 'open', appId: 'artifact-app' },
  );
});

test('thread artifact deep links ignore apps scoped to a different thread', () => {
  assert.deepEqual(
    decideThreadArtifactDeepLink({
      requestedAppId: 'artifact-app',
      lastAutoOpenedAppId: null,
      appExists: true,
      appBelongsToCurrentThread: false,
      currentThreadLoaded: true,
      loadRequestedForAppId: null,
      appsLoading: false,
    }),
    { action: 'ignore-wrong-thread', appId: 'artifact-app' },
  );
});
