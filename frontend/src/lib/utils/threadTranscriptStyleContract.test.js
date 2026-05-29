import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const threadTranscriptPath = resolve(
  __dirname,
  '../features/threads/components/ThreadTranscript.svelte',
);
const globalComponentsPath = resolve(__dirname, '../styles/components.css');

function styleRule(source, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  return match?.[1] ?? '';
}

test('thread transcript messages own box metrics inside the component', () => {
  const source = readFileSync(threadTranscriptPath, 'utf8');
  const rule = styleRule(source, '.thread-message');

  assert.match(rule, /padding:\s*0\s*;/);
  assert.match(rule, /margin-bottom:\s*0\s*;/);
  assert.match(rule, /border-radius:\s*0\s*;/);
});

test('global component styles do not define generic thread-message rules', () => {
  const source = readFileSync(globalComponentsPath, 'utf8');

  assert.doesNotMatch(source, /(^|\n)\s*\.thread-message(?:[^\w-]|\s*\{)/);
});
