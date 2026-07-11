import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const workspaceSceneSource = readFileSync(
  new URL('../features/workspace-scene/components/WorkspaceScene.svelte', import.meta.url),
  'utf8',
);

function functionSource(name) {
  const start = workspaceSceneSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `${name} should exist`);

  let parameterDepth = 0;
  let bodyStart = -1;
  for (let index = start; index < workspaceSceneSource.length; index += 1) {
    const char = workspaceSceneSource[index];
    if (char === '(') parameterDepth += 1;
    else if (char === ')') parameterDepth -= 1;
    else if (char === '{' && parameterDepth === 0) {
      bodyStart = index;
      break;
    }
  }
  assert.notEqual(bodyStart, -1, `${name} body should exist`);

  let depth = 0;
  for (let index = bodyStart; index < workspaceSceneSource.length; index += 1) {
    const char = workspaceSceneSource[index];
    if (char === '{') {
      depth += 1;
    } else if (char === '}') {
      depth -= 1;
      if (depth === 0) return workspaceSceneSource.slice(start, index + 1);
    }
  }

  throw new Error(`${name} source is incomplete`);
}

test('workspace orbit heat always cools to zero and only runs while visible', () => {
  const heatOrbit = functionSource('heatOrbit');

  assert.match(heatOrbit, /\.alphaTarget\(0\)/);
  assert.match(heatOrbit, /Math\.max\(simulation\.alpha\(\), alpha\)/);
  assert.match(heatOrbit, /document\.visibilityState !== 'visible'/);
  assert.match(heatOrbit, /schedulePrimitiveOrbitVisuals/);
  assert.match(heatOrbit, /simulation\.restart\(\)/);
  assert.ok(heatOrbit.indexOf('.alphaTarget(0)') < heatOrbit.indexOf("visibilityState !== 'visible'"));
  assert.ok(heatOrbit.indexOf("visibilityState !== 'visible'") < heatOrbit.indexOf('simulation.restart()'));
});

test('workspace orbit has no permanent heat, random force, or periodic wake machinery', () => {
  assert.doesNotMatch(workspaceSceneSource, /\.force\('brownian'/);
  const alphaTargets = Array.from(
    workspaceSceneSource.matchAll(/\.alphaTarget\(([^)]*)\)/g),
    (match) => match[1].trim(),
  );
  assert.deepEqual(alphaTargets, ['0', '0']);
  assert.equal(workspaceSceneSource.match(/simulation\.restart\(\)/g)?.length, 1);
  assert.doesNotMatch(workspaceSceneSource, /workspaceMotionSuspended|suspendWorkspaceMotion|resumeWorkspaceMotion/);
  assert.doesNotMatch(workspaceSceneSource, /lastOrbitTickAt|ORBIT_WAKE_CHECK_INTERVAL_MS|ORBIT_STALE_AFTER_MS|orbitWakeCheckInterval|checkOrbitMotionHealth/);
});

test('workspace visibility stops hidden work and resumes only retained heat', () => {
  const visibility = functionSource('handleVisibilityChange');
  const mount = workspaceSceneSource.match(/onMount\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';

  assert.match(visibility, /visibilityState === 'hidden'/);
  assert.match(visibility, /simulation\.stop\(\)/);
  assert.match(visibility, /cancelPrimitiveOrbitVisualSync\(\)/);
  assert.match(visibility, /simulation\.alpha\(\) < simulation\.alphaMin\(\)/);
  assert.match(visibility, /heatOrbit\(0\)/);
  assert.match(workspaceSceneSource, /simulation = createSim[\s\S]*?paused \|\| document\.visibilityState !== 'visible'\) simulation\.stop\(\)/);
  assert.doesNotMatch(mount, /handleVisibilityChange\(\)/);
});

test('workspace pause survives rebuilds and restores an open thread anchor', () => {
  const togglePause = functionSource('handleTogglePause');
  const resetRuntime = functionSource('resetRenderRuntime');
  const renderCanvas = functionSource('renderCanvas');
  const restoreThreadAnchor = functionSource('restoreThreadAnchorState');
  const syncThreadAnchor = functionSource('syncThreadAnchor');

  assert.match(togglePause, /if \(paused\)[\s\S]*restoreThreadAnchorState\([\s\S]*stopActiveSimulation\(\)/);
  assert.match(togglePause, /else \{\s*renderCanvas\(\{ preserveViewport: true \}\);\s*\}/);
  assert.match(resetRuntime, /restoreThreadAnchorState\([\s\S]*stopActiveSimulation\(\)/);
  assert.match(renderCanvas, /untrack\(syncThreadAnchor\)/);
  assert.ok(restoreThreadAnchor.indexOf('previousNode.fx = previous.fx') < restoreThreadAnchor.indexOf('threadAnchorState = null'));
  assert.ok(restoreThreadAnchor.indexOf('previousNode.fy = previous.fy') < restoreThreadAnchor.indexOf('threadAnchorState = null'));
  assert.ok(restoreThreadAnchor.indexOf('delete previousNode._threadAnchorPinned') < restoreThreadAnchor.indexOf('threadAnchorState = null'));
  assert.match(syncThreadAnchor, /if \(destroyed \|\| !simulation\) return;/);
  assert.match(workspaceSceneSource, /if \(paused \|\| document\.visibilityState !== 'visible'\) simulation\.stop\(\)/);
});

test('workspace settle finalizes primitive motion and persists once', () => {
  const endHandler = workspaceSceneSource.match(/simulation\.on\('end', \(\) => \{[\s\S]*?\n    \}\);/)?.[0] ?? '';

  assert.match(endHandler, /cancelPrimitiveOrbitVisualSync\(\)/);
  assert.match(endHandler, /syncPrimitiveMotionVisuals\(nodes\)/);
  assert.match(endHandler, /persistSceneIdeaPositions\(positions\)/);
  assert.equal(endHandler.match(/syncPrimitiveMotionVisuals\(nodes\)/g)?.length, 1);
  assert.equal(endHandler.match(/persistSceneIdeaPositions\(positions\)/g)?.length, 1);
});

test('workspace focus flow is event-driven instead of polled', () => {
  const updateFlow = functionSource('updateFlowState');

  assert.match(updateFlow, /if \(destroyed\) return;/);
  assert.match(workspaceSceneSource, /document\.addEventListener\('focusin', handleFlowFocusChange\)/);
  assert.match(workspaceSceneSource, /document\.addEventListener\('focusout', handleFlowFocusChange\)/);
  assert.match(workspaceSceneSource, /document\.removeEventListener\('focusin', handleFlowFocusChange\)/);
  assert.match(workspaceSceneSource, /document\.removeEventListener\('focusout', handleFlowFocusChange\)/);
  assert.match(workspaceSceneSource, /document\.removeEventListener\('visibilitychange', handleVisibilityChange\)/);
  assert.match(workspaceSceneSource, /window\.removeEventListener\('cortex-fit-view', handleFitView\)/);
  assert.match(workspaceSceneSource, /window\.removeEventListener\('cortex-toggle-pause', handleTogglePause\)/);
  assert.doesNotMatch(workspaceSceneSource, /flowCheckInterval|setInterval\(checkFlowState/);
});

test('workspace topology and app wakes are signature-deduplicated', () => {
  assert.match(workspaceSceneSource, /nextTopologyKey === connectionTopologyKey/);
  assert.match(workspaceSceneSource, /linkForce\?\.links\(workspaceConnectionLinkData\(visible\)\)/);
  assert.equal(workspaceSceneSource.match(/linkForce\?\.links\(workspaceConnectionLinkData\(visible\)\)/g)?.length, 1);
  assert.match(workspaceSceneSource, /if \(layoutChanged\) heatOrbit\(0\.12\)/);
  assert.doesNotMatch(workspaceSceneSource, /app\.updated_at/);
});

test('initial presence seeds without heat and later signature changes heat once', () => {
  const presenceEffect = workspaceSceneSource.match(
    /const typingEntries = Array\.from\(cortex\.typingUsers\.values\(\)\)[\s\S]*?heatOrbit\(0\.08\);/,
  )?.[0] ?? '';

  assert.match(workspaceSceneSource, /let presenceMotionKey: string \| null = null;/);
  assert.match(presenceEffect, /`\$\{entry\.user_id\}:\$\{entry\.idea_id\}`/);
  assert.match(presenceEffect, /new Set\(\s*presenceStore\.viewers\.map\(\(entry\) => entry\.user_id\)/);
  assert.match(presenceEffect, /const initialPresence = presenceMotionKey === null;/);
  assert.match(presenceEffect, /if \(!presenceChanged\) return;/);
  assert.match(presenceEffect, /presenceMotionKey = nextPresenceMotionKey;\s*refreshOrbitVisualState\(\);\s*if \(!initialPresence\) heatOrbit\(0\.08\);/);
});

test('accepted archive mutations start before teardown-stoppable visual frames', () => {
  assert.match(workspaceSceneSource, /let destroyed = false;/);
  assert.match(workspaceSceneSource, /onDestroy\(\(\) => \{\s*destroyed = true;/);

  const localFrameFunctions = [
    ['popPrimitiveBlob', 'tick', 'cortex.deleteIdea(nodeId)'],
    ['animatePinDeleteToBin', 'archiveTick', 'onpindelete({ pinId: pin.pinId })'],
    ['animateAppArchiveToBin', 'archiveTick', 'onapparchive?.({ appId: app.id })'],
    ['animateArchiveToBin', 'archiveTick', 'cortex.deleteIdea(d.id)'],
  ];
  for (const [owner, callback, mutation] of localFrameFunctions) {
    const source = functionSource(owner);
    assert.match(
      source,
      new RegExp(`function ${callback}\\([^)]*\\) \\{\\s*if \\(destroyed\\) return;`),
      `${owner} should stop its local frame after teardown`,
    );
    assert.ok(
      source.indexOf(mutation) < source.indexOf(`requestAnimationFrame(${callback})`),
      `${owner} should start its accepted mutation before its first visual frame`,
    );
  }
});
