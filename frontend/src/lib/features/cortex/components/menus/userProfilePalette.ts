import type { ConstellationTone } from '$lib/components/constellation/constellationTypes';

export type UserProfileColorOption = {
  id: string;
  label: string;
  tone: ConstellationTone;
};

export const DEFAULT_PROFILE_COLOR = '#6d28d9';

export const USER_PROFILE_COLOR_OPTIONS: UserProfileColorOption[] = [
  { id: '#c51f4a', label: 'Ruby', tone: 'spectral' },
  { id: '#c026d3', label: 'Fuchsia', tone: 'spectral' },
  { id: '#6d28d9', label: 'Violet', tone: 'spectral' },
  { id: '#4c1d95', label: 'Aubergine', tone: 'spectral' },
  { id: '#087f5b', label: 'Emerald', tone: 'spectral' },
  { id: '#166534', label: 'Forest', tone: 'spectral' },
  { id: '#6f8f00', label: 'Chartreuse', tone: 'spectral' },
  { id: '#9a7b00', label: 'Gold', tone: 'spectral' },
];
