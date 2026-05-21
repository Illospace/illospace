import test from 'node:test';
import assert from 'node:assert/strict';

import { buildChronologicalRunSegments } from './threadRunChronology.ts';

test('places live text between the run work that happened around it', () => {
  const segments = buildChronologicalRunSegments(
    [
      { kind: 'thought', text: 'Reading context', at: '2026-05-21T15:06:34Z' },
      { kind: 'thought', text: 'Writing response... (~4 output tokens)', at: '2026-05-21T15:06:35Z' },
      { kind: 'tool', tool: 'read_team_activity', at: '2026-05-21T15:06:38Z' },
      { kind: 'tool', tool: 'read_roster', at: '2026-05-21T15:06:42Z' },
    ],
    [{
      id: 'live-run-255',
      timestamp: '2026-05-21T15:06:35.399537Z',
      metadata: {
        live_agent_text: true,
        live_agent_text_first_delta_at: '2026-05-21T15:06:35.399537Z',
      },
    }],
    { includeTrailingCue: true },
  );

  assert.deepEqual(segments.map((segment) => segment.kind), ['work', 'live_text', 'work']);
  assert.deepEqual(segments[0].items.map((item) => item.text ?? item.tool), ['Reading context']);
  assert.equal(segments[1].item.id, 'live-run-255');
  assert.deepEqual(segments[2].items.map((item) => item.text ?? item.tool), [
    'read_team_activity',
    'read_roster',
  ]);
  assert.equal(segments[2].showLiveCue, true);
});

test('splits additional live text after tool work into a new chronological segment', () => {
  const segments = buildChronologicalRunSegments(
    [
      { kind: 'tool', tool: 'read_file', at: '2026-05-21T15:06:38Z' },
      { kind: 'tool', tool: 'search_files', at: '2026-05-21T15:06:45Z' },
    ],
    [
      {
        id: 'live-run-255',
        timestamp: '2026-05-21T15:06:35Z',
        metadata: { live_agent_text_first_delta_at: '2026-05-21T15:06:35Z' },
      },
      {
        id: 'live-run-255-2',
        timestamp: '2026-05-21T15:06:41Z',
        metadata: { live_agent_text_first_delta_at: '2026-05-21T15:06:41Z' },
      },
    ],
  );

  assert.deepEqual(segments.map((segment) => segment.kind), ['live_text', 'work', 'live_text', 'work']);
  assert.equal(segments[0].item.id, 'live-run-255');
  assert.deepEqual(segments[1].items.map((item) => item.tool), ['read_file']);
  assert.equal(segments[2].item.id, 'live-run-255-2');
  assert.deepEqual(segments[3].items.map((item) => item.tool), ['search_files']);
});
