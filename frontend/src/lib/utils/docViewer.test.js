import test from 'node:test';
import assert from 'node:assert/strict';

import {
  docViewerFilename,
  docViewerRenderMode,
  docViewerTitle,
  firstMarkdownHeading,
  normalizeDocViewerSrc,
} from './docViewer.ts';

test('accepts only /static/uploads sources', () => {
  assert.equal(
    normalizeDocViewerSrc('/static/uploads/thread-assets/t1/prd-abc123.md'),
    '/static/uploads/thread-assets/t1/prd-abc123.md',
  );
  assert.equal(normalizeDocViewerSrc('  /static/uploads/a.md  '), '/static/uploads/a.md');

  assert.equal(normalizeDocViewerSrc(null), null);
  assert.equal(normalizeDocViewerSrc(''), null);
  assert.equal(normalizeDocViewerSrc('/etc/passwd'), null);
  assert.equal(normalizeDocViewerSrc('https://evil.example/static/uploads/a.md'), null);
  assert.equal(normalizeDocViewerSrc('//evil.example/static/uploads/a.md'), null);
  assert.equal(normalizeDocViewerSrc('/static/uploads/../secrets.md'), null);
  assert.equal(normalizeDocViewerSrc('/static/uploads/a/../../b.md'), null);
  assert.equal(normalizeDocViewerSrc('/static/uploads//double.md'), null);
  assert.equal(normalizeDocViewerSrc('/static/uploads/dir/'), null);
  assert.equal(normalizeDocViewerSrc('/static/uploads/a.md?x=1'), null);
  assert.equal(normalizeDocViewerSrc('/static/uploads/a.md#frag'), null);
  assert.equal(normalizeDocViewerSrc('/static/uploads\\a.md'), null);
});

test('rejects percent-encoded traversal while preserving valid encoded paths', () => {
  assert.equal(
    normalizeDocViewerSrc('/static/uploads/%252e%252e/%252e%252e/api/runtime/settings'),
    null,
  );
  assert.equal(normalizeDocViewerSrc('/static/uploads/%2e%2e/x'), null);
  assert.equal(
    normalizeDocViewerSrc('/static/uploads/foo/My%20Doc.md'),
    '/static/uploads/foo/My%20Doc.md',
  );
  assert.equal(
    normalizeDocViewerSrc('/static/uploads/thread-assets/shared/prd-abc.md'),
    '/static/uploads/thread-assets/shared/prd-abc.md',
  );
});

test('picks a render mode from the file extension', () => {
  assert.equal(docViewerRenderMode('/static/uploads/t/prd.md'), 'markdown');
  assert.equal(docViewerRenderMode('/static/uploads/t/prd.MARKDOWN'), 'markdown');
  assert.equal(docViewerRenderMode('/static/uploads/t/spec.pdf'), 'pdf');
  for (const extension of ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'svg']) {
    assert.equal(docViewerRenderMode(`/static/uploads/t/image.${extension}`), 'image');
  }
  assert.equal(docViewerRenderMode('/static/uploads/t/data.csv'), 'text');
  assert.equal(docViewerRenderMode('/static/uploads/t/noext'), 'text');
});

test('extracts the first markdown heading and strips inline markers', () => {
  assert.equal(firstMarkdownHeading('intro\n\n## Chantier **Mémoire** PRD ##\ntext'), 'Chantier Mémoire PRD');
  assert.equal(firstMarkdownHeading('no headings here'), '');
  assert.equal(firstMarkdownHeading(''), '');
});

test('decodes the filename segment of a src path', () => {
  assert.equal(docViewerFilename('/static/uploads/t/aws%20mini.svg'), 'aws mini.svg');
  assert.equal(docViewerFilename(null), '');
});

test('title prefers the explicit param, then heading, then filename', () => {
  assert.equal(docViewerTitle('Chantier PRD', '/static/uploads/t/x.md', '# Other'), 'Chantier PRD');
  assert.equal(docViewerTitle('', '/static/uploads/t/x.md', '# From Heading'), 'From Heading');
  assert.equal(docViewerTitle(null, '/static/uploads/t/plan%20v2.md', 'plain text'), 'plan v2.md');
  assert.equal(docViewerTitle(null, null, ''), 'Shared document');
});
