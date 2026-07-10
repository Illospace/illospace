import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import zlib from 'node:zlib';

export const THREAD_STAGE_GZIP_BUDGET_BYTES = 250_000;

export function measureThreadStageStaticClosure(clientRoot) {
  const manifestPath = path.join(clientRoot, '.vite', 'manifest.json');
  if (!fs.existsSync(manifestPath)) {
    throw new Error(`Missing Vite manifest at ${manifestPath}. Run npm run build first.`);
  }

  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const entries = Object.values(manifest);
  const entry = entries.find(
    (candidate) => candidate?.name === 'ThreadStageScreen' && candidate?.isDynamicEntry,
  );
  if (!entry) throw new Error('ThreadStageScreen dynamic entry is missing from the Vite manifest.');

  const entriesByFile = new Map(entries.map((candidate) => [candidate.file, candidate]));
  const visitedEntries = new Set();
  const staticFiles = new Set();

  function visit(candidate) {
    if (!candidate?.file || visitedEntries.has(candidate.file)) return;
    visitedEntries.add(candidate.file);
    staticFiles.add(candidate.file);
    for (const cssFile of candidate.css ?? []) staticFiles.add(cssFile);
    for (const importedEntry of candidate.imports ?? []) {
      visit(manifest[importedEntry] ?? entriesByFile.get(importedEntry));
    }
  }

  visit(entry);

  let rawBytes = 0;
  let gzipBytes = 0;
  for (const relativeFile of staticFiles) {
    const content = fs.readFileSync(path.join(clientRoot, relativeFile));
    rawBytes += content.byteLength;
    gzipBytes += zlib.gzipSync(content).byteLength;
  }

  return {
    entryFile: entry.file,
    fileCount: staticFiles.size,
    rawBytes,
    gzipBytes,
  };
}

function main() {
  const scriptDir = path.dirname(fileURLToPath(import.meta.url));
  const clientRoot = path.resolve(scriptDir, '../.svelte-kit/output/client');
  const measurement = measureThreadStageStaticClosure(clientRoot);
  const delta = measurement.gzipBytes - THREAD_STAGE_GZIP_BUDGET_BYTES;
  console.log(JSON.stringify({
    ...measurement,
    budgetBytes: THREAD_STAGE_GZIP_BUDGET_BYTES,
    passed: delta <= 0,
    deltaBytes: delta,
  }, null, 2));

  if (delta > 0) {
    console.error(`ThreadStage static closure exceeds its gzip budget by ${delta} bytes.`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
