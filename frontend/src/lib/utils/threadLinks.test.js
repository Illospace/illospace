import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildAbsoluteCortexThreadUrl,
  buildCortexHrefWithoutThread,
  buildCortexThreadHref,
  isThreadRoutePathname,
  threadIdFromThreadPathname,
  threadIdFromUrl,
} from '../features/threads/domain/threadLinks.ts';

test('builds canonical thread routes and removes transient query params', () => {
  const params = new URLSearchParams({
    idea: 'old-thread',
    onboarding: 'runtime-ready',
    open_existing: '1',
    modal: 'vault',
    focus: 'reply',
  });

  assert.equal(
    buildCortexThreadHref('thread:123', params),
    '/threads/thread%3A123?focus=reply',
  );
});

test('extracts thread ids from canonical and legacy URLs', () => {
  assert.equal(threadIdFromThreadPathname('/threads/thread%3A123'), 'thread:123');
  assert.equal(threadIdFromUrl(new URL('http://localhost:8080/threads/abc-123')), 'abc-123');
  assert.equal(threadIdFromUrl(new URL('http://localhost:8080/cortex?idea=abc-123')), 'abc-123');
  assert.equal(threadIdFromUrl(new URL('http://localhost:8080/cortex')), null);
});

test('clears legacy thread query params when returning to the workspace', () => {
  const params = new URLSearchParams({ idea: 'abc-123', focus: 'reply' });

  assert.equal(buildCortexHrefWithoutThread(params), '/cortex?focus=reply');
});

test('recognizes canonical thread pathnames', () => {
  assert.equal(isThreadRoutePathname('/threads/abc-123'), true);
  assert.equal(isThreadRoutePathname('/threads'), false);
  assert.equal(isThreadRoutePathname('/cortex'), false);
});

test('absolute copied thread URLs use the canonical route', () => {
  assert.equal(
    buildAbsoluteCortexThreadUrl('abc-123', 'http://localhost:8080'),
    'http://localhost:8080/threads/abc-123',
  );
});
