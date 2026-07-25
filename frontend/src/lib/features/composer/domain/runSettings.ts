import {
  WORKSPACE_DEFAULT_RUN_SETTING,
  type CortexEffortSelection,
} from '$lib/features/cortex/controllers/runSettingsController';
import type {
  CortexWorkspaceComposerSettingsGroup,
  CortexWorkspaceComposerIntentOption,
} from '$lib/features/composer/domain/composerAdapter';
import type { RuntimeModelCatalogEntry } from '$lib/types/runtimeSettings';

const WORKSPACE_DEFAULT_MODEL_OPTION = {
  value: WORKSPACE_DEFAULT_RUN_SETTING,
  label: 'Workspace default',
  description: 'Use the model configured for this workspace',
} as const satisfies CortexWorkspaceComposerIntentOption;

export const EFFORT_OPTIONS = [
  {
    value: WORKSPACE_DEFAULT_RUN_SETTING,
    label: 'Workspace default',
    description: 'Use the effort configured for this workspace',
  },
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
  const catalogOptions = catalog.map((entry) => ({
    value: entry.id,
    label: entry.label,
    description: entry.description,
  }));
  if (
    catalogOptions.length === 0
    && selectedModel
    && selectedModel !== WORKSPACE_DEFAULT_RUN_SETTING
  ) {
    return [
      WORKSPACE_DEFAULT_MODEL_OPTION,
      {
        value: selectedModel,
        label: selectedModel.split('/', 2).at(-1) || selectedModel,
        description: 'Current model selection',
      },
    ];
  }
  return [WORKSPACE_DEFAULT_MODEL_OPTION, ...catalogOptions];
}

export function buildRunSettingsGroups(values: {
  model: string;
  modelCatalog: readonly RuntimeModelCatalogEntry[];
  effort: CortexEffortSelection;
}): readonly CortexWorkspaceComposerSettingsGroup[] {
  const selectedCatalogEntry = values.modelCatalog.find((entry) =>
    values.model === WORKSPACE_DEFAULT_RUN_SETTING
      ? entry.default_provenance.workspace_default
      : entry.id === values.model);
  const [workspaceDefaultEffortOption, ...explicitEffortOptions] = EFFORT_OPTIONS;
  const effortOptions = selectedCatalogEntry
    ? [
        workspaceDefaultEffortOption,
        ...explicitEffortOptions.filter((option) =>
          selectedCatalogEntry.supported_effort_tiers.includes(option.value)),
      ]
    : EFFORT_OPTIONS;
  return [
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
    setModel: (value: string) => void;
    setEffortLevel: (value: CortexEffortSelection) => void;
  },
): void {
  if (key === 'model') handlers.setModel(value);
  if (key === 'effort') handlers.setEffortLevel(value as CortexEffortSelection);
}
