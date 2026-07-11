import test from 'node:test';
import assert from 'node:assert/strict';
import { forceSimulation } from 'd3-force';

import { cortexOrbitPerformanceProfile } from './cortexOrbitPhysics.ts';

test('orbit performance profiles cool naturally with a zero alpha target', () => {
  for (const nodeCount of [1, 60, 120, 220]) {
    const profile = cortexOrbitPerformanceProfile(nodeCount);
    assert.equal('idleAlphaTarget' in profile, false, `nodeCount=${nodeCount} must not encode perpetual heat`);

    for (const initialAlpha of [1, 0.14]) {
      const simulation = forceSimulation([])
        .stop()
        .alpha(initialAlpha)
        .alphaDecay(profile.alphaDecay)
        .alphaMin(profile.alphaMin)
        .alphaTarget(0);
      for (let ticks = 0; simulation.alpha() >= profile.alphaMin && ticks < 1600; ticks += 1) {
        simulation.tick();
      }
      assert.ok(simulation.alpha() < profile.alphaMin, `nodeCount=${nodeCount} alpha=${initialAlpha} did not converge`);
    }
  }
});
