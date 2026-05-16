import type { ModelTier } from './types';

export const MODEL_FIELDS: { key: ModelTier; label: string; help: string }[] = [
  { key: 'low', label: 'Low', help: 'Smallest capable model' },
  { key: 'medium', label: 'Medium', help: 'Balanced default model' },
  { key: 'high', label: 'High', help: 'Strongest model tier' },
];
