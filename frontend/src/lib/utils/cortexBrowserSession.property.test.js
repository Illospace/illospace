import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applyBrowserSessionDelta,
  applyBrowserSessionError,
  applyBrowserSessionSnapshot,
  emptyBrowserSessionViewState,
} from './cortexBrowserSession.ts';

test('browser session state preserves unrelated sidecars across arbitrary state deltas', () => {
  const base = {
    session: { id: 'session-1', idea_id: 'idea-1', status: 'running', current_url: 'https://old.test' },
    frame: { sha1: 'frame-0', image_url: '/frame.png' },
    discovery: { count: 1, elements: [{ text: 'Keep me' }] },
    extraction: { content: 'existing extraction' },
  };

  const deltas = [
    { result: { state: { id: 'session-1', status: 'running' } } },
    { result: { state: { id: 'session-1', current_url: 'https://new.test' } } },
    { result: { state: { id: 'session-1', page_title: 'New title' } } },
  ];

  for (const delta of deltas) {
    const next = applyBrowserSessionDelta(base, { session_id: 'session-1', ...delta });
    assert.equal(next.frame.sha1, 'frame-0');
    assert.equal(next.discovery.count, 1);
    assert.equal(next.extraction.content, 'existing extraction');
  }
});

test('browser session snapshots clear only the supplied frame and never invent stale state', () => {
  const empty = emptyBrowserSessionViewState();
  const snapshot = applyBrowserSessionSnapshot(
    empty,
    { id: 'session-1', idea_id: 'idea-1', status: 'running' },
    { sha1: 'frame-1', image_url: '/frame.png' },
  );
  const refreshed = applyBrowserSessionSnapshot(
    snapshot,
    { id: 'session-1', idea_id: 'idea-1', status: 'closed' },
    null,
  );

  assert.equal(snapshot.session.status, 'running');
  assert.equal(snapshot.frame.sha1, 'frame-1');
  assert.equal(refreshed.session.status, 'closed');
  assert.equal(refreshed.frame, null);
  assert.equal(refreshed.discovery, null);
  assert.equal(refreshed.extraction, null);
});

test('browser session errors are scoped to the active session id', () => {
  const current = { id: 'session-1', idea_id: 'idea-1', status: 'running', last_error: null };
  const ignored = applyBrowserSessionError(current, { session_id: 'session-2', error: 'wrong tab' });
  const errored = applyBrowserSessionError(current, { session_id: 'session-1', error: 'navigation failed' });

  assert.equal(ignored, current);
  assert.equal(errored.status, 'error');
  assert.equal(errored.last_error, 'navigation failed');
});
