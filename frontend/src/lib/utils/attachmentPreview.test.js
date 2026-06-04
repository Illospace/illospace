import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ATTACHMENT_INPUT_ACCEPT,
  attachmentPreviewKind,
  messageLinkAttachments,
  normalizeServerUploadPreviewUrl,
} from './attachmentPreview.ts';

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

test('accepts and previews SVG attachments as images', () => {
  assert.ok(ATTACHMENT_INPUT_ACCEPT.includes('image/svg+xml'));
  assert.ok(ATTACHMENT_INPUT_ACCEPT.includes('.svg'));
  assert.equal(attachmentPreviewKind({ url: '/static/uploads/diagram.svg' }), 'image');
});

test('promotes static upload links from message text into attachments', () => {
  const attachments = messageLinkAttachments('See /static/uploads/thread-assets/t/diagram.svg');

  assert.deepEqual(attachments, [
    {
      kind: 'file',
      url: '/static/uploads/thread-assets/t/diagram.svg',
      filename: '/static/uploads/thread-assets/t/diagram.svg',
    },
  ]);
});
