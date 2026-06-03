import test from 'node:test';
import assert from 'node:assert/strict';

import { safeVisualImageSrc } from './visualImageSource.ts';

test('encodes sanitized inline svg content as an image data url', () => {
  const source = '<svg><script>alert(1)</script><path d="M0 0h1v1z"/></svg>';
  const imageSrc = safeVisualImageSrc(source, {
    sanitizeSvg: (content) => content.replace(/<script[\s\S]*?<\/script>/gi, ''),
  });

  assert.ok(imageSrc.startsWith('data:image/svg+xml;charset=utf-8,'));
  const decoded = decodeURIComponent(imageSrc.split(',')[1]);
  assert.equal(decoded, '<svg><path d="M0 0h1v1z"/></svg>');
});

test('preserves already-addressable image urls', () => {
  assert.equal(safeVisualImageSrc('https://example.com/chart.png'), 'https://example.com/chart.png');
  assert.equal(safeVisualImageSrc('data:image/png;base64,cG5n'), 'data:image/png;base64,cG5n');
  assert.equal(safeVisualImageSrc('blob:https://example.com/graph'), 'blob:https://example.com/graph');
});

test('rejects non-image content and svg sanitized into non-svg content', () => {
  assert.equal(safeVisualImageSrc('console.log("not an image")'), '');
  assert.equal(safeVisualImageSrc('<svg><path /></svg>', { sanitizeSvg: () => '<p>nope</p>' }), '');
});
