import test from 'node:test';
import assert from 'node:assert/strict';

import { normalizeDomainRequest, withDomainRecordAliases } from './generatedAppBridge.ts';

const manifest = {
  data_plan: {
    mode: 'domain',
    bindings: {
      todos: {
        domain_id: 1,
        domain_slug: 'todo-notes',
        object_key: 'todo_item',
        fields: ['title', 'notes', 'completed'],
        operations: ['schema', 'list', 'create', 'update', 'archive'],
      },
    },
  },
};

test('normalizes compatibility create values into Domain data', () => {
  const request = normalizeDomainRequest(manifest, 'create', {
    alias: 'todos',
    values: { title: 'Ship the bridge', notes: 'Keep generated code boring' },
  });

  assert.equal(request.domainId, 1);
  assert.equal(request.objectKey, 'todo_item');
  assert.deepEqual(request.data, {
    title: 'Ship the bridge',
    notes: 'Keep generated code boring',
  });
  assert.equal(request.title, 'Ship the bridge');
  assert.ok(request.warnings.some((warning) => warning.includes("'values' as 'data'")));
});

test('normalizes update aliases for record id, patch, and expected version', () => {
  const request = normalizeDomainRequest(manifest, 'update', {
    alias: 'todos',
    id: '42',
    fields: { completed: true },
    expected_version: '7',
  });

  assert.equal(request.recordId, 42);
  assert.deepEqual(request.dataPatch, { completed: true });
  assert.equal(request.expectedVersion, 7);
  assert.ok(request.warnings.some((warning) => warning.includes("'id' as 'recordId'")));
  assert.ok(request.warnings.some((warning) => warning.includes("'fields' as 'dataPatch'")));
  assert.ok(request.warnings.some((warning) => warning.includes("'expected_version' as 'expectedVersion'")));
});

test('resolves the only manifest binding for raw compatibility calls', () => {
  const request = normalizeDomainRequest(manifest, 'create', {
    values: { title: 'Notes-only generated code' },
  });

  assert.equal(request.alias, 'todos');
  assert.equal(request.domainId, 1);
  assert.equal(request.objectKey, 'todo_item');
  assert.deepEqual(request.data, { title: 'Notes-only generated code' });
  assert.ok(request.warnings.some((warning) => warning.includes("only manifest binding 'todos'")));
});

test('resolves direct domain id and object key without a manifest alias', () => {
  const request = normalizeDomainRequest({}, 'list', {
    domain_id: '5',
    object_key: 'task',
    include_archived: 'true',
    limit: '25',
  });

  assert.equal(request.domainId, 5);
  assert.equal(request.objectKey, 'task');
  assert.equal(request.includeArchived, true);
  assert.equal(request.limit, 25);
});

test('normalizes generic workspace app Domain primitives', () => {
  const extended = structuredClone(manifest);
  extended.data_plan.bindings.todos.operations = [
    'schema',
    'list',
    'query',
    'get',
    'create',
    'update',
    'archive',
    'aggregate',
    'bulkUpdate',
    'history',
    'listRelations',
    'createRelation',
    'archiveRelation',
  ];

  const aggregate = normalizeDomainRequest(extended, 'aggregate', {
    alias: 'todos',
    group_by: 'completed',
    metrics: [{ type: 'count', as: 'todos' }],
  });
  assert.equal(aggregate.groupBy, 'completed');
  assert.deepEqual(aggregate.metrics, [{ type: 'count', field: null, as: 'todos' }]);

  const bulk = normalizeDomainRequest(extended, 'bulkUpdate', {
    alias: 'todos',
    record_ids: ['1', 2],
    data_patch: { completed: true },
  });
  assert.deepEqual(bulk.updates, [
    { recordId: 1, dataPatch: { completed: true }, title: null, expectedVersion: null },
    { recordId: 2, dataPatch: { completed: true }, title: null, expectedVersion: null },
  ]);

  const relation = normalizeDomainRequest(extended, 'createRelation', {
    alias: 'todos',
    relation_key: 'todo_blocks_todo',
    source_record_id: '1',
    target_record_id: '2',
    properties: { reason: 'dependency' },
  });
  assert.equal(relation.relationKey, 'todo_blocks_todo');
  assert.equal(relation.sourceRecordId, 1);
  assert.equal(relation.targetRecordId, 2);
  assert.deepEqual(relation.properties, { reason: 'dependency' });
});

test('rejects operations not declared by the manifest binding', () => {
  assert.throws(
    () => normalizeDomainRequest(manifest, 'aggregate', { alias: 'todos' }),
    /does not allow operation 'aggregate'/,
  );
});

test('returns Domain records with both data and values aliases', () => {
  const records = withDomainRecordAliases([
    { id: 1, domain_id: 1, data: { title: 'One' } },
    { id: 2, domain_id: 1, values: { title: 'Two' } },
  ]);

  assert.deepEqual(records[0].values, { title: 'One' });
  assert.deepEqual(records[1].data, { title: 'Two' });
  assert.equal(records[0].recordId, 1);
  assert.equal(records[1].recordId, 2);
});
