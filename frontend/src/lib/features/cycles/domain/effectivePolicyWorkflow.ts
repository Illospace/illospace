import type {
  CyclePolicyConflictDetail,
  CyclePolicyHistoryRead,
  EffectiveCyclePolicyRead,
} from '$lib/api/client';
import type { RuntimeModelCatalogEntry } from '$lib/types/runtimeSettings';

import {
  applyPolicyReview,
  clonePolicyDraft,
  hydratePolicyDraft,
  isPolicyDraftDirty,
  recoverPolicyDraftAfterConflict,
  reviewPolicyDraft,
  reviewPolicyRevert,
  type CyclePolicyDraft,
  type CyclePolicyDraftErrors,
  type CyclePolicyEditorApi,
  type CyclePolicyReview,
} from './effectivePolicy.ts';

export const POLICY_ACTIVE_RUN_BOUNDARY =
  'Active runs are unchanged. Future runs use this policy only after apply.';

export type CyclePolicyWorkflowData = {
  policy: EffectiveCyclePolicyRead;
  history: CyclePolicyHistoryRead;
};

export type CyclePolicyWorkflowState =
  | {
      kind: 'view';
      data: CyclePolicyWorkflowData;
      notice?: string;
      historyError?: string;
    }
  | {
      kind: 'edit';
      data: CyclePolicyWorkflowData;
      draft: CyclePolicyDraft;
      errors: CyclePolicyDraftErrors;
      error?: string;
      notice?: string;
      historyError?: string;
    }
  | {
      kind: 'reviewing';
      data: CyclePolicyWorkflowData;
      draft: CyclePolicyDraft;
      historyError?: string;
    }
  | {
      kind: 'review';
      data: CyclePolicyWorkflowData;
      draft: CyclePolicyDraft | null;
      review: CyclePolicyReview;
      rationale: string;
      error?: string;
      historyError?: string;
    }
  | {
      kind: 'applying';
      data: CyclePolicyWorkflowData;
      draft: CyclePolicyDraft | null;
      review: CyclePolicyReview;
      rationale: string;
      historyError?: string;
    }
  | {
      kind: 'conflicted';
      data: CyclePolicyWorkflowData;
      draft: CyclePolicyDraft | null;
      notice: string;
      historyError?: string;
    }
  | {
      kind: 'reverting';
      data: CyclePolicyWorkflowData;
      changeId: number;
      historyError?: string;
    };

export type PolicyReviewProps = {
  review: CyclePolicyReview;
  rationale: string;
  applying: boolean;
  applyDisabled: boolean;
  activeRunBoundary: typeof POLICY_ACTIVE_RUN_BOUNDARY;
  error?: string;
};

type WorkflowOptions = {
  client: CyclePolicyEditorApi;
  cycleId: number;
  data: CyclePolicyWorkflowData;
  modelCatalog?: readonly RuntimeModelCatalogEntry[];
  onStateChange?: (state: CyclePolicyWorkflowState) => void;
  onPolicyApplied?: (policy: EffectiveCyclePolicyRead) => void | Promise<void>;
};

export function policyWorkflowDraft(state: CyclePolicyWorkflowState): CyclePolicyDraft | null {
  if (
    state.kind === 'edit'
    || state.kind === 'reviewing'
    || state.kind === 'review'
    || state.kind === 'applying'
    || state.kind === 'conflicted'
  ) return state.draft;
  return null;
}

export function isPolicyWorkflowDirty(state: CyclePolicyWorkflowState): boolean {
  return isPolicyDraftDirty(policyWorkflowDraft(state), state.data.policy);
}

export function policyReviewProps(
  state: Extract<CyclePolicyWorkflowState, { kind: 'review' | 'applying' }>,
): PolicyReviewProps {
  return {
    review: state.review,
    rationale: state.rationale,
    applying: state.kind === 'applying',
    applyDisabled: state.kind === 'applying'
      || !state.review.preview.changed_fields.length
      || !state.rationale.trim(),
    activeRunBoundary: POLICY_ACTIVE_RUN_BOUNDARY,
    ...(state.kind === 'review' && state.error ? { error: state.error } : {}),
  };
}

export function workflowErrorMessage(value: unknown, fallback: string): string {
  if (value && typeof value === 'object' && 'detail' in value) {
    const detail = (value as { detail?: unknown }).detail;
    return typeof detail === 'string' ? detail : fallback;
  }
  return value instanceof Error ? value.message : fallback;
}

export function policyConflictDetail(value: unknown): CyclePolicyConflictDetail | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = value as { status?: unknown; detail?: unknown };
  if (candidate.status !== 409 || !candidate.detail || typeof candidate.detail !== 'object') {
    return null;
  }
  const detail = candidate.detail as Partial<CyclePolicyConflictDetail>;
  if (!detail.latest_effective_policy || typeof detail.reason !== 'string') return null;
  return detail as CyclePolicyConflictDetail;
}

export class EffectivePolicyWorkflowController {
  private current: CyclePolicyWorkflowState;
  private operationSerial = 0;
  private readonly client: CyclePolicyEditorApi;
  private readonly cycleId: number;
  private readonly modelCatalog: readonly RuntimeModelCatalogEntry[];
  private readonly onStateChange?: (state: CyclePolicyWorkflowState) => void;
  private readonly onPolicyApplied?: (
    policy: EffectiveCyclePolicyRead,
  ) => void | Promise<void>;

  constructor(options: WorkflowOptions) {
    this.client = options.client;
    this.cycleId = options.cycleId;
    this.modelCatalog = options.modelCatalog ?? [];
    this.onStateChange = options.onStateChange;
    this.onPolicyApplied = options.onPolicyApplied;
    this.current = { kind: 'view', data: options.data };
  }

  get state(): CyclePolicyWorkflowState {
    return this.current;
  }

  private transition(state: CyclePolicyWorkflowState): void {
    this.current = state;
    this.onStateChange?.(state);
  }

  dispose(): void {
    this.operationSerial += 1;
  }

  startEditing(): void {
    if (
      this.current.kind !== 'view'
      && !(this.current.kind === 'conflicted' && !this.current.draft)
    ) return;
    this.transition({
      kind: 'edit',
      data: this.current.data,
      draft: hydratePolicyDraft(this.current.data.policy),
      errors: {},
    });
  }

  async loadMoreHistory(): Promise<void> {
    const { pagination } = this.current.data.history;
    if (!pagination.has_more || pagination.next_offset === null) return;
    const version = this.current.data.policy.version;
    try {
      const nextHistory = await this.client.getCycleBehaviorPolicyHistory(
        this.cycleId,
        pagination.limit,
        pagination.next_offset,
      );
      if (this.current.data.policy.version !== version) return;
      this.transition({
        ...this.current,
        data: {
          ...this.current.data,
          history: {
            items: [...this.current.data.history.items, ...nextHistory.items],
            pagination: nextHistory.pagination,
          },
        },
        historyError: undefined,
      });
    } catch (error) {
      this.transition({
        ...this.current,
        historyError: workflowErrorMessage(error, 'Older history failed to load.'),
      });
    }
  }

  updateDraft(draft: CyclePolicyDraft): void {
    if (this.current.kind === 'edit') {
      this.transition({
        ...this.current,
        draft: clonePolicyDraft(draft),
        errors: {},
        error: undefined,
      });
      return;
    }
    if (this.current.kind === 'conflicted' && this.current.draft) {
      this.transition({ ...this.current, draft: clonePolicyDraft(draft) });
    }
  }

  cancelEditing(confirmDiscard: () => boolean = () => true): void {
    const draft = policyWorkflowDraft(this.current);
    if (!draft) return;
    if (isPolicyDraftDirty(draft, this.current.data.policy) && !confirmDiscard()) return;
    this.operationSerial += 1;
    this.transition({ kind: 'view', data: this.current.data });
  }

  leaveReview(): void {
    if (this.current.kind !== 'review') return;
    if (this.current.review.kind === 'edit' && this.current.draft) {
      this.transition({
        kind: 'edit',
        data: this.current.data,
        draft: this.current.draft,
        errors: {},
      });
      return;
    }
    this.transition({ kind: 'view', data: this.current.data });
  }

  setRationale(rationale: string): void {
    if (this.current.kind !== 'review') return;
    this.transition({ ...this.current, rationale, error: undefined });
  }

  async reviewDraft(): Promise<void> {
    if (this.current.kind !== 'edit' && this.current.kind !== 'conflicted') return;
    const draft = this.current.draft;
    if (!draft) return;
    const data = this.current.data;
    const serial = ++this.operationSerial;
    this.transition({ kind: 'reviewing', data, draft });
    try {
      const result = await reviewPolicyDraft(
        this.client,
        this.cycleId,
        clonePolicyDraft(draft),
        this.modelCatalog,
        data.policy.configuration.model_override,
      );
      if (serial !== this.operationSerial) return;
      if (!result.review) {
        this.transition({ kind: 'edit', data, draft, errors: result.errors });
        return;
      }
      this.transition({
        kind: 'review',
        data,
        draft,
        review: result.review,
        rationale: '',
      });
    } catch (error) {
      if (serial !== this.operationSerial) return;
      this.transition({
        kind: 'edit',
        data,
        draft,
        errors: {},
        error: workflowErrorMessage(error, 'The change could not be reviewed.'),
      });
    }
  }

  async beginRevert(changeId: number): Promise<void> {
    if (this.current.kind !== 'view') return;
    const data = this.current.data;
    const serial = ++this.operationSerial;
    this.transition({ kind: 'reverting', data, changeId });
    try {
      const review = await reviewPolicyRevert(this.client, this.cycleId, changeId);
      if (serial !== this.operationSerial) return;
      this.transition({ kind: 'review', data, draft: null, review, rationale: '' });
    } catch (error) {
      if (serial !== this.operationSerial) return;
      this.transition({
        kind: 'view',
        data,
        historyError: workflowErrorMessage(error, 'The revert could not be reviewed.'),
      });
    }
  }

  async applyReviewedChange(confirmRevert: () => boolean = () => true): Promise<void> {
    if (this.current.kind !== 'review' || policyReviewProps(this.current).applyDisabled) return;
    const { data, draft, review, rationale } = this.current;
    const serial = ++this.operationSerial;
    this.transition({ kind: 'applying', data, draft, review, rationale });
    try {
      const result = await applyPolicyReview(
        this.client,
        this.cycleId,
        review,
        rationale,
        confirmRevert,
      );
      if (serial !== this.operationSerial) return;
      if (!result) {
        this.transition({ kind: 'review', data, draft, review, rationale });
        return;
      }
      const nextData = { policy: result.policy, history: result.history };
      this.transition({
        kind: 'view',
        data: nextData,
        notice: review.kind === 'revert'
          ? 'Revert applied as a new behavior version.'
          : 'Behavior applied for future runs.',
      });
      await this.onPolicyApplied?.(result.policy);
    } catch (error) {
      if (serial !== this.operationSerial) return;
      const conflict = policyConflictDetail(error);
      if (conflict) {
        await this.recoverConflict(data, draft, conflict, serial);
        return;
      }
      this.transition({
        kind: 'review',
        data,
        draft,
        review,
        rationale,
        error: workflowErrorMessage(error, 'The change could not be applied.'),
      });
    }
  }

  private async recoverConflict(
    previousData: CyclePolicyWorkflowData,
    draft: CyclePolicyDraft | null,
    detail: CyclePolicyConflictDetail,
    serial: number,
  ): Promise<void> {
    let history = previousData.history;
    let historyError: string | undefined;
    try {
      history = await this.client.getCycleBehaviorPolicyHistory(this.cycleId);
    } catch {
      historyError = 'History could not be refreshed after the conflict.';
    }
    if (serial !== this.operationSerial) return;
    const recoveredDraft = draft
      ? recoverPolicyDraftAfterConflict(draft, detail.latest_effective_policy).draft
      : null;
    this.transition({
      kind: 'conflicted',
      data: { policy: detail.latest_effective_policy, history },
      draft: recoveredDraft,
      notice: recoveredDraft
        ? 'Another person changed this Cycle. Your draft is safe. Review it against the latest version and try again.'
        : 'Another person changed this Cycle. Review the latest version before you try again.',
      ...(historyError ? { historyError } : {}),
    });
  }
}
