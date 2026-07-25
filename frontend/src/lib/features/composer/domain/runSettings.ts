import type {
  CortexEffortLevel,
  CortexExecutionProfile,
} from '$lib/stores/cortex.svelte';
import type {
  CortexWorkspaceComposerSettingsGroup,
  CortexWorkspaceComposerIntentOption,
} from '$lib/features/composer/domain/composerAdapter';
import type { RuntimeModelCatalogEntry } from '$lib/types/runtimeSettings';

export const EXECUTION_PROFILE_OPTIONS = [
  {
    value: 'fast',
    label: 'Fast',
    description: 'Direct single-session work',
    icon: 'bolt',
  },
  {
    value: 'deep',
    label: 'Deep',
    description: 'Run graph with coordinator and workers',
    icon: 'route',
  },
] as const satisfies readonly CortexWorkspaceComposerIntentOption[];

export const EFFORT_OPTIONS = [
  { value: 'none', label: 'None', description: 'No additional reasoning effort' },
  { value: 'low', label: 'Low', description: 'Quick reasoning pass' },
  { value: 'medium', label: 'Medium', description: 'Balanced reasoning depth' },
  { value: 'high', label: 'High', description: 'Deeper reasoning for hard work' },
  { value: 'xhigh', label: 'xHigh', description: 'Maximum reasoning depth' },
] as const satisfies readonly CortexWorkspaceComposerIntentOption[];

export const STEERING_INTENT_OPTIONS = [
  { value: 'steer', label: 'Steer', description: 'Guide the active run', icon: 'reply-thread' },
  { value: 'queue', label: 'Queue', description: 'Run after this reply', icon: 'queue' },
] as const satisfies readonly CortexWorkspaceComposerIntentOption[];

export type ActiveRunMessageIntent = (typeof STEERING_INTENT_OPTIONS)[number]['value'];

function modelOptions(
  catalog: readonly RuntimeModelCatalogEntry[],
  selectedModel: string,
): readonly CortexWorkspaceComposerIntentOption[] {
  const options = catalog.map((entry) => ({
    value: entry.id,
    label: entry.label,
    description: entry.description,
  }));
  if (options.length > 0 || !selectedModel) return options;
  return [{
    value: selectedModel,
    label: selectedModel.split('/', 2).at(-1) || selectedModel,
    description: 'Current workspace model',
  }];
}

export function buildRunSettingsGroups(values: {
  mode: CortexExecutionProfile;
  model: string;
  effort: CortexEffortLevel;
  modelCatalog: readonly RuntimeModelCatalogEntry[];
}): readonly CortexWorkspaceComposerSettingsGroup[] {
  const selectedCatalogEntry = values.modelCatalog.find((entry) => entry.id === values.model);
  const effortOptions = selectedCatalogEntry
    ? EFFORT_OPTIONS.filter((option) =>
        selectedCatalogEntry.supported_effort_tiers.includes(option.value))
    : EFFORT_OPTIONS;
  return [
    {
      key: 'mode',
      label: 'Mode',
      options: EXECUTION_PROFILE_OPTIONS,
      value: values.mode,
      ariaLabel: 'Mode',
    },
    {
      key: 'model',
      label: 'Model',
      options: modelOptions(values.modelCatalog, values.model),
      value: values.model,
      ariaLabel: 'Model',
    },
    {
      key: 'effort',
      label: 'Effort',
      options: effortOptions,
      value: values.effort,
      ariaLabel: 'Effort',
    },
  ];
}

export function applyRunSetting(
  key: string,
  value: string,
  handlers: {
    setExecutionProfile: (value: CortexExecutionProfile) => void;
    setModel: (value: string) => void;
    setEffortLevel: (value: CortexEffortLevel) => void;
  },
): void {
  if (key === 'mode') handlers.setExecutionProfile(value as CortexExecutionProfile);
  if (key === 'model') handlers.setModel(value);
  if (key === 'effort') handlers.setEffortLevel(value as CortexEffortLevel);
}
