import type { CyclePolicyEditorApi } from './effectivePolicy.ts';

export type EffectivePolicyClientResolver = (
  cycleId: number,
) => CyclePolicyEditorApi | undefined;

export const EFFECTIVE_POLICY_CLIENT_CONTEXT = Symbol('effective-policy-client');
