import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applyAgentActivityToStream,
  applyAgentTextDeltaToStream,
  applyRunCompletedToStream,
  mergeLiveStreamState,
  mergeThreadStreamPageItems,
  runUiEventKey,
  shouldRenderLiveAgentTextItem,
} from './cortexRunStream.ts';
import { mergeRunProgressSnapshot } from './cortexRunPresentation.ts';

const message = (id, content = id, timestamp = /^\d+$/.test(id)
  ? new Date(Number(id) * 1000).toISOString()
  : undefined) => ({ type: 'message', id, idea_id: 'idea-1', content, timestamp });

function reconcilePage(current, page, mode) {
  const paged = mergeThreadStreamPageItems(current, page, mode);
  return mergeLiveStreamState(
    paged,
    current,
    'idea-1',
    (item) => ['starting', 'running', 'queued'].includes(item.status),
    mergeRunProgressSnapshot,
  );
}

test('merges live run progress into persisted stream snapshots', () => {
  const persisted = [{
    type: 'run',
    id: '7',
    run_id: 7,
    idea_id: 'idea-1',
    status: 'starting',
    activity_trace: [{ at: '2026-05-05T10:00:00Z', activity: 'Started' }],
  }];
  const live = [{
    type: 'run',
    id: '7',
    run_id: 7,
    idea_id: 'idea-1',
    status: 'running',
    activity_trace: [{ at: '2026-05-05T10:00:01Z', activity: 'Reading files' }],
    tool_calls: [{ tool: 'read_file', at: '2026-05-05T10:00:01Z', status: 'running' }],
  }];

  const merged = mergeLiveStreamState(
    persisted,
    live,
    'idea-1',
    (item) => item.status === 'running',
    mergeRunProgressSnapshot,
  );

  assert.equal(merged.length, 1);
  assert.equal(merged[0].status, 'running');
  assert.equal(merged[0].activity_trace.length, 2);
  assert.equal(merged[0].tool_calls.length, 1);
});

test('keeps active live runs missing from the server snapshot', () => {
  const merged = mergeLiveStreamState(
    [{ type: 'message', id: 'm1', idea_id: 'idea-1', content: 'hello' }],
    [{ type: 'run', id: '8', run_id: 8, idea_id: 'idea-1', status: 'running' }],
    'idea-1',
    (item) => item.status === 'running',
    mergeRunProgressSnapshot,
  );

  assert.deepEqual(merged.map((item) => item.id), ['m1', '8']);
});

test('hides live partial replies once persisted run reply is visible', () => {
  const merged = mergeLiveStreamState(
    [{
      type: 'message',
      id: 'm1',
      idea_id: 'idea-1',
      content: 'Persisted answer',
      metadata: { run_id: 9 },
    }],
    [{
      type: 'message',
      id: 'live-run-9',
      idea_id: 'idea-1',
      content: 'Partial answer',
      metadata: { run_id: 9, idea_id: 'idea-1', live_agent_text: true },
    }],
    'idea-1',
    () => false,
    mergeRunProgressSnapshot,
  );

  assert.deepEqual(merged.map((item) => item.id), ['m1']);
});

test('appends, extends, and resets live text deltas', () => {
  const first = applyAgentTextDeltaToStream([], {
    idea_id: 'idea-1',
    run_id: 12,
    delta: 'Hel',
    profile: 'fast',
  }, 'idea-1', '2026-05-05T10:00:00Z');
  const second = applyAgentTextDeltaToStream(first, {
    idea_id: 'idea-1',
    run_id: 12,
    delta: 'lo',
  }, 'idea-1', '2026-05-05T10:00:01Z');
  const reset = applyAgentTextDeltaToStream(second, {
    idea_id: 'idea-1',
    run_id: 12,
    reset: true,
  }, 'idea-1');

  assert.equal(second[0].content, 'Hello');
  assert.equal(second[0].metadata.live_agent_text, true);
  assert.deepEqual(reset, []);
});

test('keeps live agent text visible until a settled run reply exists', () => {
  const run = { type: 'run', id: '12', run_id: 12, status: 'running' };
  const live = {
    type: 'message',
    id: 'live-run-12',
    role: 'illo',
    content: 'I will inspect the trace first.',
    metadata: { run_id: 12, live_agent_text: true },
  };
  const settled = {
    type: 'message',
    id: 'm12',
    role: 'illo',
    content: 'Here is what I found.',
    metadata: { run_id: 12 },
  };

  assert.equal(shouldRenderLiveAgentTextItem(live, [run, live]), true);
  assert.equal(shouldRenderLiveAgentTextItem(live, [run, live, settled]), false);
  assert.equal(shouldRenderLiveAgentTextItem(settled, [run, live, settled]), true);
});

test('starts a new live text segment after tool work begins', () => {
  const preamble = applyAgentTextDeltaToStream([], {
    idea_id: 'idea-1',
    run_id: 12,
    delta: 'I will inspect first.',
    profile: 'fast',
    event_created_at: '2026-05-05T10:00:01Z',
  }, 'idea-1');
  const withTool = applyAgentActivityToStream(preamble, {
    type: 'tool_started',
    idea_id: 'idea-1',
    run_id: 12,
    activity: 'Using read_file',
    tool_name: 'read_file',
    event_created_at: '2026-05-05T10:00:02Z',
  }, 'idea-1', 'fast');
  const next = applyAgentTextDeltaToStream(withTool, {
    idea_id: 'idea-1',
    run_id: 12,
    delta: 'I found the file.',
    event_created_at: '2026-05-05T10:00:03Z',
  }, 'idea-1');
  const liveMessages = next.filter((item) => item.metadata?.live_agent_text);

  assert.deepEqual(liveMessages.map((item) => item.content), [
    'I will inspect first.',
    'I found the file.',
  ]);
  assert.equal(liveMessages[0].metadata.live_agent_text_after_tool, false);
  assert.equal(liveMessages[1].metadata.live_agent_text_after_tool, true);
});

test('creates and updates run activity with tool call state', () => {
  const started = applyAgentActivityToStream([], {
    type: 'tool_started',
    idea_id: 'idea-1',
    run_id: 42,
    activity: 'Using read_file',
    tool_name: 'read_file',
    args: { path: 'README.md' },
    event_created_at: '2026-05-05T10:00:00Z',
  }, 'idea-1', 'fast');
  const finished = applyAgentActivityToStream(started, {
    type: 'tool_finished',
    idea_id: 'idea-1',
    run_id: 42,
    activity: 'read_file completed',
    tool_name: 'read_file',
    status: 'completed',
    result: 'ok',
    event_created_at: '2026-05-05T10:00:05Z',
  }, 'idea-1', 'fast');

  assert.equal(finished[0].last_activity, 'read_file completed');
  assert.equal(finished[0].tool_calls[0].status, 'completed');
  assert.equal(finished[0].tool_calls[0].result, 'ok');
});

test('settles run completion summaries', () => {
  const stream = [{
    type: 'run',
    id: '42',
    run_id: 42,
    idea_id: 'idea-1',
    started_at: '2026-05-05T10:00:00Z',
    status: 'running',
    activity_trace: [{ activity: 'Working' }],
    tool_calls: [{ tool: 'read_file', status: 'completed' }],
  }];

  const completed = applyRunCompletedToStream(stream, {
    idea_id: 'idea-1',
    run_id: 42,
    status: 'completed',
    event_created_at: '2026-05-05T10:00:10Z',
  }, 'idea-1');

  assert.equal(completed[0].status, 'completed');
  assert.equal(completed[0].duration_sec, 10);
  assert.equal(completed[0].work_summary.tool_count, 1);
});

test('deduplicates run UI events by event cursor', () => {
  assert.equal(runUiEventKey({ type: 'tool_started', run_id: 1, event_cursor: 20 }), 'tool_started:1:20');
  assert.equal(runUiEventKey({ type: 'tool_started', run_id: 1, event_cursor: 0 }), null);
});

test('prepends overlapping older pages by typed identity without replacing newer items', () => {
  const current = [message('3', 'newer three'), message('4'), message('5')];
  const merged = reconcilePage(
    current,
    [message('1'), message('2'), message('3', 'stale three')],
    'older',
  );

  assert.deepEqual(merged.map((item) => `${item.type}:${item.id}`), [
    'message:1', 'message:2', 'message:3', 'message:4', 'message:5',
  ]);
  assert.equal(merged[2].content, 'newer three');
  assert.deepEqual(
    mergeThreadStreamPageItems(
      [{ type: 'run', id: 'shared' }, message('shared')],
      [],
      'older',
    ).map((item) => `${item.type}:${item.id}`),
    ['message:shared', 'run:shared'],
  );
});

test('concurrent head and older pages converge regardless of response order', () => {
  const initial = [message('3'), message('4'), message('5')];
  const older = [message('1'), message('2'), message('3')];
  const head = [message('4'), message('5'), message('6')];

  const headThenOlder = reconcilePage(reconcilePage(initial, head, 'head'), older, 'older');
  const olderThenHead = reconcilePage(reconcilePage(initial, older, 'older'), head, 'head');
  for (const result of [headThenOlder, olderThenHead]) {
    assert.deepEqual(result.map((item) => item.id), ['1', '2', '3', '4', '5', '6']);
  }
});

test('keeps websocket items and live progress that arrive while a page is loading', () => {
  const websocketMessage = message('6', 'arrived live');
  const initial = reconcilePage([websocketMessage], [
    message('1'), message('2'), message('3'), message('4'), message('5'),
  ], 'initial');
  assert.deepEqual(initial.map((item) => item.id), ['1', '2', '3', '4', '5', '6']);

  const liveRun = {
    type: 'run', id: '7', run_id: 7, idea_id: 'idea-1', status: 'running',
    tool_calls: [{ tool: 'read_file', status: 'running' }],
  };
  const refreshed = reconcilePage([liveRun], [{
    type: 'run', id: '7', run_id: 7, idea_id: 'idea-1', status: 'starting', tool_calls: [],
  }], 'head');
  assert.equal(refreshed[0].status, 'running');
  assert.equal(refreshed[0].tool_calls.length, 1);

  const noOverlapHead = reconcilePage([
    message('old', 'old', '2026-01-01T00:00:00Z'),
    message('websocket', 'live', '2026-01-03T00:00:00Z'),
  ], [message('head', 'head', '2026-01-02T00:00:00Z')], 'head');
  assert.deepEqual(noOverlapHead.map((item) => item.id), ['old', 'head', 'websocket']);
});

test('sorts a head page around an injected active root instead of its first item', () => {
  const merged = reconcilePage([
    message('history', 'history', '2026-01-01T12:00:00Z'),
    message('websocket', 'live', '2026-01-04T00:00:00Z'),
  ], [{
    type: 'run', id: 'old-root', run_id: 'old-root', idea_id: 'idea-1', status: 'running',
    timestamp: '2026-01-01T00:00:00Z',
  },
  message('head-2', 'head 2', '2026-01-02T00:00:00Z'),
  message('head-3', 'head 3', '2026-01-03T00:00:00Z')], 'head');

  assert.deepEqual(merged.map((item) => item.id), [
    'old-root', 'history', 'head-2', 'head-3', 'websocket',
  ]);
});

test('sorts a later synthetic final from an older run by its own timestamp', () => {
  const merged = reconcilePage(
    [message('current', 'current', '2026-01-05T00:00:00Z')],
    [{
      type: 'run', id: '9', run_id: 9, idea_id: 'idea-1', status: 'completed',
      timestamp: '2026-01-01T00:00:00Z',
    }, {
      type: 'message', id: 'run-final-9-artifact', idea_id: 'idea-1', content: 'Synthetic',
      timestamp: '2026-01-06T00:00:00Z',
      metadata: { run_id: 9, synthetic_from_run_artifact: true },
    }],
    'older',
  );

  assert.deepEqual(merged.map((item) => item.id), ['9', 'current', 'run-final-9-artifact']);
});

test('uses kind rank and numeric persisted ids for equal timestamps', () => {
  const timestamp = '2026-01-01T00:00:00Z';
  const merged = mergeThreadStreamPageItems([], [
    { type: 'visual_block', id: 'vb-2', timestamp },
    { type: 'message', id: '10', timestamp },
    { type: 'run', id: '10', timestamp },
    { type: 'visual_block', id: 'vb-10', timestamp },
    { type: 'run', id: '2', timestamp },
    { type: 'message', id: '2', timestamp },
  ], 'initial');

  assert.deepEqual(merged.map((item) => `${item.type}:${item.id}`), [
    'message:2', 'message:10', 'run:2', 'run:10',
    'visual_block:vb-2', 'visual_block:vb-10',
  ]);
});

test('uses deterministic lexical order for equal-time noncanonical ids', () => {
  const timestamp = '2026-01-01T00:00:00Z';
  const merged = mergeThreadStreamPageItems([], [
    { type: 'message', id: 'live-run-2', timestamp },
    { type: 'message', id: 'live-run-10', timestamp },
  ], 'initial');

  assert.deepEqual(merged.map((item) => item.id), ['live-run-10', 'live-run-2']);
});

test('never regresses a terminal run to stale live state', () => {
  const terminal = {
    type: 'run', id: '7', run_id: 7, idea_id: 'idea-1', status: 'completed',
    timestamp: '2026-01-01T00:00:00Z', completed_at: '2026-01-01T00:01:00Z',
  };
  const stale = { ...terminal, status: 'running', completed_at: undefined };

  assert.equal(reconcilePage([terminal], [stale], 'head')[0].status, 'completed');
  assert.equal(reconcilePage([stale], [terminal], 'head')[0].status, 'completed');
});

test('deduplicates synthetic final answers when a persisted reply crosses a page boundary', () => {
  const synthetic = {
    type: 'message', id: 'run-final-9-artifact', idea_id: 'idea-1', content: 'Synthetic',
    metadata: { run_id: 9, synthetic_from_run_artifact: true },
  };
  const persisted = {
    type: 'message', id: 'persisted-9', idea_id: 'idea-1', content: 'Persisted',
    metadata: { run_id: 9 },
  };
  const run = { type: 'run', id: '9', run_id: 9, idea_id: 'idea-1', status: 'completed' };

  const persistedInHead = reconcilePage([persisted], [run, synthetic], 'older');
  const persistedInOlder = reconcilePage([synthetic], [run, persisted], 'older');
  for (const result of [persistedInHead, persistedInOlder]) {
    assert.deepEqual(
      result.filter((item) => item.type === 'message').map((item) => item.id),
      ['persisted-9'],
    );
  }
});

test('repeated overlapping older pages recover the full chronological sequence', () => {
  const messages = (start, end) => Array.from(
    { length: end - start + 1 },
    (_, index) => message(String(start + index)),
  );
  let stream = reconcilePage([], messages(801, 1000), 'initial');
  for (const page of [
    messages(601, 801),
    messages(401, 601),
    messages(201, 401),
    messages(1, 201),
  ]) {
    stream = reconcilePage(stream, page, 'older');
  }

  assert.equal(stream.length, 1000);
  assert.equal(new Set(stream.map((item) => `${item.type}:${item.id}`)).size, 1000);
  assert.deepEqual(stream.map((item) => item.id), messages(1, 1000).map((item) => item.id));
});
