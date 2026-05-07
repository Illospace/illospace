import test from 'node:test';
import assert from 'node:assert/strict';

import {
  bindingAllowsField,
  bindingAllowsOperation,
  inlineThumbnailSource,
  resolveDomainBinding,
  structuredThumbnailSpec,
} from './generatedWorkspaceAppContract.ts';

const manifest = {
  data_plan: {
    bindings: {
      records: {
        domain_id: 42,
        object_key: 'record',
        fields: ['title', 'status'],
        operations: ['query', 'create'],
      },
    },
  },
};

test('resolveDomainBinding accepts bound aliases and allowed operations', () => {
  assert.deepEqual(resolveDomainBinding(manifest, { alias: 'records' }, 'query'), {
    domainId: 42,
    objectKey: 'record',
  });
});

test('resolveDomainBinding denies unbound aliases and disallowed operations', () => {
  assert.throws(
    () => resolveDomainBinding(manifest, { alias: 'missing' }, 'query'),
    /not bound/,
  );
  assert.throws(
    () => resolveDomainBinding(manifest, { alias: 'records' }, 'archive'),
    /not allowed/,
  );
});

test('binding helpers keep write affordances inside manifest permissions', () => {
  const binding = manifest.data_plan.bindings.records;

  assert.equal(bindingAllowsOperation(binding, 'create'), true);
  assert.equal(bindingAllowsOperation(binding, 'update'), false);
  assert.equal(bindingAllowsField(binding, 'status'), true);
  assert.equal(bindingAllowsField(binding, 'notes'), false);
  assert.equal(bindingAllowsField({ operations: ['update'] }, 'notes'), true);
});

test('structuredThumbnailSpec renders metadata and ignores iframe thumbnail HTML', () => {
  assert.deepEqual(
    structuredThumbnailSpec(
      {
        thumbnail: {
          label: 'Signal',
          value: 'Live',
          unit: 'status',
          secondary: 'Domain-backed',
          progress: 120,
        },
      },
      'Generated App',
    ),
    {
      label: 'Signal',
      value: 'Live',
      unit: 'status',
      status: '',
      secondary: 'Domain-backed',
      progress: 100,
    },
  );

  assert.equal(structuredThumbnailSpec({ thumbnail: { source_code: '<div></div>' } }, 'Inline App'), null);
  assert.equal(inlineThumbnailSource({ thumbnail: { source_code: '<div></div>' } }), '<div></div>');
});
