import test from 'node:test';
import assert from 'node:assert/strict';

import { getRunDecision, getRunHint } from './backgroundRun.ts';

test('plain text runs explicitly to Illo', () => {
  const decision = getRunDecision('can you review this?');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.isExplicit, true);
  assert.equal(decision.reason, 'message');
});

test('slash command still runs explicitly', () => {
  const decision = getRunDecision('/debug flaky test');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.isExplicit, true);
  assert.equal(decision.reason, 'slash_command');
});

test('inline slash command runs as a skill command', () => {
  const decision = getRunDecision('can you use /debug for this flaky test?');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.isExplicit, true);
  assert.equal(decision.reason, 'slash_command');
  assert.equal(getRunHint('can you use /debug for this flaky test?'), 'Skill command');
});

test('inline slash skill reference runs as a skill command', () => {
  const decision = getRunDecision('hey illo what does /manage-domains do?');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.isExplicit, true);
  assert.equal(decision.reason, 'slash_command');
  assert.equal(getRunHint('hey illo what does /manage-domains do?'), 'Skill command');
});

test('inline slash skill explanation request runs as a skill command', () => {
  const decision = getRunDecision('can you explain /debug?');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.reason, 'slash_command');
  assert.equal(getRunHint('can you explain /debug?'), 'Skill command');
});

test('path-like slash text runs as a normal message', () => {
  const decision = getRunDecision('please inspect /api/foo');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.isExplicit, true);
  assert.equal(decision.reason, 'message');
  assert.equal(getRunHint('please inspect /api/foo'), 'Illo will respond');
});

test('question text runs as a normal Illo message', () => {
  const decision = getRunDecision('How should we handle this edge case?');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.isExplicit, true);
  assert.equal(decision.reason, 'message');
  assert.equal(getRunHint('How should we handle this edge case?'), 'Illo will respond');
});

test('legacy @illo mention is just normal text now', () => {
  assert.equal(getRunHint('@illo can you review this?'), 'Illo will respond');
});

test('attachment-only hint stays neutral until the user writes to Illo', () => {
  assert.equal(getRunHint('', 1), 'Attachment ready');
  assert.equal(getRunHint('plain note', 1), 'Illo will respond');
});

test('request text runs to Illo', () => {
  const decision = getRunDecision('Please review this plan');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.reason, 'message');
});

test('plain note runs to Illo', () => {
  const decision = getRunDecision('noting progress from today');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.reason, 'message');
});

test('human mentions still run while mentions are resolved separately', () => {
  const decision = getRunDecision('@alex can you take a look?');
  assert.equal(decision.shouldRun, true);
  assert.equal(decision.reason, 'message');
});
