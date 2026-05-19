import type { VaultAgentGrantPrompt } from '$lib/types/cortex';

type PromptFallback = {
  ideaId?: unknown;
  runId?: unknown;
  createdAt?: unknown;
};

function objectValue(value: any): Record<string, any> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

function textValue(value: any): string | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  return text || null;
}

function firstText(...values: any[]): string | null {
  for (const value of values) {
    const text = textValue(value);
    if (text) return text;
  }
  return null;
}

function numberValue(value: any): number | null {
  const numeric = Number(value);
  return Number.isSafeInteger(numeric) && numeric > 0 ? numeric : null;
}

function firstNumber(...values: any[]): number | null {
  for (const value of values) {
    const numeric = numberValue(value);
    if (numeric !== null) return numeric;
  }
  return null;
}

function parseJsonObject(value: unknown): Record<string, any> | null {
  const object = objectValue(value);
  if (object) return object;
  const source = textValue(value);
  if (!source) return null;
  try {
    const parsed = JSON.parse(source);
    return objectValue(parsed);
  } catch {
    return null;
  }
}

function fallbackId(ideaId: string, grantId: number): string {
  return `vault-grant-${ideaId}-${grantId}`;
}

export function normalizeVaultAgentGrantPromptMessage(
  msg: any,
  fallback: PromptFallback = {},
): VaultAgentGrantPrompt | null {
  const payload = objectValue(msg?.payload) ?? objectValue(msg);
  if (!payload) return null;
  const grant = objectValue(payload.grant) ?? {};
  const source = objectValue(payload.prompt) ?? payload;
  const grantId = firstNumber(source.grant_id, source.grantId, payload.grant_id, payload.grantId, grant.id);
  const keyName = firstText(source.key_name, source.keyName, payload.key_name, payload.keyName, grant.key_name);
  const ideaId = firstText(
    payload.idea_id,
    payload.ideaId,
    payload.thread_id,
    payload.threadId,
    source.idea_id,
    source.ideaId,
    source.thread_id,
    source.threadId,
    fallback.ideaId,
  );
  if (!grantId || !keyName || !ideaId) return null;

  return {
    id: firstText(source.id, payload.id) ?? fallbackId(ideaId, grantId),
    idea_id: ideaId,
    grant_id: grantId,
    key_name: keyName,
    run_id: source.run_id ?? source.runId ?? payload.run_id ?? payload.runId ?? grant.run_id ?? fallback.runId ?? null,
    reason: firstText(source.reason, payload.reason, grant.reason),
    requested_by: firstText(
      source.requested_by,
      source.requestedBy,
      payload.requested_by,
      payload.requestedBy,
      grant.requested_by,
    ),
    requested_at: firstText(source.requested_at, source.requestedAt, payload.requested_at, payload.requestedAt, grant.requested_at),
    created_at: firstText(source.created_at, source.createdAt, payload.created_at, payload.createdAt, fallback.createdAt),
  };
}

export function vaultAgentGrantPromptFromToolResult(
  result: unknown,
  fallback: PromptFallback & { keyName?: unknown } = {},
): VaultAgentGrantPrompt | null {
  const parsed = parseJsonObject(result);
  if (!parsed || parsed.error !== 'Vault grant required before this agent can read the secret') return null;
  return normalizeVaultAgentGrantPromptMessage(
    {
      ...parsed,
      key_name: parsed.key_name ?? fallback.keyName,
    },
    fallback,
  );
}

