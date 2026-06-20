type RuntimeConnectionLike = {
  status?: string | null;
  source?: string | null;
};

type RuntimeSettingsLike = {
  connection?: RuntimeConnectionLike | null;
} | null | undefined;

export function hasPersonalOpenAIRuntimeConnection(runtime: RuntimeSettingsLike) {
  const connection = runtime?.connection;
  return connection?.status === 'connected' && ['codex_subscription', 'user_openai'].includes(connection?.source || '');
}

export function requiresPersonalOpenAIOnboarding(runtime: RuntimeSettingsLike) {
  return !hasPersonalOpenAIRuntimeConnection(runtime);
}
