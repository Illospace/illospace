import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeServerUploadPreviewUrl } from './attachmentPreview.ts';

test('normalizes same-origin server upload preview links', () => {
  assert.equal(
    normalizeServerUploadPreviewUrl('https://illo.local/static/uploads/spec.md?download=1#top', 'https://illo.local'),
    '/static/uploads/spec.md?download=1#top',
  );
  assert.equal(
    normalizeServerUploadPreviewUrl('/static/uploads/spec.md', 'https://illo.local'),
    '/static/uploads/spec.md',
  );
});

test('keeps off-origin upload-looking links external', () => {
  assert.equal(
    normalizeServerUploadPreviewUrl('https://docs.example.com/static/uploads/spec.md', 'https://illo.local'),
    '',
  );
});
