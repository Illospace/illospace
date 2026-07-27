import test from 'node:test';
import assert from 'node:assert/strict';

import {
  runActivitySteps,
  runWorkSummarySubtitle,
  runWorkSummaryTitle,
  deriveCodeReviewFilesFromRun,
  deriveCodeReviewFilesFromRuns,
  findActiveFastRun,
  hasLiveFastReply,
  shouldShowRunInTranscript,
  mergeRunProgressSnapshot,
  runWorkTimelineItems,
} from './cortexRunPresentation.ts';

test('shows run cards as live work logs in the transcript', () => {
  assert.equal(shouldShowRunInTranscript({ status: 'queued' }), true);
  assert.equal(shouldShowRunInTranscript({ status: 'running' }), true);
  assert.equal(shouldShowRunInTranscript({ status: 'pending_approval' }), true);
  assert.equal(shouldShowRunInTranscript({ status: 'completed' }), true);
  assert.equal(shouldShowRunInTranscript({ status: 'failed' }), true);
});

test('builds codex-style run work summaries', () => {
  assert.equal(
    runWorkSummaryTitle({ status: 'completed', duration_sec: 515 }),
    'Worked for 8m 35s',
  );
  assert.equal(
    runWorkSummarySubtitle({ work_summary: { tool_count: 2, activity_count: 5 } }),
    '2 tools · 5 events',
  );
  assert.equal(runWorkSummaryTitle({ status: 'running' }), 'Working');
});

test('finds active fast runs and live fast replies', () => {
  const run = { type: 'run', id: '7', status: 'running', execution_profile: 'fast' };
  assert.equal(findActiveFastRun(null, [run]), run);
  assert.equal(hasLiveFastReply([
    { type: 'message', metadata: { live_agent_text: true, execution_profile: 'fast' } },
  ]), true);
});

test('builds compact activity steps with live lines after persisted trace', () => {
  const steps = runActivitySteps({
    activity_trace: [
      { at: '2026-05-02T22:14:11.000Z', activity: 'Reading README.md' },
      { at: '2026-05-02T22:14:10.000Z', activity: 'Loading fast context...' },
    ],
    live_lines: ['Writing response...', 'Reading README.md'],
    last_activity: 'Writing response...',
  }, {
    elapsedLabel: () => '5s',
    limit: 4,
  });

  assert.deepEqual(steps.map((step) => step.label), [
    'Loading fast context...',
    'Reading README.md',
    'Writing response...',
  ]);
  assert.equal(steps[0].time, '5s live');
});

test('builds chronological work timeline from thoughts and tools', () => {
  const items = runWorkTimelineItems({
    work_log: [
      { time: '2026-05-03T22:00:00.000Z', text: 'Started', kind: 'run.started' },
      { time: '2026-05-03T22:00:01.000Z', text: 'Reading context', kind: 'run.activity' },
      { time: '2026-05-03T22:00:02.000Z', text: 'Using read_file', kind: 'run.tool_started' },
      { time: '2026-05-03T22:00:03.000Z', text: 'Found the current thread component', kind: 'run.step_started' },
      { time: '2026-05-03T22:00:04.000Z', text: 'read_file completed', kind: 'run.tool_completed' },
      { time: '2026-05-03T22:00:05.000Z', text: 'Completed', kind: 'run.completed' },
    ],
    tool_calls: [
      {
        tool: 'read_file',
        args: '{"path":"thread.svelte"}',
        at: '2026-05-03T22:00:02.000Z',
        status: 'completed',
        finished_at: '2026-05-03T22:00:04.000Z',
      },
    ],
  });

  assert.deepEqual(items.map((item) => item.kind), ['thought', 'tool', 'thought']);
  assert.equal(items[0].text, 'Reading context');
  assert.equal(items[1].tool, 'read_file');
  assert.equal(items[2].text, 'Found the current thread component');
});

test('drops duplicate tool activity prose when a structured tool call exists', () => {
  const items = runWorkTimelineItems({
    work_log: [
      {
        time: '2026-05-03T22:00:01.000Z',
        text: 'Using exec_command: python3 - <<PY',
        kind: 'run.activity',
        tool_name: 'exec_command',
      },
      { time: '2026-05-03T22:00:02.000Z', text: 'Using exec_command', kind: 'run.tool_started' },
    ],
    tool_calls: [
      {
        tool: 'exec_command',
        args: '{"command":"python3 - <<PY"}',
        at: '2026-05-03T22:00:02.000Z',
        status: 'running',
      },
    ],
  });

  assert.deepEqual(items.map((item) => item.kind), ['tool']);
  assert.equal(items[0].tool, 'exec_command');
});

test('uses public tool display labels instead of raw arguments', () => {
  const items = runWorkTimelineItems({
    work_log: [
      { time: '2026-05-03T22:00:01.000Z', text: 'Using run_script', kind: 'run.tool_started' },
    ],
    tool_calls: [
      {
        tool: 'run_script',
        args: '{"description":"Check GitHub access"}',
        at: '2026-05-03T22:00:01.000Z',
        status: 'completed',
        display: {
          icon: '🔧',
          label: 'Checked GitHub access',
          kind: 'command',
          status: 'completed',
        },
      },
    ],
  });

  assert.equal(items.length, 1);
  assert.equal(items[0].kind, 'tool');
  assert.equal(items[0].display.label, 'Checked GitHub access');
  assert.equal(items[0].display.icon, '🔧');
});

test('drops persisted tool activity prose even when work log omits tool metadata', () => {
  const items = runWorkTimelineItems({
    work_log: [
      {
        time: '2026-05-03T22:00:01.000Z',
        text: 'Using skill_asset: examples/domain-backed-app.md',
        kind: 'run.activity',
      },
      { time: '2026-05-03T22:00:02.000Z', text: 'Using skill_asset', kind: 'run.tool_started' },
    ],
    tool_calls: [
      {
        tool: 'skill_asset',
        args: '{"path":"examples/domain-backed-app.md"}',
        at: '2026-05-03T22:00:02.000Z',
        status: 'completed',
      },
    ],
  });

  assert.deepEqual(items.map((item) => item.kind), ['tool']);
  assert.equal(items[0].tool, 'skill_asset');
});

test('compacts progressive reflection snippets into the latest thought', () => {
  const items = runWorkTimelineItems({
    work_log: [
      {
        time: '2026-05-03T22:00:01.000Z',
        text: 'Assessing API usage guidelines',
        kind: 'run.activity',
      },
      {
        time: '2026-05-03T22:00:04.000Z',
        text: 'Assessing API usage guidelines and checking whether the app can fetch public JSON directly',
        kind: 'run.activity',
      },
    ],
  });

  assert.deepEqual(items.map((item) => item.kind), ['thought']);
  assert.equal(
    items[0].text,
    'Assessing API usage guidelines and checking whether the app can fetch public JSON directly',
  );
});

test('keeps short partial thought tails from replacing fuller thoughts', () => {
  const items = runWorkTimelineItems({
    work_log: [
      {
        time: '2026-05-03T22:00:01.000Z',
        text: '**Considering GitHub response details** I',
        kind: 'run.activity',
      },
      {
        time: '2026-05-03T22:00:04.000Z',
        text: '**Considering GitHub response details** I need to summarize what the API returned.',
        kind: 'run.activity',
      },
    ],
  });

  assert.deepEqual(items.map((item) => item.kind), ['thought']);
  assert.equal(
    items[0].text,
    '**Considering GitHub response details** I need to summarize what the API returned.',
  );
});

test('collapses one-letter partial thought tails to the stable header', () => {
  const items = runWorkTimelineItems({
    work_log: [
      {
        time: '2026-05-03T22:00:01.000Z',
        text: '**Considering GitHub response details** I',
        kind: 'run.activity',
      },
    ],
  });

  assert.equal(items.length, 1);
  assert.equal(items[0].text, '**Considering GitHub response details**');
});

test('compacts adjacent duplicate tool rows with the same arguments', () => {
  const items = runWorkTimelineItems({
    tool_calls: [
      {
        tool: 'skill_asset',
        args: '{"path":"examples/domain-backed-app.md"}',
        at: '2026-05-03T22:00:01.000Z',
        status: 'completed',
      },
      {
        tool: 'skill_asset',
        args: '{"path":"examples/domain-backed-app.md"}',
        at: '2026-05-03T22:00:02.000Z',
        status: 'completed',
      },
    ],
  });

  assert.equal(items.length, 1);
  assert.equal(items[0].tool, 'skill_asset');
});

test('keeps the latest persisted activity when the trace is newest first', () => {
  const steps = runActivitySteps({
    activity_trace: [
      { at: '2026-05-02T22:14:13.000Z', activity: 'Writing response...' },
      { at: '2026-05-02T22:14:12.000Z', activity: 'Reading README.md' },
      { at: '2026-05-02T22:14:11.000Z', activity: 'Loading fast context...' },
    ],
  }, { limit: 2 });

  assert.deepEqual(steps.map((step) => step.label), [
    'Reading README.md',
    'Writing response...',
  ]);
});

test('merges stale run snapshots without erasing live work log progress', () => {
  const live = {
    type: 'run',
    id: '7',
    status: 'running',
    execution_profile: 'fast',
    activity_trace: [
      { at: '2026-05-03T20:58:51.000Z', activity: 'Started', kind: 'run.started' },
      { at: '2026-05-03T20:58:52.000Z', activity: 'Using read_file', kind: 'tool_started' },
    ],
    work_log: [{ time: '2026-05-03T20:58:52.000Z', text: 'Using read_file', kind: 'tool_started' }],
    tool_calls: [{ tool: 'read_file', at: '2026-05-03T20:58:52.000Z', status: 'running' }],
  };
  const staleSnapshot = {
    type: 'run',
    id: '7',
    status: 'starting',
    execution_profile: 'fast',
    activity_trace: [{ at: '2026-05-03T20:58:51.000Z', activity: 'Started', kind: 'run.started' }],
    work_summary: { activity_count: 1, tool_count: 0 },
  };

  const merged = mergeRunProgressSnapshot(staleSnapshot, live);

  assert.equal(merged.status, 'running');
  assert.equal(merged.activity_trace.length, 2);
  assert.equal(merged.tool_calls.length, 1);
  assert.equal(merged.work_summary.activity_count, 2);
  assert.equal(merged.work_summary.tool_count, 1);
});

test('terminal run snapshots settle without dropping live activity', () => {
  const live = {
    id: '7',
    status: 'running',
    activity_trace: [{ at: '2026-05-03T20:58:52.000Z', activity: 'Using tool' }],
  };
  const terminal = {
    id: '7',
    status: 'completed',
    activity_trace: [{ at: '2026-05-03T20:58:53.000Z', activity: 'Completed' }],
    work_summary: { duration_sec: 32 },
  };

  const merged = mergeRunProgressSnapshot(terminal, live);

  assert.equal(merged.status, 'completed');
  assert.deepEqual(merged.activity_trace.map((entry) => entry.activity), ['Using tool', 'Completed']);
  assert.equal(merged.work_summary.duration_sec, 32);
});


test('derives code review files from write and edit tool calls', () => {
  const files = deriveCodeReviewFilesFromRun({
    id: 42,
    tool_calls: [
      { tool: 'read_file', args: '{"path":"README.md"}', status: 'completed' },
      { tool: 'write_file', args: '{"path":"src/new.ts","content":"x"}', status: 'completed' },
      { tool: 'edit_file', args: '{"path":"src/existing.ts"}', status: 'completed' },
    ],
  });

  assert.deepEqual(files.map((file) => file.path), ['src/existing.ts', 'src/new.ts']);
  assert.equal(files[0].operation, 'changed');
  assert.equal(files[1].operation, 'created or updated');
});

test('derives code review files from file artifacts and dedupes with tool calls', () => {
  const files = deriveCodeReviewFilesFromRuns([
    {
      id: 7,
      artifacts: [
        {
          artifact_type: 'file_observation',
          payload: { type: 'file_observation', operation: 'read', path: 'src/read-only.ts' },
        },
        {
          artifact_type: 'file',
          created_at: '2026-05-03T22:00:00.000Z',
          payload: { type: 'file', relative_path: 'src/changed.ts', status: 'edit' },
        },
      ],
      tool_calls: [
        { tool: 'edit_file', args: '{"path":"src/changed.ts"}', status: 'completed' },
      ],
    },
  ]);

  assert.equal(files.length, 1);
  assert.equal(files[0].path, 'src/changed.ts');
  assert.equal(files[0].source, 'artifact');
  assert.equal(files[0].operation, 'edit');
});
