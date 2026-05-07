export type DomainOperation = 'schema' | 'list' | 'query' | 'get' | 'create' | 'update' | 'archive';

export type DomainBinding = {
  domain_id?: number;
  domainId?: number;
  object_key?: string;
  objectKey?: string;
  fields?: string[];
  operations?: string[];
};

export type ResolvedDomainBinding = {
  domainId: number;
  objectKey: string;
};

export type ThumbnailSpec = {
  label: string;
  value: string;
  unit: string;
  status: string;
  secondary: string;
  progress: number;
};

export function numberFrom(value: unknown, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function domainBindings(manifest: Record<string, any> | null | undefined) {
  const bindings = manifest?.data_plan?.bindings;
  return bindings && typeof bindings === 'object' ? (bindings as Record<string, DomainBinding>) : {};
}

export function bindingAllowsOperation(
  binding: DomainBinding | null | undefined,
  operation: DomainOperation,
): boolean {
  const operations = Array.isArray(binding?.operations) ? binding.operations.map(String) : [];
  return operations.includes(operation);
}

export function bindingAllowsField(
  binding: DomainBinding | null | undefined,
  fieldKey: string | null | undefined,
): boolean {
  const key = String(fieldKey || '').trim();
  if (!key) return false;
  const fields = Array.isArray(binding?.fields) ? binding.fields.map(String) : [];
  return fields.length === 0 || fields.includes(key);
}

export function resolveDomainBinding(
  manifest: Record<string, any> | null | undefined,
  payload: Record<string, any> | undefined,
  operation: DomainOperation,
): ResolvedDomainBinding {
  const alias = String(payload?.alias || '').trim();
  if (!alias) throw new Error('Domain alias is required');
  const binding = domainBindings(manifest)[alias];
  if (!binding) throw new Error(`Domain alias '${alias}' is not bound for this app`);
  if (!bindingAllowsOperation(binding, operation)) {
    throw new Error(`Domain operation '${operation}' is not allowed for alias '${alias}'`);
  }
  const domainId = Number(binding.domain_id ?? binding.domainId);
  if (!Number.isFinite(domainId) || domainId <= 0) throw new Error(`Domain alias '${alias}' has no domain_id`);
  const objectKey = String(binding.object_key ?? binding.objectKey ?? '').trim();
  if (!objectKey && operation !== 'schema') throw new Error(`Domain alias '${alias}' has no object_key`);
  return { domainId, objectKey };
}

export function structuredThumbnailSpec(
  visualSpec: Record<string, any> | null | undefined,
  appName: string,
  previewSpec: Record<string, any> = {},
): ThumbnailSpec | null {
  const source = visualSpec?.thumbnail_manifest ?? visualSpec?.thumbnail;
  if (!source || typeof source !== 'object') return null;
  const thumbnail = source as Record<string, any>;
  if (thumbnail.source_code || thumbnail.html) return null;
  const label = String(thumbnail.label || thumbnail.title || appName || 'App').trim();
  const value = thumbnail.value === undefined || thumbnail.value === null ? '' : String(thumbnail.value);
  const status = thumbnail.status === undefined || thumbnail.status === null ? '' : String(thumbnail.status);
  const progress = numberFrom(thumbnail.progress, numberFrom(thumbnail.percent, previewSpec.progress ?? 0));
  if (!label || (!value && !status)) return null;
  return {
    label,
    value,
    unit: String(thumbnail.unit || thumbnail.value_label || ''),
    status,
    secondary: String(thumbnail.secondary || previewSpec.secondary || ''),
    progress: Math.max(0, Math.min(100, progress)),
  };
}

export function inlineThumbnailSource(visualSpec: Record<string, any> | null | undefined) {
  const thumbnail = visualSpec?.thumbnail;
  if (typeof thumbnail === 'string') return thumbnail;
  if (thumbnail && typeof thumbnail === 'object') {
    return String(thumbnail.source_code || thumbnail.html || '');
  }
  return String(visualSpec?.thumbnail_source || visualSpec?.thumbnail_html || '');
}
