import type { VaultSecretPrompt } from '$lib/types/cortex';

type PromptFallback = {
  ideaId?: unknown;
  runId?: unknown;
  createdAt?: unknown;
};

type ToolCallLike = {
  tool?: unknown;
  tool_name?: unknown;
  status?: unknown;
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

const VAULT_SECRET_PROMPT_TOOL = 'vault_secret_prompt';

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

function isVaultSecretPromptTool(value: ToolCallLike | Record<string, any>): boolean {
  return toolName(value) === VAULT_SECRET_PROMPT_TOOL;
}

function fallbackId(ideaId: string, keyName: string, runId?: unknown): string {
  return `vault-secret-${textValue(runId) || ideaId}-${keyName}`;
}

export function normalizeVaultSecretPromptMessage(
  msg: any,
  fallback: PromptFallback = {},
): VaultSecretPrompt | null {
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
    fallback.ideaId,
  );
  if (!keyName || !ideaId) return null;

  return {
    id: firstText(source.id, payload.id) ?? fallbackId(ideaId, keyName, fallback.runId),
    idea_id: ideaId,
    key_name: keyName,
    description: firstText(source.description, payload.description),
    category: firstText(source.category, payload.category) ?? 'api',
    reason: firstText(source.reason, payload.reason),
    requested_by: firstText(source.requested_by, source.requestedBy, payload.requested_by, payload.requestedBy),
    created_at: firstText(source.created_at, source.createdAt, payload.created_at, payload.createdAt, fallback.createdAt),
  };
}

export function vaultSecretPromptFromToolResult(
  result: unknown,
  fallback: PromptFallback = {},
): VaultSecretPrompt | null {
  const parsed = parseJsonObject(result);
  if (!parsed || parsed.error) return null;
  return normalizeVaultSecretPromptMessage(parsed, fallback);
}

export function vaultSecretPromptFromRunToolEvent(msg: unknown): VaultSecretPrompt | null {
  const payload = objectValue(msg);
  if (!payload || !isVaultSecretPromptTool(payload)) return null;
  if (textValue(payload.status) === 'failed') return null;
  return vaultSecretPromptFromToolResult(payload.result, {
    ideaId: payload.idea_id ?? payload.thread_id,
    runId: payload.run_id ?? payload.root_run_id ?? payload.id,
    createdAt: payload.event_created_at,
  });
}

export function vaultSecretPromptFromStream(
  stream: readonly StreamItemLike[],
  ideaId: string | null | undefined,
  ignoredPromptIds: ReadonlySet<string> = new Set(),
): VaultSecretPrompt | null {
  const selectedIdeaId = textValue(ideaId);
  if (!selectedIdeaId) return null;

  let latest: { prompt: VaultSecretPrompt; at: number; order: number } | null = null;
  let order = 0;

  for (const item of stream) {
    if (textValue(item?.type) !== 'run') continue;
    const itemIdeaId = firstText(item.idea_id, item.thread_id);
    if (itemIdeaId && itemIdeaId !== selectedIdeaId) continue;

    for (const call of item.tool_calls || []) {
      order += 1;
      if (!isVaultSecretPromptTool(call)) continue;
      if (textValue(call.status) === 'failed') continue;

      const prompt = vaultSecretPromptFromToolResult(call.result, {
        ideaId: selectedIdeaId,
        runId: item.run_id ?? item.id,
        createdAt: call.finished_at ?? call.at ?? item.completed_at ?? item.started_at ?? item.timestamp,
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
