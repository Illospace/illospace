import type { VaultSecretPrompt } from '$lib/types/cortex';

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

export function normalizeVaultSecretPromptMessage(msg: any): VaultSecretPrompt | null {
  const payload = objectValue(msg?.payload) ?? objectValue(msg);
  if (!payload) return null;
  const source = objectValue(payload.prompt) ?? payload;
  const keyName = firstText(source.key_name, source.keyName, payload.key_name, payload.keyName);
  const ideaId = firstText(
    payload.idea_id,
    payload.ideaId,
    payload.thread_id,
    payload.threadId,
    payload.target_idea_id,
    payload.targetIdeaId,
    source.idea_id,
    source.ideaId,
    source.thread_id,
    source.threadId,
    source.target_idea_id,
    source.targetIdeaId,
  );
  if (!keyName || !ideaId) return null;

  return {
    id: firstText(source.id, payload.id) ?? `vault-secret-${ideaId}-${keyName}`,
    idea_id: ideaId,
    key_name: keyName,
    description: firstText(source.description, payload.description),
    category: firstText(source.category, payload.category) ?? 'api',
    reason: firstText(source.reason, payload.reason),
    requested_by: firstText(source.requested_by, source.requestedBy, payload.requested_by, payload.requestedBy),
    created_at: firstText(source.created_at, source.createdAt, payload.created_at, payload.createdAt),
  };
}
