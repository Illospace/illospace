import type {
  CyclePolicyChangeRead,
  CyclePolicyConfigurationRead,
  CyclePolicyFieldSourceRead,
  CyclePolicyJsonValue,
} from '$lib/api/client';
import { parseServerDate } from '../../../utils/datetime.ts';

export type CyclePolicyConfigurationEntry = {
  key: keyof CyclePolicyConfigurationRead & string;
  value: CyclePolicyConfigurationRead[keyof CyclePolicyConfigurationRead];
};

export function policyConfigurationEntries(
  configuration: CyclePolicyConfigurationRead,
): CyclePolicyConfigurationEntry[] {
  return Object.entries(configuration).map(([key, value]) => ({
    key: key as CyclePolicyConfigurationEntry['key'],
    value,
  }));
}

export function policyFieldLabel(field: string): string {
  if (field === 'prompt') return 'Mission prompt';
  if (field === 'schedule_expr') return 'Schedule rule';
  return field
    .replaceAll('_', ' ')
    .replace(/^./, (first) => first.toUpperCase());
}

export function policyValueLabel(
  value: CyclePolicyConfigurationEntry['value'] | CyclePolicyJsonValue,
): string {
  if (value === null || value === undefined || value === '') return 'Not set';
  if (typeof value === 'boolean') return value ? 'Enabled' : 'Disabled';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return String(value);
}

export function policyFieldSource(
  fieldSources: Record<string, CyclePolicyFieldSourceRead>,
  field: string,
): CyclePolicyFieldSourceRead | undefined {
  if (field === 'schedule_human') return fieldSources.schedule_expr;
  return fieldSources[field];
}

export function policySourceLabel(source: {
  actor_type?: string | null;
  actor_id?: string | null;
  source_reference?: string | null;
}): string {
  if (source.source_reference) return source.source_reference;
  const actor = [source.actor_type, source.actor_id].filter(Boolean).join(' · ');
  return actor || 'Initial cycle definition';
}

export function formatPolicyDateTime(
  timestamp: string | null | undefined,
  displayTimezone: string,
  locale?: string,
): string {
  const date = parseServerDate(timestamp);
  if (!date) return 'Time not recorded';
  try {
    return new Intl.DateTimeFormat(locale, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: displayTimezone,
    }).format(date);
  } catch {
    return new Intl.DateTimeFormat(locale, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: 'UTC',
    }).format(date);
  }
}

export function retiredGuidance(change: CyclePolicyChangeRead): string[] {
  const activeAfter = new Set(change.after_snapshot.guidance);
  return change.before_snapshot.guidance.filter((guidance) => !activeAfter.has(guidance));
}
