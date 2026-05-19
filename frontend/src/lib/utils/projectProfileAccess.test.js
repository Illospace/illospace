import test from 'node:test';
import assert from 'node:assert/strict';
import {
  projectAccessInitial,
  projectAccessMemberName,
  summarizeProjectAccess,
} from './projectProfileAccess.ts';

test('summarizes public project access as a public badge', () => {
  const summary = summarizeProjectAccess({
    visibility: 'public',
    access: [{ user_id: 'user-1', name: 'Ada Lovelace' }],
  });

  assert.equal(summary.isPublic, true);
  assert.deepEqual(summary.visibleMembers, []);
  assert.equal(summary.overflowCount, 0);
  assert.equal(summary.tooltip, 'Public project');
  assert.equal(summary.ariaLabel, 'Public project');
});

test('summarizes private project access with three visible members and full-list tooltip', () => {
  const summary = summarizeProjectAccess({
    visibility: 'private',
    access: [
      { user_id: 'user-1', name: 'Ada Lovelace' },
      { user_id: 'user-2', name: 'Ben Bitdiddle' },
      { user_id: 'user-3', name: 'Cora Count' },
      { user_id: 'user-4', name: 'Dana Scully' },
    ],
  });

  assert.equal(summary.isPublic, false);
  assert.deepEqual(summary.visibleMembers.map(projectAccessMemberName), [
    'Ada Lovelace',
    'Ben Bitdiddle',
    'Cora Count',
  ]);
  assert.equal(summary.overflowCount, 1);
  assert.equal(summary.tooltip, 'Shared with Ada Lovelace, Ben Bitdiddle, Cora Count, Dana Scully');
  assert.equal(summary.ariaLabel, 'Private project shared with Ada Lovelace, Ben Bitdiddle, Cora Count, Dana Scully');
});

test('dedupes private access members, includes owner first, and falls back to a private badge when empty', () => {
  const summary = summarizeProjectAccess(
    {
      visibility: 'private',
      access: [
        { user_id: 'user-1', name: 'Ada Lovelace' },
        { user_id: 'user-1', name: 'Ada Lovelace' },
        { user_id: 'user-2', name: 'Ben Bitdiddle' },
        { name: '  ' },
      ],
    },
    undefined,
    { user_id: 'owner-1', name: 'Grace Hopper' },
  );

  assert.deepEqual(summary.members.map(projectAccessMemberName), [
    'Grace Hopper',
    'Ada Lovelace',
    'Ben Bitdiddle',
  ]);
  assert.equal(summary.overflowCount, 0);

  const emptySummary = summarizeProjectAccess({ visibility: 'private', access: [] });
  assert.equal(emptySummary.tooltip, 'Private project');
  assert.equal(emptySummary.ariaLabel, 'Private project');
});

test('builds round-letter initials from member names or email fallback', () => {
  assert.equal(projectAccessInitial({ name: 'Ada Lovelace' }), 'A');
  assert.equal(projectAccessInitial({ email: 'grace@example.com' }), 'G');
  assert.equal(projectAccessInitial(null), '?');
});
