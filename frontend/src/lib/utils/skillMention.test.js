import test from 'node:test';
import assert from 'node:assert/strict';

import { hasSkillMention, splitSkillMentions } from './skillMention.ts';

test('splitSkillMentions highlights inline slash skills', () => {
  assert.deepEqual(splitSkillMentions('use /manage-domains for this'), [
    { kind: 'text', text: 'use ' },
    { kind: 'skill', text: '/manage-domains', name: 'manage-domains' },
    { kind: 'text', text: ' for this' },
  ]);
});

test('splitSkillMentions preserves multiple skill mentions', () => {
  assert.deepEqual(splitSkillMentions('/manage-domains then /build-workspace-app'), [
    { kind: 'skill', text: '/manage-domains', name: 'manage-domains' },
    { kind: 'text', text: ' then ' },
    { kind: 'skill', text: '/build-workspace-app', name: 'build-workspace-app' },
  ]);
});

test('splitSkillMentions ignores path-like slashes', () => {
  assert.equal(hasSkillMention('/api/foo'), false);
  assert.equal(hasSkillMention('open foo/bar'), false);
});
