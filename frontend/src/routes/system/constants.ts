import type { ModelTier } from './types';

export const MODEL_FIELDS: { key: ModelTier; label: string; help: string }[] = [
  { key: 'low', label: 'Low', help: 'Summaries and light background work' },
  { key: 'medium', label: 'Medium', help: 'Default everyday work' },
  { key: 'high', label: 'High', help: 'Hard reasoning' },
];
