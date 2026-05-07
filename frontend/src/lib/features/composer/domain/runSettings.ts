import type {
  CortexEffortLevel,
  CortexExecutionProfile,
  CortexIntelligenceTier,
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

export const INTELLIGENCE_OPTIONS = [
  { value: 'low', label: 'Low', description: 'Smallest capable model' },
  { value: 'medium', label: 'Medium', description: 'Balanced default model' },
  { value: 'high', label: 'High', description: 'Strongest model tier' },
] as const satisfies readonly CortexWorkspaceComposerIntentOption[];

export const EFFORT_OPTIONS = [
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
  intelligence: CortexIntelligenceTier;
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
      key: 'intelligence',
      label: 'Intelligence',
      options: INTELLIGENCE_OPTIONS,
      value: values.intelligence,
      ariaLabel: 'Intelligence',
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
    setIntelligenceTier: (value: CortexIntelligenceTier) => void;
    setEffortLevel: (value: CortexEffortLevel) => void;
  },
): void {
  if (key === 'mode') handlers.setExecutionProfile(value as CortexExecutionProfile);
  if (key === 'intelligence') handlers.setIntelligenceTier(value as CortexIntelligenceTier);
  if (key === 'effort') handlers.setEffortLevel(value as CortexEffortLevel);
}
