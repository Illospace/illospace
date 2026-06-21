export const WORKSPACE_APP_STATE_KEY_PARAM = 'state_key';

const MAX_STATE_KEY_LENGTH = 120;

export function normalizeWorkspaceAppStateKey(value: unknown, fallback = 'default'): string {
  const fallbackText = String(fallback || 'default').trim() || 'default';
  const text = String(value || '').trim();
  if (!text) return fallbackText;
  if (text.length > MAX_STATE_KEY_LENGTH) return fallbackText;
  return text;
}

export function resolveWorkspaceAppStateKey(
  searchParams: URLSearchParams | null | undefined,
  manifestStateKey: unknown,
): string {
  return normalizeWorkspaceAppStateKey(
    searchParams?.get(WORKSPACE_APP_STATE_KEY_PARAM),
    normalizeWorkspaceAppStateKey(manifestStateKey),
  );
}
