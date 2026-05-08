import test from 'node:test';
import assert from 'node:assert/strict';
import {
  filterProjectContextUploadEntries,
  PROJECT_CONTEXT_UPLOAD_MAX_FILES,
  PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE,
  PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE,
} from './projectContextLocal.ts';

function entry(relativePath, size = 100) {
  return {
    file: {
      name: relativePath.split('/').at(-1) || 'file',
      size,
    },
    relativePath,
  };
}

test('keeps arbitrary document types before sending multipart data', () => {
  const result = filterProjectContextUploadEntries([
    entry('src/App.svelte'),
    entry('reference/karoid_ai.pdf'),
    entry('research/custom.dataset'),
    entry('docs/wireframe.png'),
    entry('docs/large.md', PROJECT_CONTEXT_UPLOAD_MAX_FILE_SIZE + 1),
  ]);

  assert.deepEqual(result.entries.map((item) => item.relativePath), [
    'src/App.svelte',
    'reference/karoid_ai.pdf',
    'research/custom.dataset',
    'docs/wireframe.png',
  ]);
  assert.equal(result.skippedFiles.length, 1);
  assert.match(result.skippedFiles[0].reason, /larger than/);
});

test('caps local Project Context uploads to backend limits', () => {
  const result = filterProjectContextUploadEntries(
    Array.from({ length: PROJECT_CONTEXT_UPLOAD_MAX_FILES + 2 }, (_, index) => entry(`src/file-${index}.ts`)),
  );

  assert.equal(result.entries.length, PROJECT_CONTEXT_UPLOAD_MAX_FILES);
  assert.equal(result.skippedFiles.length, 2);
  assert.match(result.skippedFiles[0].reason, /Only the first 200/);
});

test('caps local Project Context upload total size', () => {
  const fileSize = 1_000_000;
  const result = filterProjectContextUploadEntries(
    Array.from({ length: PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE / fileSize + 1 }, (_, index) => entry(`docs/file-${index}.md`, fileSize)),
  );

  assert.equal(result.entries.length, PROJECT_CONTEXT_UPLOAD_MAX_TOTAL_SIZE / fileSize);
  assert.equal(result.skippedFiles.length, 1);
  assert.match(result.skippedFiles[0].reason, /capped at 20 MB/);
});
