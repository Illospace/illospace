import test from 'node:test';
import assert from 'node:assert/strict';

import { getRunDecision, getRunHint } from './backgroundRun.ts';

const runCases = [
  {
    name: 'plain request',
    content: 'can you review this?',
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
  {
    name: 'leading slash command',
    content: '/debug flaky test',
    decision: { shouldRun: true, isExplicit: true, reason: 'slash_command' },
    hint: 'Skill command',
  },
  {
    name: 'inline slash command',
    content: 'can you use /debug for this flaky test?',
    decision: { shouldRun: true, isExplicit: true, reason: 'slash_command' },
    hint: 'Skill command',
  },
  {
    name: 'inline slash skill reference',
    content: 'hey illo what does /manage-domains do?',
    decision: { shouldRun: true, isExplicit: true, reason: 'slash_command' },
    hint: 'Skill command',
  },
  {
    name: 'inline slash explanation request',
    content: 'can you explain /debug?',
    decision: { shouldRun: true, isExplicit: true, reason: 'slash_command' },
    hint: 'Skill command',
  },
  {
    name: 'path-like slash text',
    content: 'please inspect /api/foo',
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
  {
    name: 'question text',
    content: 'How should we handle this edge case?',
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
  {
    name: 'legacy @illo mention',
    content: '@illo can you review this?',
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
  {
    name: 'attachment only',
    content: '',
    attachments: 1,
    decision: { shouldRun: false, isExplicit: false, reason: 'none' },
    hint: 'Attachment ready',
  },
  {
    name: 'plain note with attachment',
    content: 'plain note',
    attachments: 1,
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
  {
    name: 'explicit review request',
    content: 'Please review this plan',
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
  {
    name: 'plain note',
    content: 'noting progress from today',
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
  {
    name: 'human mention',
    content: '@alex can you take a look?',
    decision: { shouldRun: true, isExplicit: true, reason: 'message' },
    hint: 'Illo will respond',
  },
];

for (const { name, content, attachments = 0, decision, hint } of runCases) {
  test(`run decision: ${name}`, () => {
    assert.deepEqual(getRunDecision(content), decision);
    assert.equal(getRunHint(content, attachments), hint);
  });
}

test('empty message without attachments stays idle', () => {
  assert.deepEqual(getRunDecision(''), {
    shouldRun: false,
    isExplicit: false,
    reason: 'none',
  });
  assert.equal(getRunHint(''), '');
});
