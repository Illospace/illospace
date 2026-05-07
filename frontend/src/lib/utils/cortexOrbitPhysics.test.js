import test from 'node:test';
import assert from 'node:assert/strict';

import { cortexOrbitPerformanceProfile } from './cortexOrbitPhysics.ts';

test('orbit performance profiles keep a nonzero idle alpha target above alphaMin', () => {
  for (const nodeCount of [1, 60, 120, 220]) {
    const profile = cortexOrbitPerformanceProfile(nodeCount);
    assert.ok(
      profile.idleAlphaTarget > profile.alphaMin,
      `nodeCount=${nodeCount} should not let the orbit simulation cool below alphaMin`,
    );
  }
});

test('orbit idle heat tapers down for dense workspaces', () => {
  assert.ok(cortexOrbitPerformanceProfile(1).idleAlphaTarget > cortexOrbitPerformanceProfile(60).idleAlphaTarget);
  assert.ok(cortexOrbitPerformanceProfile(60).idleAlphaTarget > cortexOrbitPerformanceProfile(120).idleAlphaTarget);
  assert.ok(cortexOrbitPerformanceProfile(120).idleAlphaTarget > cortexOrbitPerformanceProfile(220).idleAlphaTarget);
});
