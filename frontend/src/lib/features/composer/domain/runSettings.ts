import type {
  CortexEffortLevel,
  CortexExecutionProfile,
} from '$lib/stores/cortex.svelte';
import type {
  CortexWorkspaceComposerSettingsGroup,
  CortexWorkspaceComposerIntentOption,
} from '$lib/features/composer/domain/composerAdapter';

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

export const MODEL_OPTIONS = [
  { value: 'openai/gpt-5.6-sol', label: 'GPT-5.6 Sol', description: 'Preview model for eligible personal connections' },
  { value: 'openai/gpt-5.5', label: 'GPT-5.5', description: 'Best quality for hard reasoning' },
  { value: 'openai/gpt-5.4', label: 'GPT-5.4', description: 'Balanced general-purpose model' },
  { value: 'openai/gpt-5.4-mini', label: 'GPT-5.4 Mini', description: 'Fast and economical' },
  { value: 'openai/gpt-5-mini', label: 'GPT-5 Mini', description: 'Lower cost and latency' },
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

export function buildRunSettingsGroups(values: {
  mode: CortexExecutionProfile;
  model: string;
  effort: CortexEffortLevel;
}): readonly CortexWorkspaceComposerSettingsGroup[] {
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
      options: MODEL_OPTIONS,
      value: values.model,
      ariaLabel: 'Model',
    },
    {
      key: 'effort',
      label: 'Effort',
      options: EFFORT_OPTIONS,
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
