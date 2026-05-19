import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  findFirstSlashCommandToken,
  findSlashCommandToken,
  replaceSlashCommandToken,
} from './slashCommand.ts';

const utilsDir = dirname(fileURLToPath(import.meta.url));
const srcDir = resolve(utilsDir, '..', '..');

function readSource(relativePath) {
  return readFileSync(resolve(srcDir, relativePath), 'utf8');
}

function cssRule(source, selector) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`${escapedSelector}\\s*\\{(?<body>[^}]*)\\}`, 'm'));
  return match?.groups?.body ?? '';
}

test('findSlashCommandToken detects a slash command at the cursor inside a sentence', () => {
  const value = 'can you use /dev to fix this?';
  const cursor = value.indexOf('/dev') + '/dev'.length;
  const token = findSlashCommandToken(value, cursor);

  assert.deepEqual(token, {
    start: value.indexOf('/dev'),
    end: value.indexOf('/dev') + '/dev'.length,
    query: 'dev',
  });
});

test('findSlashCommandToken replaces an entire partially typed token', () => {
  const value = 'please /devlop this';
  const cursor = value.indexOf('/dev') + '/dev'.length;
  const token = findSlashCommandToken(value, cursor);

  assert.ok(token);
  assert.equal(value.slice(token.start, token.end), '/devlop');
});

test('findSlashCommandToken ignores slashes embedded in words and paths', () => {
  assert.equal(findSlashCommandToken('hello/world', 'hello/world'.length), null);
  assert.equal(findSlashCommandToken('open foo/bar', 'open foo/bar'.length), null);
  assert.equal(findFirstSlashCommandToken('open /api/foo'), null);
});

test('replaceSlashCommandToken inserts the selected skill without replacing the sentence', () => {
  const value = 'can you use /dev to fix this?';
  const cursor = value.indexOf('/dev') + '/dev'.length;
  const token = findSlashCommandToken(value, cursor);

  assert.ok(token);
  assert.deepEqual(replaceSlashCommandToken(value, token, 'develop'), {
    value: 'can you use /develop to fix this?',
    cursor: 'can you use /develop'.length,
  });
});

test('replaceSlashCommandToken adds a spacer when selecting at the end', () => {
  const value = 'please /dev';
  const token = findSlashCommandToken(value, value.length);

  assert.ok(token);
  assert.deepEqual(replaceSlashCommandToken(value, token, '/develop '), {
    value: 'please /develop ',
    cursor: 'please /develop '.length,
  });
});

test('findFirstSlashCommandToken sees inline commands beyond the end cursor case', () => {
  const value = 'can you use /develop to fix this?';
  const token = findFirstSlashCommandToken(value);

  assert.ok(token);
  assert.equal(value.slice(token.start, token.end), '/develop');
});

test('slash autocomplete is anchored to the viewport instead of composer overflow boxes', () => {
  const source = readSource('lib/features/composer/components/SlashAutocomplete.svelte');

  assert.match(source, /document\.body\.appendChild\(node\)/);
  assert.match(source, /position:\s*fixed;/);
  assert.match(source, /--slash-dropdown-left/);
  assert.match(source, /--slash-dropdown-width/);
});

test('thread composer editor does not clip overlay hosts', () => {
  const source = readSource('lib/features/composer/components/WorkspaceComposerAdapter.svelte');
  const rule = cssRule(source, '.thread-mode .composer-editor');

  assert.match(rule, /overflow:\s*visible/);
  assert.doesNotMatch(rule, /overflow:\s*hidden/);
});
