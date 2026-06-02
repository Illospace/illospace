import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';
import assert from 'node:assert/strict';

const srcDir = resolve(import.meta.dirname, '..');

function readSource(relativePath) {
  return readFileSync(resolve(srcDir, relativePath), 'utf8');
}

test('thread route changes open direct threads even when absent from active ideas', () => {
  const routeSource = readSource('features/cortex/components/CortexWorkspaceRoute.svelte');

  assert.match(
    routeSource,
    /async function maybeSelectIdeaFromUrl\(\)[\s\S]*await cortex\.loadDirectThread\(requestedIdeaId\);/,
  );
  assert.doesNotMatch(
    routeSource,
    /maybeSelectIdeaFromUrl\(\)[\s\S]*cortex\.ideas\.some\(\(idea\) => idea\.id === requestedIdeaId\)/,
  );
});

test('direct thread loader can recover selected metadata outside bootstrap', () => {
  const storeSource = readSource('stores/cortex.svelte.ts');

  assert.match(
    storeSource,
    /async loadDirectThread\(id: string\)[\s\S]*api\.getIdea\(id\)[\s\S]*api\.unifiedStream\(id\)/,
  );
});
