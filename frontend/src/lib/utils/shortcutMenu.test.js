import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  anchoredShortcutMenuGeometry,
  shortcutMenuCssVariables,
} from '../features/composer/domain/shortcutMenu.ts';

const utilsDir = dirname(fileURLToPath(import.meta.url));
const srcDir = resolve(utilsDir, '..', '..');

function readSource(relativePath) {
  return readFileSync(resolve(srcDir, relativePath), 'utf8');
}

test('shortcut menu geometry flips below when there is not enough room above', () => {
  const geometry = anchoredShortcutMenuGeometry(
    { top: 24, bottom: 72, left: 20, width: 300 },
    480,
    600,
    { placement: 'above', preferredHeight: 160, minHeight: 96, maxHeight: 220 },
  );

  assert.equal(geometry.placement, 'below');
  assert.equal(geometry.top, 80);
  assert.equal(geometry.bottom, null);
  assert.equal(geometry.left, 20);
  assert.equal(geometry.width, 300);
});

test('shortcut menu css variables always encode top and bottom closure states', () => {
  const style = shortcutMenuCssVariables(
    {
      placement: 'above',
      left: 12,
      width: 320,
      maxHeight: 180,
      top: null,
      bottom: 64,
    },
    'shortcut',
  );

  assert.match(style, /--shortcut-dropdown-top:auto/);
  assert.match(style, /--shortcut-dropdown-bottom:64px/);
});

test('composer shortcut menus share the same body portal action', () => {
  const slashSource = readSource('lib/features/composer/components/SlashAutocomplete.svelte');
  const mentionSource = readSource('lib/features/composer/components/MentionAutocomplete.svelte');
  const portalSource = readSource('lib/features/composer/domain/shortcutMenu.ts');

  assert.match(slashSource, /use:shortcutMenuPortal/);
  assert.match(mentionSource, /use:shortcutMenuPortal/);
  assert.match(portalSource, /document\.body\.appendChild\(node\)/);
  assert.match(portalSource, /destroy\(\)\s*\{\s*node\.remove\(\);\s*\}/);
  assert.doesNotMatch(slashSource, /insertBefore\(node/);
  assert.doesNotMatch(mentionSource, /insertBefore\(node/);
});
