import type {
  AgentRunOptions,
  CortexEffortLevel,
  CortexExecutionProfile,
  CortexIntelligenceTier,
} from '$lib/types/cortex';

export type CortexRunSettings = Required<
  Pick<AgentRunOptions, 'executionProfile' | 'intelligenceTier' | 'effortLevel'>
>;

export type CortexRunSettingsInput = {
  executionProfile?: unknown;
  intelligenceTier?: unknown;
  effortLevel?: unknown;
};

export type RunSettingsStorage = Pick<Storage, 'getItem' | 'setItem'>;

export const CORTEX_RUN_SETTINGS_STORAGE_KEYS = {
  executionProfile: 'illo:cortex:execution-profile',
  intelligenceTier: 'illo:cortex:intelligence-tier',
  effortLevel: 'illo:cortex:effort-level',
} as const;

export const DEFAULT_CORTEX_RUN_SETTINGS: CortexRunSettings = {
  executionProfile: 'fast',
  intelligenceTier: 'high',
  effortLevel: 'high',
};

export function normalizeExecutionProfile(value: unknown): CortexExecutionProfile {
  return String(value || '').trim().toLowerCase() === 'deep' ? 'deep' : 'fast';
}

export function normalizeIntelligenceTier(value: unknown): CortexIntelligenceTier {
  const normalized = String(value || '').trim().toLowerCase();
  return normalized === 'low' || normalized === 'medium' || normalized === 'high' ? normalized : 'high';
}

export function normalizeEffortLevel(value: unknown): CortexEffortLevel {
  const normalized = String(value || '').trim().toLowerCase();
  return normalized === 'low' || normalized === 'medium' || normalized === 'high' || normalized === 'xhigh'
    ? normalized
    : 'high';
}

export function normalizeRunSettings(
  settings: CortexRunSettingsInput | null | undefined,
  fallback: CortexRunSettings = DEFAULT_CORTEX_RUN_SETTINGS,
): CortexRunSettings {
  return {
    executionProfile: normalizeExecutionProfile(settings?.executionProfile ?? fallback.executionProfile),
    intelligenceTier: normalizeIntelligenceTier(settings?.intelligenceTier ?? fallback.intelligenceTier),
    effortLevel: normalizeEffortLevel(settings?.effortLevel ?? fallback.effortLevel),
  };
}

export function runSettingsOptions(
  settings: CortexRunSettingsInput,
): Pick<AgentRunOptions, 'executionProfile' | 'intelligenceTier' | 'effortLevel'> {
  return normalizeRunSettings(settings);
}

export function normalizeRunOptions(
  options: AgentRunOptions = {},
  currentSettings: CortexRunSettingsInput = DEFAULT_CORTEX_RUN_SETTINGS,
): AgentRunOptions {
  const settings = normalizeRunSettings({
    executionProfile: options.executionProfile ?? currentSettings.executionProfile,
    intelligenceTier: options.intelligenceTier ?? currentSettings.intelligenceTier,
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
      intelligenceTier: storage.getItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.intelligenceTier),
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
    const normalized = normalizeRunSettings(settings);
    storage.setItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.executionProfile, normalized.executionProfile);
    storage.setItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.intelligenceTier, normalized.intelligenceTier);
    storage.setItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.effortLevel, normalized.effortLevel);
    return true;
  } catch {
    return false;
  }
}
