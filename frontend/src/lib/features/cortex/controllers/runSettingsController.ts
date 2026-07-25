import type {
  AgentRunOptions,
  CortexEffortLevel,
} from '$lib/types/cortex';

export const WORKSPACE_DEFAULT_RUN_SETTING = 'workspace-default' as const;
export type CortexEffortSelection =
  | CortexEffortLevel
  | typeof WORKSPACE_DEFAULT_RUN_SETTING;

export interface CortexRunSettings {
  model: string;
  effortLevel: CortexEffortSelection;
}

export type CortexRunSettingsInput = {
  model?: unknown;
  effortLevel?: unknown;
};

export type RunSettingsStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export const CORTEX_RUN_SETTINGS_STORAGE_KEYS = {
  model: 'illo:cortex:model',
  effortLevel: 'illo:cortex:effort-level',
  workspaceDefaultMigration: 'illo:cortex:workspace-default-migration',
} as const;

const LEGACY_EXECUTION_PROFILE_STORAGE_KEY = 'illo:cortex:execution-profile';
const WORKSPACE_DEFAULT_MIGRATION_VERSION = '1';

export const DEFAULT_CORTEX_RUN_SETTINGS: CortexRunSettings = {
  model: WORKSPACE_DEFAULT_RUN_SETTING,
  effortLevel: WORKSPACE_DEFAULT_RUN_SETTING,
};

export function isWorkspaceDefaultRunSetting(
  value: unknown,
): value is typeof WORKSPACE_DEFAULT_RUN_SETTING {
  return String(value || '').trim().toLowerCase() === WORKSPACE_DEFAULT_RUN_SETTING;
}

export function normalizeModel(
  value: unknown,
  fallback: string = WORKSPACE_DEFAULT_RUN_SETTING,
): string {
  const normalized = String(value || '').trim().replace(':', '/');
  if (isWorkspaceDefaultRunSetting(normalized)) return WORKSPACE_DEFAULT_RUN_SETTING;
  return normalized || fallback;
}

export function normalizeEffortLevel(
  value: unknown,
  fallback: CortexEffortSelection = WORKSPACE_DEFAULT_RUN_SETTING,
): CortexEffortSelection {
  const normalized = String(value || '').trim().toLowerCase();
  if (isWorkspaceDefaultRunSetting(normalized)) return WORKSPACE_DEFAULT_RUN_SETTING;
  return normalized === 'none' || normalized === 'low' || normalized === 'medium' || normalized === 'high' || normalized === 'xhigh'
    ? normalized
    : fallback;
}

export function normalizeRunSettings(
  settings: CortexRunSettingsInput | null | undefined,
  fallback: CortexRunSettings = DEFAULT_CORTEX_RUN_SETTINGS,
): CortexRunSettings {
  return {
    model: normalizeModel(settings?.model ?? fallback.model, fallback.model),
    effortLevel: normalizeEffortLevel(
      settings?.effortLevel ?? fallback.effortLevel,
      fallback.effortLevel,
    ),
  };
}

export function runSettingsOptions(
  settings: CortexRunSettingsInput,
): Pick<AgentRunOptions, 'model' | 'effortLevel'> {
  const normalized = normalizeRunSettings(settings);
  const options: Pick<AgentRunOptions, 'model' | 'effortLevel'> = {};
  if (!isWorkspaceDefaultRunSetting(normalized.model)) {
    options.model = normalized.model;
  }
  if (!isWorkspaceDefaultRunSetting(normalized.effortLevel)) {
    options.effortLevel = normalized.effortLevel;
  }
  return options;
}

export function normalizeRunOptions(
  options: AgentRunOptions = {},
  currentSettings: CortexRunSettingsInput = DEFAULT_CORTEX_RUN_SETTINGS,
): AgentRunOptions {
  const settings = runSettingsOptions({
    model: options.model ?? currentSettings.model,
    effortLevel: options.effortLevel ?? currentSettings.effortLevel,
  });
  const {
    model: _model,
    effortLevel: _effortLevel,
    ...remainingOptions
  } = options;
  return {
    ...remainingOptions,
    ...settings,
    metadata: { ...(options.metadata || {}) },
  };
}

export function routingMetadataForRunOptions(
  options: AgentRunOptions,
): Record<string, string> {
  const metadata: Record<string, string> = {};
  if (options.model) metadata.model = options.model;
  if (options.effortLevel) {
    metadata.thinking_tier = options.effortLevel;
    metadata.effort = options.effortLevel;
  }
  return metadata;
}

export function loadRunSettings(
  storage: RunSettingsStorage | null | undefined,
  fallback: CortexRunSettings = DEFAULT_CORTEX_RUN_SETTINGS,
): CortexRunSettings {
  if (!storage) return { ...fallback };
  try {
    if (
      storage.getItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.workspaceDefaultMigration)
      !== WORKSPACE_DEFAULT_MIGRATION_VERSION
    ) {
      storage.removeItem(LEGACY_EXECUTION_PROFILE_STORAGE_KEY);
      storage.removeItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.model);
      storage.removeItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.effortLevel);
      storage.setItem(
        CORTEX_RUN_SETTINGS_STORAGE_KEYS.workspaceDefaultMigration,
        WORKSPACE_DEFAULT_MIGRATION_VERSION,
      );
      return { ...fallback };
    }
    return normalizeRunSettings({
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
    storage.setItem(
      CORTEX_RUN_SETTINGS_STORAGE_KEYS.workspaceDefaultMigration,
      WORKSPACE_DEFAULT_MIGRATION_VERSION,
    );
    if (settings.model !== undefined) {
      const model = normalizeModel(settings.model);
      if (isWorkspaceDefaultRunSetting(model)) {
        storage.removeItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.model);
      } else {
        storage.setItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.model, model);
      }
    }
    if (settings.effortLevel !== undefined) {
      const effortLevel = normalizeEffortLevel(settings.effortLevel);
      if (isWorkspaceDefaultRunSetting(effortLevel)) {
        storage.removeItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.effortLevel);
      } else {
        storage.setItem(CORTEX_RUN_SETTINGS_STORAGE_KEYS.effortLevel, effortLevel);
      }
    }
    return true;
  } catch {
    return false;
  }
}
