import test from 'node:test';
import assert from 'node:assert/strict';

import {
  applyAgentActivityToStream,
  applyAgentTextDeltaToStream,
  applyRunCompletedToStream,
  mergeLiveStreamState,
  runUiEventKey,
  shouldRenderLiveAgentTextItem,
} from './cortexRunStream.ts';
import { mergeRunProgressSnapshot } from './cortexRunPresentation.ts';

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
