import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeReadableMarkdown, renderReadableMarkdown } from './readableMarkdown.ts';

test('normalizes punctuation-only lines from LLM replies', () => {
  const raw = [
    'Coordinator-visible phase metadata shows:',
    '',
    '`schema_valid=True`',
    '',
    ',',
    '',
    '`schema_source=worker_output`',
  ].join('\n');

  assert.equal(
    normalizeReadableMarkdown(raw),
    'Coordinator-visible phase metadata shows:\n\n`schema_valid=True`, `schema_source=worker_output`',
  );
});

test('renders inline code inside paragraphs instead of as standalone blocks', () => {
  const html = renderReadableMarkdown('Evidence: `schema_valid=True`, `files=30`');

  assert.equal(
    html,
    '<p>Evidence: <code class="md-inline-code">schema_valid=True</code>, <code class="md-inline-code">files=30</code></p>',
  );
});

test('renders compact markdown lists and escapes unsafe html', () => {
  const html = renderReadableMarkdown('What is proven:\n- worker evidence exists\n- <script>alert(1)</script>');

  assert.equal(
    html,
    '<p>What is proven:</p><ul><li>worker evidence exists</li><li>&lt;script&gt;alert(1)&lt;/script&gt;</li></ul>',
  );
});

test('continues repeated ordered list markers across explanatory paragraphs', () => {
  const html = renderReadableMarkdown([
    '1. **First decision**',
    '',
    'Reasoning for the first item.',
    '',
    '1. **Second decision**',
    '',
    'Reasoning for the second item.',
    '',
    '1. **Third decision**',
  ].join('\n'));

  assert.equal(
    html,
    '<ol><li><strong>First decision</strong></li></ol><p>Reasoning for the first item.</p><ol start="2"><li><strong>Second decision</strong></li></ol><p>Reasoning for the second item.</p><ol start="3"><li><strong>Third decision</strong></li></ol>',
  );
});

test('renders bare urls without double-linking markdown links', () => {
  const html = renderReadableMarkdown('Open https://example.com/docs. Also [site](https://example.com).');

  assert.equal(
    html,
    '<p>Open <a href="https://example.com/docs" target="_blank" rel="noopener">https://example.com/docs</a>. Also <a href="https://example.com" target="_blank" rel="noopener">site</a>.</p>',
  );
});

test('preserves server-relative markdown links', () => {
  const html = renderReadableMarkdown('Open [PRD](/static/uploads/prd.md).');

  assert.equal(
    html,
    '<p>Open <a href="/static/uploads/prd.md" target="_blank" rel="noopener">PRD</a>.</p>',
  );
});

test('renders server upload markdown images as inline preview links', () => {
  const html = renderReadableMarkdown('![AWS mini diagram](/static/uploads/thread-assets/t/aws.svg)');

  assert.equal(
    html,
    '<p><a class="md-readable-image-link" href="/static/uploads/thread-assets/t/aws.svg" target="_blank" rel="noopener"><img class="md-readable-image" src="/static/uploads/thread-assets/t/aws.svg" alt="AWS mini diagram" loading="lazy" decoding="async"/></a></p>',
  );
});

test('does not render remote markdown images inline', () => {
  const html = renderReadableMarkdown('![Remote diagram](https://example.com/aws.svg)');

  assert.equal(html, '<p>Remote diagram</p>');
});

test('preserves query parameters in bare url hrefs', () => {
  const html = renderReadableMarkdown('Open https://example.com/search?q=illo&sort=new');

  assert.equal(
    html,
    '<p>Open <a href="https://example.com/search?q=illo&amp;sort=new" target="_blank" rel="noopener">https://example.com/search?q=illo&amp;sort=new</a></p>',
  );
});
