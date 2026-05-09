type RuntimeConnectionLike = {
  status?: string | null;
  source?: string | null;
};

type RuntimeSettingsLike = {
  connection?: RuntimeConnectionLike | null;
} | null | undefined;

export function hasPersonalOpenAIRuntimeConnection(runtime: RuntimeSettingsLike) {
  const connection = runtime?.connection;
  return connection?.status === 'connected' && connection?.source === 'user_default';
}

export function requiresPersonalOpenAIOnboarding(runtime: RuntimeSettingsLike) {
  return !hasPersonalOpenAIRuntimeConnection(runtime);
}
