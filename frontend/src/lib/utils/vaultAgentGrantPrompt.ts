import type { VaultAgentGrantPrompt } from '$lib/types/cortex';

type PromptFallback = {
  ideaId?: unknown;
  runId?: unknown;
  createdAt?: unknown;
  currentUserId?: unknown;
};

type ToolCallLike = {
  tool?: unknown;
  tool_name?: unknown;
  result?: unknown;
  at?: unknown;
  finished_at?: unknown;
};

type StreamItemLike = {
  type?: unknown;
  id?: unknown;
  run_id?: unknown;
  idea_id?: unknown;
  thread_id?: unknown;
  timestamp?: unknown;
  started_at?: unknown;
  completed_at?: unknown;
  tool_calls?: ToolCallLike[];
};

const BRAIN_VAULT_TOOL = 'brain_vault';

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

function toolName(value: ToolCallLike | Record<string, any>): string | null {
  return firstText(value.tool_name, value.tool);
}

function isBrainVaultTool(value: ToolCallLike | Record<string, any>): boolean {
  return toolName(value) === BRAIN_VAULT_TOOL;
}

function fallbackId(ideaId: string, grantId: number): string {
  return `vault-grant-${ideaId}-${grantId}`;
}

function targetUserMatches(payload: Record<string, any>, fallback: PromptFallback): boolean {
  const currentUserId = textValue(fallback.currentUserId);
  if (!currentUserId) return true;
  const prompt = objectValue(payload.prompt) ?? {};
  const grant = objectValue(payload.grant) ?? {};
  const targetUserId = firstText(
    payload.target_user_id,
    payload.targetUserId,
    prompt.target_user_id,
    prompt.targetUserId,
    grant.user_id,
    grant.userId,
  );
  return targetUserId === currentUserId;
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
  if (!targetUserMatches(parsed, fallback)) return null;
  return normalizeVaultAgentGrantPromptMessage(
    {
      ...parsed,
      key_name: parsed.key_name ?? fallback.keyName,
    },
    fallback,
  );
}

export function vaultAgentGrantPromptFromRunToolEvent(
  msg: unknown,
  currentUserId?: unknown,
): VaultAgentGrantPrompt | null {
  const payload = objectValue(msg);
  if (!payload || !isBrainVaultTool(payload)) return null;
  return vaultAgentGrantPromptFromToolResult(payload.result, {
    ideaId: payload.idea_id ?? payload.thread_id,
    runId: payload.run_id ?? payload.root_run_id ?? payload.id,
    createdAt: payload.event_created_at,
    currentUserId,
  });
}

export function vaultAgentGrantPromptFromStream(
  stream: readonly StreamItemLike[],
  ideaId: string | null | undefined,
  ignoredPromptIds: ReadonlySet<string> = new Set(),
  currentUserId?: unknown,
): VaultAgentGrantPrompt | null {
  const selectedIdeaId = textValue(ideaId);
  if (!selectedIdeaId) return null;

  let latest: { prompt: VaultAgentGrantPrompt; at: number; order: number } | null = null;
  let order = 0;

  for (const item of stream) {
    if (textValue(item?.type) !== 'run') continue;
    const itemIdeaId = firstText(item.idea_id, item.thread_id);
    if (itemIdeaId && itemIdeaId !== selectedIdeaId) continue;

    for (const call of item.tool_calls || []) {
      order += 1;
      if (!isBrainVaultTool(call)) continue;

      const prompt = vaultAgentGrantPromptFromToolResult(call.result, {
        ideaId: selectedIdeaId,
        runId: item.run_id ?? item.id,
        createdAt: call.finished_at ?? call.at ?? item.completed_at ?? item.started_at ?? item.timestamp,
        currentUserId,
      });
      if (!prompt || ignoredPromptIds.has(prompt.id)) continue;

      const at = Date.parse(prompt.created_at || '') || Date.parse(textValue(call.finished_at) || '') || order;
      if (!latest || at > latest.at || (at === latest.at && order > latest.order)) {
        latest = { prompt, at, order };
      }
    }
  }

  return latest?.prompt ?? null;
}
