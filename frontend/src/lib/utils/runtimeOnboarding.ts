type RuntimeConnectionLike = {
  status?: string | null;
  source?: string | null;
};

type RuntimeSettingsLike = {
  connection?: RuntimeConnectionLike | null;
} | null | undefined;

export function hasPersonalOpenAIRuntimeConnection(runtime: RuntimeSettingsLike) {
  const connection = runtime?.connection;
  return connection?.status === 'connected' && connection?.source === 'codex_subscription';
}

export function requiresPersonalOpenAIOnboarding(runtime: RuntimeSettingsLike) {
  return !hasPersonalOpenAIRuntimeConnection(runtime);
}
