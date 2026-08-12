import type {
  CyclePolicyApplyRead,
  CyclePolicyChangeRead,
  CyclePolicyConfigurationRead,
  CyclePolicyDiffEntryRead,
  CyclePolicyFieldSourceRead,
  CyclePolicyJsonValue,
  CyclePolicyPreviewRead,
  CyclePolicyProposal,
  CyclePolicyHistoryRead,
  CycleRunRead,
  EffectiveCyclePolicyRead,
} from '$lib/api/client';
import type { RuntimeModelCatalogEntry } from '$lib/types/runtimeSettings';
import { parseServerDate } from '../../../utils/datetime.ts';

export const POLICY_FIELD_SCHEMA = [
  {
    key: 'prompt',
    label: 'Mission prompt',
    control: 'textarea',
    fullWidth: true,
    rows: 7,
    read: (policy: EffectiveCyclePolicyRead) => policy.configuration.prompt,
    proposal: (value: unknown) => String(value).trim(),
  },
  {
    key: 'schedule_expr',
    label: 'Stored schedule',
    control: 'text',
    mono: true,
    placeholder: '0 9 * * *',
    read: (policy: EffectiveCyclePolicyRead) => policy.configuration.schedule_expr,
    proposal: (value: unknown) => String(value).trim(),
  },
  {
    key: 'timezone',
    label: 'Timezone',
    control: 'text',
    placeholder: 'America/Toronto',
    read: (policy: EffectiveCyclePolicyRead) => policy.configuration.timezone,
    proposal: (value: unknown) => String(value).trim(),
  },
  {
    key: 'enabled',
    label: 'Status',
    control: 'toggle',
    read: (policy: EffectiveCyclePolicyRead) => policy.configuration.enabled,
    proposal: (value: unknown) => Boolean(value),
  },
  {
    key: 'model_override',
    label: 'Model override',
    control: 'model',
    read: (policy: EffectiveCyclePolicyRead) => policy.configuration.model_override ?? '',
    proposal: (value: unknown) => String(value).trim() || null,
  },
  {
    key: 'thinking_override',
    label: 'Thinking override',
    control: 'thinking',
    options: [
      { value: '', label: 'Workspace default' },
      { value: 'none', label: 'None' },
      { value: 'low', label: 'Low' },
      { value: 'medium', label: 'Medium' },
      { value: 'high', label: 'High' },
      { value: 'xhigh', label: 'xHigh' },
    ],
    read: (policy: EffectiveCyclePolicyRead) => policy.configuration.thinking_override ?? '',
    proposal: (value: unknown) => String(value) || null,
  },
  {
    key: 'guidance',
    label: 'Active guidance',
    control: 'guidance',
    fullWidth: true,
    read: (policy: EffectiveCyclePolicyRead) => [...policy.guidance],
    proposal: (value: unknown) => Array.isArray(value)
      ? value.map((item) => String(item).trim())
      : [],
  },
] as const;

type PolicyFieldDefinition = (typeof POLICY_FIELD_SCHEMA)[number];

export type CyclePolicyFieldKey = PolicyFieldDefinition['key'];

export type CyclePolicyDraft = {
  [Field in PolicyFieldDefinition as Field['key']]: ReturnType<Field['read']>;
};

export type CyclePolicyDraftErrors = Partial<Record<keyof CyclePolicyDraft, string>>;

export type CyclePolicyScheduleDiffValue = {
  schedule_expr: string;
  schedule_human: string;
  timezone: string;
};

export type PresentedCyclePolicyDiff =
  | {
      key: string;
      kind: 'value';
      field: string;
      label: string;
      before: string;
      after: string;
    }
  | {
      key: 'schedule';
      kind: 'schedule';
      field: 'schedule';
      label: 'Schedule';
      before: CyclePolicyScheduleDiffValue;
      after: CyclePolicyScheduleDiffValue;
    }
  | {
      key: 'guidance';
      kind: 'guidance';
      field: 'guidance';
      label: 'Guidance';
      added: string[];
      retired: string[];
    };

export type CyclePolicyReview =
  | {
      kind: 'edit';
      proposal: CyclePolicyProposal;
      preview: CyclePolicyPreviewRead;
    }
  | {
      kind: 'revert';
      changeId: number;
      preview: CyclePolicyPreviewRead;
    };

export type CyclePolicyEditorApi = Pick<
  typeof import('$lib/api/client').api,
  | 'previewCycleBehaviorPolicy'
  | 'applyCycleBehaviorPolicy'
  | 'previewCycleBehaviorPolicyRevert'
  | 'applyCycleBehaviorPolicyRevert'
  | 'getCycleBehaviorPolicy'
  | 'getCycleBehaviorPolicyHistory'
>;

export type CyclePolicyConfigurationEntry = {
  key: keyof CyclePolicyConfigurationRead & string;
  value: CyclePolicyConfigurationRead[keyof CyclePolicyConfigurationRead];
};

export type CyclePolicyActorPresentation = {
  kind: 'agent' | 'human' | 'system';
  label: 'Agent' | 'Human' | 'System';
  identity: string;
};

export type CycleRunPolicyChangeInspection = {
  id: number | null;
  version: number | null;
  actor_type: string | null;
  actor_id: string | null;
  source_reference: string | null;
  rationale: string | null;
  changed_fields: string[];
  applied_at: string | null;
};

export type CycleRunPolicyInspection = {
  hasSnapshot: boolean;
  revisionNumber: number | null;
  version: number | null;
  configuration: Array<{ key: CyclePolicySnapshotConfigurationKey; value: CyclePolicyJsonValue }>;
  guidance: string[];
  change: CycleRunPolicyChangeInspection | null;
};

export type CyclePolicySnapshotConfigurationKey = Exclude<
  keyof CyclePolicyConfigurationRead & string,
  'schedule_human'
>;

const CYCLE_POLICY_SNAPSHOT_CONFIGURATION_FIELDS = Object.keys({
  name: true,
  prompt: true,
  schedule_expr: true,
  timezone: true,
  enabled: true,
  max_concurrency: true,
  timeout_seconds: true,
  retry_policy: true,
  model_override: true,
  thinking_override: true,
  execution_policy_key: true,
  target_idea_id: true,
} satisfies Record<CyclePolicySnapshotConfigurationKey, true>) as CyclePolicySnapshotConfigurationKey[];

export function policyConfigurationEntries(
  configuration: CyclePolicyConfigurationRead,
): CyclePolicyConfigurationEntry[] {
  return Object.entries(configuration).map(([key, value]) => ({
    key: key as CyclePolicyConfigurationEntry['key'],
    value,
  }));
}

function policyJsonObject(
  value: CyclePolicyJsonValue | undefined,
): Record<string, CyclePolicyJsonValue> | null {
  if (!value || Array.isArray(value) || typeof value !== 'object') return null;
  return value;
}

function policyJsonNumber(value: CyclePolicyJsonValue | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function policyJsonString(value: CyclePolicyJsonValue | undefined): string | null {
  if (typeof value !== 'string') return null;
  return value.trim() || null;
}

function policyJsonStringList(value: CyclePolicyJsonValue | undefined): string[] | null {
  if (!Array.isArray(value)) return null;
  return value.filter((item): item is string => typeof item === 'string');
}

export function cycleRunPolicyInspection(run: CycleRunRead): CycleRunPolicyInspection {
  const revision = policyJsonObject(run.context_snapshot?.revision);
  const rawChange = policyJsonObject(run.context_snapshot?.behavior_change);
  const afterSnapshot = policyJsonObject(rawChange?.after_snapshot);
  const snapshot = afterSnapshot ?? revision;
  const configuration = snapshot
    ? CYCLE_POLICY_SNAPSHOT_CONFIGURATION_FIELDS.flatMap((key) => (
        Object.hasOwn(snapshot, key) ? [{ key, value: snapshot[key] }] : []
      ))
    : [];
  const snapshotGuidance = policyJsonStringList(snapshot?.guidance);
  const guidance = snapshotGuidance ?? run.guidance_snapshot.flatMap((item) => {
    const value = item.guidance;
    return typeof value === 'string' ? [value] : [];
  });
  const change = rawChange
    ? {
        id: policyJsonNumber(rawChange.id),
        version: policyJsonNumber(rawChange.version),
        actor_type: policyJsonString(rawChange.actor_type),
        actor_id: policyJsonString(rawChange.actor_id),
        source_reference: policyJsonString(rawChange.source_reference),
        rationale: policyJsonString(rawChange.rationale),
        changed_fields: policyJsonStringList(rawChange.changed_fields) ?? [],
        applied_at: policyJsonString(rawChange.applied_at),
      }
    : null;

  return {
    hasSnapshot: Boolean(snapshot || revision || configuration.length || snapshotGuidance),
    revisionNumber: policyJsonNumber(revision?.revision_number),
    version: change?.version ?? null,
    configuration,
    guidance,
    change,
  };
}

export function hydratePolicyDraft(policy: EffectiveCyclePolicyRead): CyclePolicyDraft {
  return Object.fromEntries(
    POLICY_FIELD_SCHEMA.map((field) => [field.key, field.read(policy)]),
  ) as CyclePolicyDraft;
}

export function clonePolicyDraft(draft: CyclePolicyDraft): CyclePolicyDraft {
  return { ...draft, guidance: [...draft.guidance] };
}

export function isPolicyDraftDirty(
  draft: CyclePolicyDraft | null,
  policy: EffectiveCyclePolicyRead | null,
): boolean {
  if (!draft || !policy) return false;
  const baseline = hydratePolicyDraft(policy);
  return POLICY_FIELD_SCHEMA.some((field) => {
    const current = draft[field.key];
    const original = baseline[field.key];
    if (Array.isArray(current) && Array.isArray(original)) {
      return current.length !== original.length
        || current.some((value, index) => value !== original[index]);
    }
    return current !== original;
  });
}

export function shouldConfirmPolicyDraftDiscard(
  draft: CyclePolicyDraft | null,
  policy: EffectiveCyclePolicyRead | null,
): boolean {
  return isPolicyDraftDirty(draft, policy);
}

export function isValidPolicySchedule(value: string): boolean {
  const schedule = value.trim();
  if (schedule.toLowerCase().startsWith('at:')) {
    const timestamp = schedule.slice(3).trim();
    return Boolean(timestamp) && !Number.isNaN(new Date(timestamp).getTime());
  }
  const fields = schedule.split(/\s+/);
  return fields.length === 5 && fields.every(Boolean);
}

export function validatePolicyDraft(
  draft: CyclePolicyDraft,
  modelCatalog: readonly Pick<RuntimeModelCatalogEntry, 'id'>[],
  currentModel: string | null = null,
): CyclePolicyDraftErrors {
  const errors: CyclePolicyDraftErrors = {};
  const prompt = draft.prompt.trim();
  const schedule = draft.schedule_expr.trim();
  const model = draft.model_override.trim();
  const guidance = draft.guidance.map((value) => value.trim());

  if (!prompt) errors.prompt = 'Mission prompt is required.';
  if (!schedule || !isValidPolicySchedule(schedule)) {
    errors.schedule_expr = 'Use a valid five-field cron rule or one-time at: timestamp.';
  }
  if (model) {
    const supportedModels = new Set(modelCatalog.map((entry) => entry.id));
    if (currentModel) supportedModels.add(currentModel);
    if (!supportedModels.has(model)) errors.model_override = 'Select a supported model.';
  }
  if (new Set(guidance).size !== guidance.length) {
    errors.guidance = 'Guidance entries must be unique.';
  }
  return errors;
}

export function policyProposalFromDraft(draft: CyclePolicyDraft): CyclePolicyProposal {
  return Object.fromEntries(
    POLICY_FIELD_SCHEMA.map((field) => [field.key, field.proposal(draft[field.key])]),
  ) as CyclePolicyProposal;
}

export async function reviewPolicyDraft(
  client: CyclePolicyEditorApi,
  cycleId: number,
  draft: CyclePolicyDraft,
  modelCatalog: readonly Pick<RuntimeModelCatalogEntry, 'id'>[],
  currentModel: string | null = null,
): Promise<{ review: CyclePolicyReview | null; errors: CyclePolicyDraftErrors }> {
  const errors = validatePolicyDraft(draft, modelCatalog, currentModel);
  if (Object.keys(errors).length) return { review: null, errors };
  const proposal = policyProposalFromDraft(draft);
  const preview = await client.previewCycleBehaviorPolicy(cycleId, { proposal });
  return { review: { kind: 'edit', proposal, preview }, errors: {} };
}

export async function reviewPolicyRevert(
  client: CyclePolicyEditorApi,
  cycleId: number,
  changeId: number,
): Promise<CyclePolicyReview> {
  return {
    kind: 'revert',
    changeId,
    preview: await client.previewCycleBehaviorPolicyRevert(cycleId, changeId),
  };
}

export async function applyPolicyReview(
  client: CyclePolicyEditorApi,
  cycleId: number,
  review: CyclePolicyReview | null,
  rationale: string,
  confirmRevert: () => boolean = () => true,
): Promise<{
  applied: CyclePolicyApplyRead;
  policy: EffectiveCyclePolicyRead;
  history: CyclePolicyHistoryRead;
} | null> {
  if (!review) throw new Error('Review the change before applying it.');
  const normalizedRationale = rationale.trim();
  if (!normalizedRationale) throw new Error('Rationale is required.');
  if (!review.preview.changed_fields.length) throw new Error('There are no changes to apply.');
  if (review.kind === 'revert' && !confirmRevert()) return null;

  const common = {
    expected_version: review.preview.expected_version,
    preview_digest: review.preview.preview_digest,
    rationale: normalizedRationale,
  };
  const applied = review.kind === 'edit'
    ? await client.applyCycleBehaviorPolicy(cycleId, {
        proposal: review.proposal,
        ...common,
      })
    : await client.applyCycleBehaviorPolicyRevert(cycleId, review.changeId, common);
  const [policy, history] = await Promise.all([
    client.getCycleBehaviorPolicy(cycleId),
    client.getCycleBehaviorPolicyHistory(cycleId),
  ]);
  return { applied, policy, history };
}

export function recoverPolicyDraftAfterConflict(
  draft: CyclePolicyDraft,
  latestPolicy: EffectiveCyclePolicyRead,
): { draft: CyclePolicyDraft; policy: EffectiveCyclePolicyRead } {
  return { draft: clonePolicyDraft(draft), policy: latestPolicy };
}

function scheduleDiffValue(value: CyclePolicyJsonValue): CyclePolicyScheduleDiffValue | null {
  if (!value || Array.isArray(value) || typeof value !== 'object') return null;
  const scheduleExpr = value.schedule_expr;
  const scheduleHuman = value.schedule_human;
  const timezone = value.timezone;
  if (typeof scheduleExpr !== 'string' || typeof scheduleHuman !== 'string' || typeof timezone !== 'string') {
    return null;
  }
  return { schedule_expr: scheduleExpr, schedule_human: scheduleHuman, timezone };
}

export function guidanceDiff(entry: CyclePolicyDiffEntryRead): {
  added: string[];
  retired: string[];
} {
  return { added: [...(entry.added ?? [])], retired: [...(entry.removed ?? [])] };
}

export function presentedPolicyDiff(preview: CyclePolicyPreviewRead): PresentedCyclePolicyDiff[] {
  const presented: PresentedCyclePolicyDiff[] = [];
  let scheduleAdded = false;
  for (const entry of preview.diff) {
    if (entry.kind === 'schedule') {
      if (scheduleAdded) continue;
      const before = scheduleDiffValue(entry.before);
      const after = scheduleDiffValue(entry.after);
      if (before && after) {
        presented.push({
          key: 'schedule',
          kind: 'schedule',
          field: 'schedule',
          label: 'Schedule',
          before,
          after,
        });
        scheduleAdded = true;
      }
      continue;
    }
    if (entry.kind === 'collection' && entry.field === 'guidance') {
      const { added, retired } = guidanceDiff(entry);
      presented.push({
        key: 'guidance',
        kind: 'guidance',
        field: 'guidance',
        label: 'Guidance',
        added,
        retired,
      });
      continue;
    }
    presented.push({
      key: entry.field,
      kind: 'value',
      field: entry.field,
      label: policyFieldLabel(entry.field),
      before: policyValueLabel(entry.before),
      after: policyValueLabel(entry.after),
    });
  }
  return presented;
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

export function policyActorPresentation(source: {
  actor_type?: string | null;
  actor_id?: string | null;
}): CyclePolicyActorPresentation {
  const actorType = String(source.actor_type ?? '').trim().toLowerCase();
  const identity = String(source.actor_id ?? '').trim() || 'Not recorded';
  if (actorType.includes('agent')) return { kind: 'agent', label: 'Agent', identity };
  if (actorType === 'human' || actorType === 'user') {
    return { kind: 'human', label: 'Human', identity };
  }
  return { kind: 'system', label: 'System', identity };
}

export function policySourceRunId(sourceReference: string | null | undefined): number | null {
  const match = /^(?:agent|agent_run):(\d+)$/.exec(String(sourceReference ?? '').trim());
  if (!match) return null;
  const runId = Number(match[1]);
  return Number.isSafeInteger(runId) && runId > 0 ? runId : null;
}

export function policyOriginatingRun(
  source: { source_reference?: string | null },
  runs: readonly CycleRunRead[],
): CycleRunRead | null {
  const agentRunId = policySourceRunId(source.source_reference);
  if (agentRunId === null) return null;
  return runs.find((run) => run.run_id === agentRunId) ?? null;
}

export function cycleRunAnchorId(runId: number): string {
  return `cycle-run-${runId}`;
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
