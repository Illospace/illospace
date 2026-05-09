import test from 'node:test';
import assert from 'node:assert/strict';

import { parseServerTimeMs, relativeTimeAgo } from './datetime.ts';

test('treats timezone-less server timestamps as UTC', () => {
  assert.equal(
    parseServerTimeMs('2026-05-09T14:34:54.552702'),
    Date.parse('2026-05-09T14:34:54.552702Z'),
  );
});

test('formats relative timestamps without going negative for clock skew', () => {
  const nowMs = Date.parse('2026-05-09T14:34:54Z');

  assert.equal(relativeTimeAgo('2026-05-09T14:34:24', nowMs), '30s ago');
  assert.equal(relativeTimeAgo('2026-05-09T14:35:24', nowMs), '0s ago');
});

test('returns an empty label for invalid timestamps', () => {
  assert.equal(relativeTimeAgo('not-a-date'), '');
});
