import test from 'node:test';
import assert from 'node:assert/strict';

import {
  normalizeWorkspaceAppStateKey,
  resolveWorkspaceAppStateKey,
} from '../features/workspace-apps/domain/workspaceAppStateKey.ts';

test('workspace app state key routing prefers a route state_key over the manifest default', () => {
  const params = new URLSearchParams({ state_key: 'thread-collab-123' });

  assert.equal(resolveWorkspaceAppStateKey(params, 'default'), 'thread-collab-123');
});

test('workspace app state key routing falls back to the manifest key when route key is absent or invalid', () => {
  assert.equal(resolveWorkspaceAppStateKey(new URLSearchParams(), 'manifest-key'), 'manifest-key');
  assert.equal(
    resolveWorkspaceAppStateKey(new URLSearchParams({ state_key: 'x'.repeat(121) }), 'manifest-key'),
    'manifest-key',
  );
});

test('workspace app state key routing normalizes blank values to default', () => {
  assert.equal(normalizeWorkspaceAppStateKey('   '), 'default');
  assert.equal(normalizeWorkspaceAppStateKey(null, 'fallback'), 'fallback');
});
