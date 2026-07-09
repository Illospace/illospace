import type {
  AgentRunOptions,
  CortexEffortLevel,
  CortexExecutionProfile,
} from '$lib/types/cortex';

export type CortexRunSettings = Required<
  Pick<AgentRunOptions, 'executionProfile' | 'model' | 'effortLevel'>
>;

export type CortexRunSettingsInput = {
  executionProfile?: unknown;
  model?: unknown;
  effortLevel?: unknown;
};

export type RunSettingsStorage = Pick<Storage, 'getItem' | 'setItem'>;

export const DEFAULT_RUN_MODEL = 'openai/gpt-5.6-sol';

export const CORTEX_RUN_SETTINGS_STORAGE_KEYS = {
  executionProfile: 'illo:cortex:execution-profile',
  model: 'illo:cortex:model',
  effortLevel: 'illo:cortex:effort-level',
} as const;

export const DEFAULT_CORTEX_RUN_SETTINGS: CortexRunSettings = {
  executionProfile: 'fast',
  model: DEFAULT_RUN_MODEL,
  effortLevel: 'xhigh',
};

export function normalizeExecutionProfile(value: unknown): CortexExecutionProfile {
  return String(value || '').trim().toLowerCase() === 'deep' ? 'deep' : 'fast';
}

export function normalizeModel(value: unknown, fallback = DEFAULT_RUN_MODEL): string {
  const normalized = String(value || '').trim().replace(':', '/');
  return normalized || fallback;
}

export function normalizeEffortLevel(value: unknown): CortexEffortLevel {
  const normalized = String(value || '').trim().toLowerCase();
  return normalized === 'none' || normalized === 'low' || normalized === 'medium' || normalized === 'high' || normalized === 'xhigh'
    ? normalized
    : 'high';
}

export function normalizeRunSettings(
  settings: CortexRunSettingsInput | null | undefined,
  fallback: CortexRunSettings = DEFAULT_CORTEX_RUN_SETTINGS,
): CortexRunSettings {
  return {
    executionProfile: normalizeExecutionProfile(settings?.executionProfile ?? fallback.executionProfile),
    model: normalizeModel(settings?.model ?? fallback.model, fallback.model),
    effortLevel: normalizeEffortLevel(settings?.effortLevel ?? fallback.effortLevel),
  };
}

export function runSettingsOptions(
  settings: CortexRunSettingsInput,
): Pick<AgentRunOptions, 'executionProfile' | 'model' | 'effortLevel'> {
  return normalizeRunSettings(settings);
}

export function normalizeRunOptions(
  options: AgentRunOptions = {},
  currentSettings: CortexRunSettingsInput = DEFAULT_CORTEX_RUN_SETTINGS,
): AgentRunOptions {
  const settings = normalizeRunSettings({
    executionProfile: options.executionProfile ?? currentSettings.executionProfile,
    model: options.model ?? currentSettings.model,
    effortLevel: options.effortLevel ?? currentSettings.effortLevel,
  });
  return {
    ...options,
    ...settings,
    metadata: { ...(options.metadata || {}) },
  };
}

export function loadRunSettings(
  storage: RunSettingsStorage | null | undefined,
  fallback: CortexRunSettings = DEFAULT_CORTEX_RUN_SETTINGS,
): CortexRunSettings {
  if (!storage) return { ...fallback };
  try {
    return normalizeRunSettings({
      executionProfile: storage.getItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.executionProfile),
      model: storage.getItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.model),
      effortLevel: storage.getItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.effortLevel),
    }, fallback);
  } catch {
    return { ...fallback };
  }
}

export function persistRunSettings(
  storage: RunSettingsStorage | null | undefined,
  settings: Partial<CortexRunSettings>,
): boolean {
  if (!storage) return false;
  try {
    if (settings.executionProfile !== undefined) {
      storage.setItem(
        CORTEX_RUN_SETTINGS_STORAGE_KEYS.executionProfile,
        normalizeExecutionProfile(settings.executionProfile),
      );
    }
    if (settings.model !== undefined) {
      storage.setItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.model, normalizeModel(settings.model));
    }
    if (settings.effortLevel !== undefined) {
      storage.setItem(
        CORTEX_RUN_SETTINGS_STORAGE_KEYS.effortLevel,
        normalizeEffortLevel(settings.effortLevel),
      );
    }
    return true;
  } catch {
    return false;
  }
}
