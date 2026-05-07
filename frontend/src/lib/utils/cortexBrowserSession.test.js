import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applyBrowserSessionDelta,
  applyBrowserSessionError,
  applyBrowserSessionSnapshot,
  browserCommandPayload,
  browserEventShouldFocusThread,
  emptyBrowserSessionViewState,
} from './cortexBrowserSession.ts';

test('decides when browser events should focus their owning thread', () => {
  assert.equal(browserEventShouldFocusThread({ idea_id: 'idea-1', run_id: 7 }, { id: 's1' }), true);
  assert.equal(browserEventShouldFocusThread({ idea_id: 'idea-1', replayed: true }, { id: 's1', run_id: 7 }), false);
  assert.equal(browserEventShouldFocusThread({ idea_id: 'idea-1' }, { id: 's1' }), false);
});

test('applies browser session snapshots without clearing existing sidecars', () => {
  const current = {
    session: { id: 'old', idea_id: 'idea-1', status: 'running' },
    frame: { sha1: 'old' },
    discovery: { count: 1 },
    extraction: { content: 'old' },
  };

  const next = applyBrowserSessionSnapshot(
    current,
    { id: 'new', idea_id: 'idea-1', status: 'running' },
    { sha1: 'new' },
  );

  assert.equal(next.session.id, 'new');
  assert.equal(next.frame.sha1, 'new');
  assert.equal(next.discovery.count, 1);
  assert.equal(next.extraction.content, 'old');
});

test('applies browser deltas to state, frame, discovery, and extraction', () => {
  const current = {
    session: { id: 's1', idea_id: 'idea-1', status: 'running', current_url: 'https://old.test' },
    frame: null,
    discovery: null,
    extraction: null,
  };

  const stateDelta = applyBrowserSessionDelta(current, {
    session_id: 's1',
    result: {
      state: { id: 's1', current_url: 'https://new.test' },
      frame: { sha1: 'frame-1' },
    },
  });
  const discovered = applyBrowserSessionDelta(stateDelta, {
    session_id: 's1',
    action: 'discover',
    result: { elements: [{ index: 0, text: 'Open' }] },
  });
  const extracted = applyBrowserSessionDelta(discovered, {
    session_id: 's1',
    action: 'extract',
    result: { content: 'hello page' },
  });

  assert.equal(stateDelta.session.current_url, 'https://new.test');
  assert.equal(stateDelta.frame.sha1, 'frame-1');
  assert.equal(discovered.discovery.elements[0].text, 'Open');
  assert.equal(extracted.extraction.content, 'hello page');
});

test('ignores deltas for a different active browser session', () => {
  const current = {
    session: { id: 's1', status: 'running' },
    frame: null,
    discovery: null,
    extraction: null,
  };

  assert.equal(applyBrowserSessionDelta(current, { session_id: 'other', result: { state: { id: 'other' } } }), current);
});

test('marks active browser sessions as errored', () => {
  const errored = applyBrowserSessionError(
    { id: 's1', status: 'running' },
    { session_id: 's1', error: 'Navigation failed' },
  );

  assert.equal(errored.status, 'error');
  assert.equal(errored.last_error, 'Navigation failed');
  assert.equal(applyBrowserSessionError(errored, { session_id: 'other', error: 'nope' }), errored);
});

test('builds command payloads only when a session exists', () => {
  assert.deepEqual(browserCommandPayload('s1', { url: 'https://example.com' }), {
    session_id: 's1',
    url: 'https://example.com',
  });
  assert.equal(browserCommandPayload(null), null);
});

test('creates an empty browser view state', () => {
  assert.deepEqual(emptyBrowserSessionViewState(), {
    session: null,
    frame: null,
    discovery: null,
    extraction: null,
  });
});
