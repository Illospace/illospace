import test from 'node:test';
import assert from 'node:assert/strict';

import {
  LOCAL_PREVIEW_TEXT_LAB_ID,
  buildLocalPreviewIdeas,
  buildLocalPreviewThreadStream,
  isLocalPreviewIdeaId,
  isLocalPreviewMemberId,
} from './cortexLocalPreview.ts';

const previewMember = {
  id: '__cortex-preview-user__0',
  name: 'Maya',
  color: '#8db7ff',
};

test('identifies local preview member and idea ids', () => {
  assert.equal(isLocalPreviewIdeaId('__cortex-preview-idea__0-0'), true);
  assert.equal(isLocalPreviewIdeaId(LOCAL_PREVIEW_TEXT_LAB_ID), true);
  assert.equal(isLocalPreviewMemberId('__cortex-preview-user__0'), true);
  assert.equal(isLocalPreviewIdeaId('real-idea-id'), false);
  assert.equal(isLocalPreviewMemberId(null), false);
});

test('adds a dev-only Illo text lab preview thread', () => {
  const ideas = buildLocalPreviewIdeas([previewMember], 1);
  const lab = ideas.find((idea) => idea.id === LOCAL_PREVIEW_TEXT_LAB_ID);

  assert.equal(lab?.title, 'Illo text lab');
  assert.equal(lab?.status, 'working');
  assert.equal(lab?.user_id, previewMember.id);
});

test('builds the Illo text lab stream with markdown, tools, and live progress', () => {
  const stream = buildLocalPreviewThreadStream({
    id: LOCAL_PREVIEW_TEXT_LAB_ID,
    title: 'Illo text lab',
    status: 'working',
    created_at: '2026-05-04T16:00:00.000Z',
    user_id: previewMember.id,
  }, [previewMember]);
  const run = stream.find((item) => item.type === 'run');

  assert.equal(stream.length, 4);
  assert.equal(stream[1].role, 'illo');
  assert.match(stream[1].content, /\*\*Short answer:\*\*/);
  assert.equal(run.status, 'running');
  assert.equal(run.work_log.some((entry) => entry.text.includes('**Reviewing reflection typography**')), true);
  assert.equal(run.work_log.some((entry) => entry.text.includes('output tokens')), true);
  assert.deepEqual(run.tool_calls.map((toolCall) => toolCall.tool), ['read_file', 'exec_command', 'apply_patch']);
});

test('builds a completed local preview thread stream with collapsed work data', () => {
  const stream = buildLocalPreviewThreadStream({
    id: '__cortex-preview-idea__0-0',
    title: 'Timeline polish',
    status: 'done',
    created_at: '2026-05-04T16:00:00.000Z',
    user_id: previewMember.id,
  }, [previewMember]);
  const run = stream.find((item) => item.type === 'run');

  assert.equal(stream.length, 4);
  assert.equal(stream[0].role, 'user');
  assert.equal(stream[0].user_name, 'Maya');
  assert.equal(run.status, 'completed');
  assert.equal(run.duration_sec, 107);
  assert.equal(run.work_summary.duration_sec, 107);
  assert.deepEqual(run.tool_calls.map((toolCall) => toolCall.tool), ['edit_file', 'exec_command']);
  assert.deepEqual(run.tool_calls.map((toolCall) => toolCall.status), ['completed', 'completed']);
  assert.equal(stream.at(-1).role, 'illo');
});

test('builds a working local preview thread stream with an active tool call', () => {
  const stream = buildLocalPreviewThreadStream({
    id: '__cortex-preview-idea__0-1',
    title: 'Live timeline',
    status: 'working',
    created_at: '2026-05-04T16:00:00.000Z',
    user_id: previewMember.id,
  }, [previewMember]);
  const run = stream.find((item) => item.type === 'run');

  assert.equal(stream.length, 3);
  assert.equal(run.status, 'running');
  assert.equal(run.completed_at, undefined);
  assert.equal(run.duration_sec, undefined);
  assert.equal(run.last_activity, 'Using exec_command');
  assert.equal(run.tool_calls.at(-1).tool, 'exec_command');
  assert.equal(run.tool_calls.at(-1).status, 'running');
  assert.equal(run.tool_calls.at(-1).finished_at, undefined);
});
