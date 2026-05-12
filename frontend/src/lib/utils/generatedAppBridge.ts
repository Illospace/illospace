export const DOMAIN_BINDING_OPERATIONS = [
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
] as const;

export type DomainBindingOperation = (typeof DOMAIN_BINDING_OPERATIONS)[number];

type LooseRecord = Record<string, any>;

export type NormalizedDomainBinding = {
  alias: string | null;
  domainId: number;
  objectKey: string | null;
  domainSlug: string | null;
  binding: LooseRecord | null;
};

export type NormalizedDomainRequest = NormalizedDomainBinding & {
  operation: DomainBindingOperation;
  warnings: string[];
  data?: LooseRecord;
  dataPatch?: LooseRecord;
  updates?: Array<{
    recordId: number;
    dataPatch: LooseRecord;
    title?: string | null;
    expectedVersion?: number | null;
  }>;
  recordId?: number;
  relationId?: number;
  relationKey?: string | null;
  sourceRecordId?: number | null;
  targetRecordId?: number | null;
  properties?: LooseRecord;
  expectedVersion?: number | null;
  title?: string | null;
  search?: string | null;
  limit?: number;
  includeArchived?: boolean;
  groupBy?: string | null;
  metrics?: Array<{
    type: string;
    field?: string | null;
    as?: string | null;
  }>;
};

function isRecord(value: unknown): value is LooseRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function warnAlias(warnings: string[], alias: string, canonical: string) {
  warnings.push(
    `Illo Domain bridge accepted legacy '${alias}' as '${canonical}'. Prefer '${canonical}'.`,
  );
}

function firstDefined(payload: LooseRecord, keys: string[], warnings: string[], canonical: string) {
  for (const key of keys) {
    if (payload[key] !== undefined) {
      if (key !== canonical) warnAlias(warnings, key, canonical);
      return payload[key];
    }
  }
  return undefined;
}

function firstObject(
  payload: LooseRecord,
  keys: string[],
  warnings: string[],
  canonical: string,
): LooseRecord {
  const value = firstDefined(payload, keys, warnings, canonical);
  return isRecord(value) ? value : {};
}

function optionalString(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  const text = String(value).trim();
  return text || null;
}

function coerceNumber(value: unknown, fieldName: string): number {
  const numberValue = Number(value);
  if (!Number.isFinite(numberValue) || !Number.isInteger(numberValue)) {
    throw new Error(`Domain request requires numeric ${fieldName}`);
  }
  return numberValue;
}

function optionalNumber(value: unknown, fieldName: string): number | null {
  if (value === undefined || value === null || value === '') return null;
  return coerceNumber(value, fieldName);
}

function optionalBoolean(value: unknown): boolean | undefined {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (normalized === 'true') return true;
    if (normalized === 'false') return false;
  }
  return Boolean(value);
}

export function getDomainBindings(manifest: unknown): Record<string, LooseRecord> {
  if (!isRecord(manifest)) return {};
  const dataPlan = manifest.data_plan;
  if (!isRecord(dataPlan)) return {};
  const bindings = dataPlan.bindings;
  if (!isRecord(bindings)) return {};
  return Object.fromEntries(
    Object.entries(bindings).filter(([, binding]) => isRecord(binding)),
  ) as Record<string, LooseRecord>;
}

export function normalizeDomainBinding(
  manifest: unknown,
  payload: unknown = {},
  explicitAlias: string | null = null,
  warnings: string[] = [],
  requireObjectKey = true,
): NormalizedDomainBinding {
  const requestPayload = isRecord(payload) ? payload : {};
  const bindings = getDomainBindings(manifest);
  let alias =
    optionalString(explicitAlias) ??
    optionalString(firstDefined(requestPayload, ['alias', 'binding', 'binding_alias', 'domain_alias'], warnings, 'alias'));
  const requestedDomainId = firstDefined(requestPayload, ['domainId', 'domain_id'], warnings, 'domainId');

  if (!alias && requestedDomainId === undefined) {
    const bindingEntries = Object.entries(bindings);
    if (bindingEntries.length === 1) {
      alias = bindingEntries[0][0];
      warnings.push(
        `Illo Domain bridge used the only manifest binding '${alias}'. Prefer window.illo.domain('${alias}').`,
      );
    }
  }

  const binding = alias ? bindings[alias] ?? null : null;

  if (alias && !binding) {
    throw new Error(`Domain binding '${alias}' was not found in this app manifest`);
  }

  const domainIdValue = requestedDomainId ?? binding?.domain_id ?? binding?.domainId;
  const domainId = coerceNumber(domainIdValue, 'domainId');
  const objectKey =
    optionalString(firstDefined(requestPayload, ['objectKey', 'object_key'], warnings, 'objectKey')) ??
    optionalString(binding?.object_key ?? binding?.objectKey);

  if (requireObjectKey && !objectKey) {
    throw new Error(
      alias
        ? `Domain binding '${alias}' requires object_key`
        : 'Domain request requires objectKey or a manifest binding with object_key',
    );
  }

  return {
    alias,
    domainId,
    objectKey,
    domainSlug:
      optionalString(firstDefined(requestPayload, ['domainSlug', 'domain_slug'], warnings, 'domainSlug')) ??
      optionalString(binding?.domain_slug ?? binding?.domainSlug),
    binding,
  };
}

export function normalizeDomainRequest(
  manifest: unknown,
  operation: string,
  payload: unknown = {},
  explicitAlias: string | null = null,
): NormalizedDomainRequest {
  if (!DOMAIN_BINDING_OPERATIONS.includes(operation as DomainBindingOperation)) {
    throw new Error(`Unsupported Domain operation '${operation}'`);
  }

  const normalizedOperation = operation as DomainBindingOperation;
  const requestPayload = isRecord(payload) ? payload : {};
  const warnings: string[] = [];
  const requireObjectKey = normalizedOperation === 'list' || normalizedOperation === 'query' || normalizedOperation === 'create';
  const binding = normalizeDomainBinding(
    manifest,
    requestPayload,
    explicitAlias,
    warnings,
    requireObjectKey,
  );

  const normalized: NormalizedDomainRequest = {
    ...binding,
    operation: normalizedOperation,
    warnings,
  };

  const declaredOperations = Array.isArray(binding.binding?.operations)
    ? binding.binding.operations.map((item) => String(item).trim()).filter(Boolean)
    : [];
  if (declaredOperations.length && !declaredOperations.includes(normalizedOperation)) {
    throw new Error(
      `Domain binding '${binding.alias ?? binding.domainId}' does not allow operation '${normalizedOperation}'`,
    );
  }

  if (normalizedOperation === 'list' || normalizedOperation === 'query') {
    normalized.search = optionalString(requestPayload.search);
    normalized.limit = optionalNumber(requestPayload.limit, 'limit') ?? undefined;
    normalized.includeArchived = optionalBoolean(
      firstDefined(requestPayload, ['includeArchived', 'include_archived'], warnings, 'includeArchived'),
    );
  }

  if (normalizedOperation === 'aggregate') {
    normalized.search = optionalString(requestPayload.search);
    normalized.limit = optionalNumber(requestPayload.limit, 'limit') ?? undefined;
    normalized.includeArchived = optionalBoolean(
      firstDefined(requestPayload, ['includeArchived', 'include_archived'], warnings, 'includeArchived'),
    );
    normalized.groupBy =
      optionalString(firstDefined(requestPayload, ['groupBy', 'group_by'], warnings, 'groupBy')) ??
      null;
    normalized.metrics = normalizeMetrics(requestPayload.metrics);
  }

  if (normalizedOperation === 'get' || normalizedOperation === 'update' || normalizedOperation === 'archive') {
    const recordIdValue = firstDefined(requestPayload, ['recordId', 'record_id', 'id'], warnings, 'recordId');
    normalized.recordId = coerceNumber(recordIdValue, 'recordId');
  }

  if (normalizedOperation === 'create') {
    normalized.data = firstObject(requestPayload, ['data', 'values', 'fields'], warnings, 'data');
    normalized.title = optionalString(requestPayload.title) ?? optionalString(normalized.data.title);
  }

  if (normalizedOperation === 'update') {
    normalized.dataPatch = firstObject(
      requestPayload,
      ['dataPatch', 'data_patch', 'values', 'fields', 'data'],
      warnings,
      'dataPatch',
    );
    normalized.title = optionalString(requestPayload.title) ?? optionalString(normalized.dataPatch.title);
    normalized.expectedVersion = optionalNumber(
      firstDefined(requestPayload, ['expectedVersion', 'expected_version'], warnings, 'expectedVersion'),
      'expectedVersion',
    );
  }

  if (normalizedOperation === 'bulkUpdate') {
    const updatesValue = firstDefined(requestPayload, ['updates', 'records'], warnings, 'updates');
    const idsValue = firstDefined(requestPayload, ['recordIds', 'record_ids', 'ids'], warnings, 'recordIds');
    const sharedPatch = firstObject(
      requestPayload,
      ['dataPatch', 'data_patch', 'values', 'fields', 'data'],
      warnings,
      'dataPatch',
    );
    const sharedTitle = optionalString(requestPayload.title);
    const sharedExpectedVersion = optionalNumber(
      firstDefined(requestPayload, ['expectedVersion', 'expected_version'], warnings, 'expectedVersion'),
      'expectedVersion',
    );
    normalized.updates = normalizeBulkUpdates(
      updatesValue,
      idsValue,
      sharedPatch,
      sharedTitle,
      sharedExpectedVersion,
      warnings,
    );
  }

  if (normalizedOperation === 'history') {
    const recordIdValue = firstDefined(requestPayload, ['recordId', 'record_id', 'id'], warnings, 'recordId');
    normalized.recordId = optionalNumber(recordIdValue, 'recordId') ?? undefined;
    normalized.limit = optionalNumber(requestPayload.limit, 'limit') ?? undefined;
  }

  if (normalizedOperation === 'listRelations') {
    normalized.relationKey =
      optionalString(firstDefined(requestPayload, ['relationKey', 'relation_key'], warnings, 'relationKey')) ??
      null;
    normalized.sourceRecordId = optionalNumber(
      firstDefined(requestPayload, ['sourceRecordId', 'source_record_id'], warnings, 'sourceRecordId'),
      'sourceRecordId',
    );
    normalized.targetRecordId = optionalNumber(
      firstDefined(requestPayload, ['targetRecordId', 'target_record_id'], warnings, 'targetRecordId'),
      'targetRecordId',
    );
    normalized.includeArchived = optionalBoolean(
      firstDefined(requestPayload, ['includeArchived', 'include_archived'], warnings, 'includeArchived'),
    );
    normalized.limit = optionalNumber(requestPayload.limit, 'limit') ?? undefined;
  }

  if (normalizedOperation === 'createRelation') {
    normalized.relationKey =
      optionalString(firstDefined(requestPayload, ['relationKey', 'relation_key'], warnings, 'relationKey')) ??
      null;
    if (!normalized.relationKey) throw new Error('Domain relation request requires relationKey');
    normalized.sourceRecordId = coerceNumber(
      firstDefined(requestPayload, ['sourceRecordId', 'source_record_id'], warnings, 'sourceRecordId'),
      'sourceRecordId',
    );
    normalized.targetRecordId = coerceNumber(
      firstDefined(requestPayload, ['targetRecordId', 'target_record_id'], warnings, 'targetRecordId'),
      'targetRecordId',
    );
    normalized.properties = firstObject(requestPayload, ['properties', 'data'], warnings, 'properties');
  }

  if (normalizedOperation === 'archiveRelation') {
    normalized.relationId = coerceNumber(
      firstDefined(requestPayload, ['relationId', 'relation_id', 'id'], warnings, 'relationId'),
      'relationId',
    );
  }

  return normalized;
}

function normalizeMetrics(value: unknown): Array<{ type: string; field?: string | null; as?: string | null }> {
  if (!Array.isArray(value) || !value.length) {
    return [{ type: 'count', as: 'count' }];
  }
  return value
    .filter(isRecord)
    .map((metric) => ({
      type: String(metric.type || metric.op || 'count').trim() || 'count',
      field: optionalString(metric.field ?? metric.key),
      as: optionalString(metric.as ?? metric.label),
    }));
}

function normalizeBulkUpdates(
  updatesValue: unknown,
  idsValue: unknown,
  sharedPatch: LooseRecord,
  sharedTitle: string | null,
  sharedExpectedVersion: number | null,
  warnings: string[],
): Array<{ recordId: number; dataPatch: LooseRecord; title?: string | null; expectedVersion?: number | null }> {
  if (Array.isArray(updatesValue) && updatesValue.length) {
    return updatesValue.map((item, index) => {
      if (!isRecord(item)) throw new Error(`bulkUpdate updates[${index}] must be an object`);
      const recordId = coerceNumber(
        firstDefined(item, ['recordId', 'record_id', 'id'], warnings, 'recordId'),
        `updates[${index}].recordId`,
      );
      const dataPatch = firstObject(
        item,
        ['dataPatch', 'data_patch', 'values', 'fields', 'data'],
        warnings,
        'dataPatch',
      );
      const title = optionalString(item.title);
      const expectedVersion = optionalNumber(
        firstDefined(item, ['expectedVersion', 'expected_version'], warnings, 'expectedVersion'),
        `updates[${index}].expectedVersion`,
      );
      return { recordId, dataPatch, title, expectedVersion };
    });
  }

  if (Array.isArray(idsValue) && idsValue.length) {
    return idsValue.map((recordIdValue, index) => ({
      recordId: coerceNumber(recordIdValue, `recordIds[${index}]`),
      dataPatch: sharedPatch,
      title: sharedTitle,
      expectedVersion: sharedExpectedVersion,
    }));
  }

  throw new Error('bulkUpdate requires updates or recordIds');
}

export function withDomainRecordAliases(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => withDomainRecordAliases(item));
  if (!isRecord(value)) return value;

  if (!('data' in value) && !('values' in value) && !('fields' in value) && !('domain_id' in value)) {
    return value;
  }

  const data = isRecord(value.data)
    ? value.data
    : isRecord(value.values)
      ? value.values
      : isRecord(value.fields)
        ? value.fields
        : {};
  return {
    ...value,
    data,
    values: data,
    recordId: value.recordId ?? value.record_id ?? value.id,
  };
}
