type JsonishRecord = Record<string, unknown>;

function jsonRoundTrip<T>(value: T): T | null {
  try {
    return JSON.parse(JSON.stringify(value)) as T;
  } catch {
    return null;
  }
}

function safeJsonValue(value: unknown, seen = new WeakSet<object>()): unknown {
  if (value === undefined || typeof value === 'function' || typeof value === 'symbol') return undefined;
  if (typeof value === 'bigint') return String(value);
  if (!value || typeof value !== 'object') return value;
  if (value instanceof Date) return value.toISOString();
  if (seen.has(value)) return undefined;
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => safeJsonValue(item, seen));

  const output: JsonishRecord = {};
  for (const [key, item] of Object.entries(value as JsonishRecord)) {
    const safeItem = safeJsonValue(item, seen);
    if (safeItem !== undefined) output[key] = safeItem;
  }
  return output;
}

export function cloneForPostMessage<T>(value: T): T {
  if (!value || typeof value !== 'object') return value;

  try {
    return structuredClone(value);
  } catch {
    const jsonClone = jsonRoundTrip(value);
    if (jsonClone !== null) return jsonClone;
    return safeJsonValue(value) as T;
  }
}
