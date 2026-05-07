export const DOMAIN_BINDING_OPERATIONS = [
  'schema',
  'list',
  'query',
  'get',
  'create',
  'update',
  'archive',
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
  recordId?: number;
  expectedVersion?: number | null;
  title?: string | null;
  search?: string | null;
  limit?: number;
  includeArchived?: boolean;
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

  if (normalizedOperation === 'list' || normalizedOperation === 'query') {
    normalized.search = optionalString(requestPayload.search);
    normalized.limit = optionalNumber(requestPayload.limit, 'limit') ?? undefined;
    normalized.includeArchived = optionalBoolean(
      firstDefined(requestPayload, ['includeArchived', 'include_archived'], warnings, 'includeArchived'),
    );
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

  return normalized;
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
