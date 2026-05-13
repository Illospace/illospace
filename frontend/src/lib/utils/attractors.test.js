import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildLookup,
  computeAttractorViewportTransform,
  createAttractors,
  getAttractionTarget,
  getDualUserSpacingDistance,
  nearestAttractorWithinRadius,
  handoffTargetWithinRadius,
  orbitAnchorKey,
  orbitAnchorRefForAttractor,
  orbitAnchorTargetWithinRadius,
  orbitPhysicsProfileForAttractor,
  orbitPhysicsProfileForKind,
  sortTeamMembersForSharedAttractorLayout,
} from './attractors.ts';

test('sortTeamMembersForSharedAttractorLayout is independent of the local viewer', () => {
  const redaFirst = [
    { id: 'user-reda', name: 'Reda', color: '#ff0000' },
    { id: 'user-axel', name: 'Axel', color: '#00ff00' },
    { id: 'user-jules', name: 'Jules', color: '#0000ff' },
  ];
  const axelFirst = [
    { id: 'user-axel', name: 'Axel', color: '#00ff00' },
    { id: 'user-reda', name: 'Reda', color: '#ff0000' },
    { id: 'user-jules', name: 'Jules', color: '#0000ff' },
  ];

  assert.deepEqual(
    sortTeamMembersForSharedAttractorLayout(redaFirst).map((member) => member.id),
    sortTeamMembersForSharedAttractorLayout(axelFirst).map((member) => member.id),
  );
});

test('getDualUserSpacingDistance keeps a minimum but does not cap dense clusters to the viewport', () => {
  assert.equal(getDualUserSpacingDistance(1000, 800, 120, 120), 500);
  assert.equal(getDualUserSpacingDistance(2000, 1200, 120, 120), 500);
  assert.equal(getDualUserSpacingDistance(1000, 800, 260, 260), 780);
  assert.equal(getDualUserSpacingDistance(1000, 800, 999, 999), 2258);
});

test('createAttractors staggers 2-user suns and increases spacing with cluster extent', () => {
  const members = [
    { id: 'u1', name: 'Ada', color: '#ff0000' },
    { id: 'u2', name: 'Bea', color: '#00ff00' },
  ];

  const tight = createAttractors(members, 1000, 800, { clusterExtentByUserId: { u1: 140, u2: 140 } });
  const wide = createAttractors(members, 1000, 800, { clusterExtentByUserId: { u1: 260, u2: 260 } });

  assert.ok(tight[0].x < 500);
  assert.ok(tight[1].x > 500);
  assert.ok(tight[0].y > 400);
  assert.ok(tight[1].y < 400);
  assert.ok(wide[0].x < tight[0].x);
  assert.ok(wide[1].x > tight[1].x);
  assert.ok((wide[1].x - wide[0].x) > (tight[1].x - tight[0].x));

  const horizontalWideSpacing = wide[1].x - wide[0].x;
  assert.equal(Math.round(horizontalWideSpacing), getDualUserSpacingDistance(1000, 800, 260, 260));
});

test('createAttractors keeps placement density-based across viewport sizes', () => {
  const members = [
    { id: 'u1', name: 'Ada', color: '#ff0000' },
    { id: 'u2', name: 'Bea', color: '#00ff00' },
  ];
  const options = { clusterExtentByUserId: { u1: 260, u2: 260 } };
  const small = createAttractors(members, 1000, 800, options);
  const large = createAttractors(members, 1800, 1200, options);

  assert.equal(Math.round(small[1].x - small[0].x), Math.round(large[1].x - large[0].x));
  assert.equal(Math.round(small[0].y - small[1].y), Math.round(large[0].y - large[1].y));
});

test('computeAttractorViewportTransform can zoom out to fit dense two-user clusters', () => {
  const members = [
    { id: 'u1', name: 'Ada', color: '#ff0000' },
    { id: 'u2', name: 'Bea', color: '#00ff00' },
  ];
  const extents = { u1: 520, u2: 520 };
  const attractors = createAttractors(members, 1000, 800, { clusterExtentByUserId: extents });
  const transform = computeAttractorViewportTransform(attractors, 1000, 800, { clusterExtentByUserId: extents });

  assert.ok(transform.k < 0.5);
});

test('createAttractors uses a circular layout for 3 or more users and expands with cluster extent', () => {
  const members = [
    { id: 'u1', name: 'Ada', color: '#ff0000' },
    { id: 'u2', name: 'Bea', color: '#00ff00' },
    { id: 'u3', name: 'Cy', color: '#0000ff' },
  ];

  const compact = createAttractors(members, 1000, 800, { clusterExtentByUserId: { u1: 180, u2: 180, u3: 180 } });
  const expanded = createAttractors(members, 1000, 800, { clusterExtentByUserId: { u1: 320, u2: 320, u3: 320 } });

  assert.equal(compact[0].x, 500);
  assert.ok(compact[0].y < 400);
  assert.equal(Math.round((compact[1].x + compact[2].x) / 2), 500);
  assert.ok(compact[1].x > 500);
  assert.ok(compact[2].x < 500);
  assert.ok(expanded[0].y < compact[0].y);
});

test('createAttractors keeps multi-user radius density-based across viewport sizes', () => {
  const members = [
    { id: 'u1', name: 'Ada', color: '#ff0000' },
    { id: 'u2', name: 'Bea', color: '#00ff00' },
    { id: 'u3', name: 'Cy', color: '#0000ff' },
  ];
  const options = { clusterExtentByUserId: { u1: 320, u2: 320, u3: 320 } };
  const small = createAttractors(members, 1000, 800, options);
  const large = createAttractors(members, 1800, 1200, options);
  const smallRadius = Math.hypot(small[0].x - 500, small[0].y - 400);
  const largeRadius = Math.hypot(large[0].x - 900, large[0].y - 600);

  assert.equal(Math.round(smallRadius), Math.round(largeRadius));
});


test('nearestAttractorWithinRadius finds the recipient sun for drag handoff', () => {
  const attractors = [
    { id: 'owner', name: 'Owner', color: '#ff0000', initial: 'O', x: 100, y: 100 },
    { id: 'recipient', name: 'Recipient', color: '#00ff00', initial: 'R', x: 300, y: 100 },
  ];

  assert.equal(nearestAttractorWithinRadius(315, 110, attractors, 80)?.id, 'recipient');
});

test('nearestAttractorWithinRadius ignores drags outside all astres', () => {
  const attractors = [
    { id: 'owner', name: 'Owner', color: '#ff0000', initial: 'O', x: 100, y: 100 },
  ];

  assert.equal(nearestAttractorWithinRadius(260, 260, attractors, 80), null);
});

test('handoffTargetWithinRadius ignores the current owner astre', () => {
  const attractors = [
    { id: 'owner', name: 'Owner', color: '#ff0000', initial: 'O', x: 100, y: 100 },
    { id: 'recipient', name: 'Recipient', color: '#00ff00', initial: 'R', x: 300, y: 100 },
  ];

  assert.equal(handoffTargetWithinRadius(110, 105, attractors, 'owner', 80), null);
  assert.equal(handoffTargetWithinRadius(315, 110, attractors, 'owner', 80)?.id, 'recipient');
});

test('handoffTargetWithinRadius can select a recipient even when the owner is also inside the radius', () => {
  const attractors = [
    { id: 'owner', name: 'Owner', color: '#ff0000', initial: 'O', x: 100, y: 100 },
    { id: 'recipient', name: 'Recipient', color: '#00ff00', initial: 'R', x: 160, y: 100 },
  ];

  assert.equal(handoffTargetWithinRadius(125, 100, attractors, 'owner', 80)?.id, 'recipient');
});

test('orbitAnchorTargetWithinRadius resolves generic user and pin orbit targets', () => {
  const attractors = [
    { id: 'owner', kind: 'user', anchorId: 'owner', name: 'Owner', color: '#ff0000', initial: 'O', x: 100, y: 100 },
    { id: 'pin:marketing', kind: 'pin', anchorId: 'marketing', name: 'Marketing', color: '#57CFA0', initial: '', x: 180, y: 100 },
  ];

  assert.deepEqual(orbitAnchorRefForAttractor(attractors[1]), {
    kind: 'pin',
    id: 'marketing',
    key: 'pin:marketing',
  });
  assert.equal(orbitAnchorTargetWithinRadius(102, 100, attractors, 'owner', 90)?.id, 'pin:marketing');
  assert.equal(orbitAnchorTargetWithinRadius(102, 100, attractors, 'pin:marketing', 90)?.id, 'owner');
  assert.equal(orbitAnchorTargetWithinRadius(180, 100, attractors, null, 90)?.id, 'pin:marketing');
});

test('getAttractionTarget prefers an explicit pin orbit anchor over blob ownership', () => {
  const attractors = [
    { id: 'owner', kind: 'user', anchorId: 'owner', name: 'Owner', color: '#ff0000', initial: 'O', x: 100, y: 100 },
    { id: 'pin:marketing', kind: 'pin', anchorId: 'marketing', name: 'Marketing', color: '#57CFA0', initial: '', x: 420, y: 260 },
  ];
  const lookup = buildLookup(attractors);

  const target = getAttractionTarget(
    {
      id: 'idea-1',
      user_id: 'owner',
      orbit_anchor_type: 'pin',
      orbit_anchor_id: 'marketing',
    },
    lookup,
    0,
    0,
    'owner',
  );

  assert.equal(orbitAnchorKey('pin', 'marketing'), 'pin:marketing');
  assert.equal(target.x, 420);
  assert.equal(target.y, 260);
  assert.equal(target.suns[0]?.id, 'pin:marketing');
});

test('getAttractionTarget falls back to blob ownership when an explicit anchor is unavailable', () => {
  const attractors = [
    { id: 'owner', kind: 'user', anchorId: 'owner', name: 'Owner', color: '#ff0000', initial: 'O', x: 100, y: 100 },
  ];
  const lookup = buildLookup(attractors);

  const target = getAttractionTarget(
    {
      id: 'idea-1',
      user_id: 'owner',
      orbit_anchor_type: 'pin',
      orbit_anchor_id: 'missing',
    },
    lookup,
    0,
    0,
    'owner',
  );

  assert.equal(target.x, 100);
  assert.equal(target.y, 100);
  assert.equal(target.suns[0]?.id, 'owner');
});

test('orbit physics profiles keep pin thread lanes tighter than astre lanes', () => {
  const userProfile = orbitPhysicsProfileForKind('user');
  const pinProfile = orbitPhysicsProfileForKind('pin');

  assert.equal(orbitPhysicsProfileForKind(undefined), userProfile);
  assert.equal(orbitPhysicsProfileForAttractor({ kind: 'pin' }), pinProfile);
  assert.ok(pinProfile.threadOrbitBaseRadius < userProfile.threadOrbitBaseRadius);
  assert.ok(pinProfile.threadOrbitRingGap < userProfile.threadOrbitRingGap);
  assert.ok(pinProfile.repulsionFieldRadius < userProfile.repulsionFieldRadius);
});
