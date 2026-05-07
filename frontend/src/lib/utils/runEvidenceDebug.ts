export type RunEvidenceTone = 'passed' | 'failed' | 'warning' | 'skipped' | 'unknown';

export interface RunEvidenceCounts {
  files: number;
  commands: number;
  artifacts: number;
  unresolved: number;
  workerResults: number;
}

export interface RunStepEvidenceDebug {
  stepId: string;
  label: string;
  tools: string[];
  evidenceLabel: string;
  counts: RunEvidenceCounts;
}

export interface RunEvidenceDebug {
  tone: RunEvidenceTone;
  defaultOpen: boolean;
  summaryLabel: string;
  contractType?: string;
  contractRequirements: string[];
  verifierLabel?: string;
  missingEvidence: string[];
  unsupportedClaims: string[];
  counts: RunEvidenceCounts;
  steps: RunStepEvidenceDebug[];
}

const ZERO_COUNTS: RunEvidenceCounts = {
  files: 0,
  commands: 0,
  artifacts: 0,
  unresolved: 0,
  workerResults: 0,
};

function asRecord(value: unknown): Record<string, any> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, any>) : {};
}

function asArray(value: unknown): any[] {
  return Array.isArray(value) ? value : [];
}

function compactText(value: unknown, limit = 96): string {
  const text = String(value ?? '').trim();
  if (!text) return '';
  return text.length > limit ? `${text.slice(0, limit - 3).trim()}...` : text;
}

function unique(values: string[], limit = 6): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const text = compactText(value);
    if (!text || seen.has(text)) continue;
    seen.add(text);
    result.push(text);
    if (result.length >= limit) break;
  }
  return result;
}

function addCounts(left: RunEvidenceCounts, right: Partial<RunEvidenceCounts>): void {
  left.files += right.files ?? 0;
  left.commands += right.commands ?? 0;
  left.artifacts += right.artifacts ?? 0;
  left.unresolved += right.unresolved ?? 0;
  left.workerResults += right.workerResults ?? 0;
}

function evidenceCountsForArtifact(artifact: Record<string, any>): RunEvidenceCounts {
  const counts = { ...ZERO_COUNTS };
  const type = String(artifact.type || '').trim();

  if (type === 'worker_result') {
    const evidence = asRecord(artifact.evidence);
    counts.workerResults = 1;
    counts.files = asArray(evidence.files).length;
    counts.commands = asArray(evidence.commands).length;
    counts.artifacts = asArray(evidence.artifacts).length;
    counts.unresolved = asArray(evidence.unresolved_uncertainty).length;
    return counts;
  }

  if (type === 'file' || type === 'file_observation') counts.files = 1;
  else if (type === 'command_run' || type === 'test_run') counts.commands = 1;
  else if (type && type !== 'worker_activity' && type !== 'worker_assignment') counts.artifacts = 1;

  return counts;
}

function formatCounts(counts: RunEvidenceCounts): string {
  const parts = [
    counts.workerResults ? `${counts.workerResults} worker` : '',
    counts.files ? `${counts.files} file${counts.files === 1 ? '' : 's'}` : '',
    counts.commands ? `${counts.commands} command${counts.commands === 1 ? '' : 's'}` : '',
    counts.artifacts ? `${counts.artifacts} artifact${counts.artifacts === 1 ? '' : 's'}` : '',
    counts.unresolved ? `${counts.unresolved} uncertain` : '',
  ].filter(Boolean);
  return parts.join(' / ') || 'no evidence';
}

function formatRequirement(key: string, value: unknown): string {
  if (Array.isArray(value)) {
    if (key === 'pull_requests') return `${value.length} PR ref${value.length === 1 ? '' : 's'}`;
    return `${key}: ${value.length}`;
  }

  if (value && typeof value === 'object') {
    const record = asRecord(value);
    const truthyKeys = Object.entries(record)
      .filter(([, item]) => item === true)
      .map(([itemKey]) => itemKey);
    if (truthyKeys.length) return `${key}: ${truthyKeys.join(', ')}`;
    return `${key}: ${Object.keys(record).slice(0, 3).join(', ')}`;
  }

  if (typeof value === 'boolean') return `${key}: ${value ? 'yes' : 'no'}`;
  return `${key}: ${compactText(value, 56)}`;
}

function contractRequirements(item: Record<string, any>): string[] {
  const requirements = asRecord(item.contract_requirements);
  return Object.entries(requirements)
    .map(([key, value]) => formatRequirement(key, value))
    .filter(Boolean)
    .slice(0, 6);
}

function verificationTone(status: string): RunEvidenceTone {
  if (status === 'passed' || status === 'pass' || status === 'satisfied') return 'passed';
  if (status === 'failed' || status === 'fail') return 'failed';
  if (
    status === 'warning' ||
    status === 'warn' ||
    status === 'needs_evidence' ||
    status === 'missing_evidence' ||
    status === 'satisfied_with_summary_evidence' ||
    status === 'satisfied_with_uncertainty' ||
    status === 'satisfied_with_warnings'
  ) return 'warning';
  if (status === 'skipped') return 'skipped';
  return 'unknown';
}

function selectVerificationRun(item: Record<string, any>): Record<string, any> {
  const runs = asArray(item.verification_runs).filter((run) => run && typeof run === 'object');
  return (
    runs.find((run) => run.verifier_type === 'semantic_evidence_judge') ||
    runs.find((run) => run.status === 'failed') ||
    runs.find((run) => run.status === 'warning') ||
    runs[runs.length - 1] ||
    {}
  );
}

function verifierClaims(run: Record<string, any>, field: 'missing_evidence' | 'unsupported_claims'): string[] {
  const observed = asRecord(run.observed);
  const evidence = asRecord(run.evidence);
  return unique([...asArray(observed[field]), ...asArray(evidence[field])], 4);
}

function warningMissingEvidence(item: Record<string, any>): string[] {
  return unique(
    asArray(item.verification_warnings).flatMap((warning) =>
      asArray(asRecord(warning).missing_conditions),
    ),
    4,
  );
}

function stepKeyForArtifact(artifact: Record<string, any>, fallback: string): string {
  return compactText(artifact.node_id || artifact.step_id || artifact.source || fallback, 64) || fallback;
}

function stepKeyForTool(tool: Record<string, any>, fallback: string): string {
  return compactText(tool.node_id || tool.step_id || tool.source || fallback, 64) || fallback;
}

function toolName(value: Record<string, any>): string {
  return compactText(value.tool || value.tool_name || value.command || value.current_tool || value.name, 72);
}

function buildStepEvidence(item: Record<string, any>): RunStepEvidenceDebug[] {
  const stepRows = asArray(item.run_steps);
  const stepLabels = new Map<string, string>();
  for (const step of stepRows) {
    const record = asRecord(step);
    const key = compactText(record.node_id || record.step_id || record.id || record.label || record.skill_name, 64);
    if (key) stepLabels.set(key, compactText(record.node_id || record.step_id || record.label || record.skill_name, 64));
  }

  const fallbackStep = [...stepLabels.keys()][0] || 'run';
  const stepData = new Map<
    string,
    { counts: RunEvidenceCounts; tools: string[]; label: string }
  >();

  function ensureStep(stepId: string): { counts: RunEvidenceCounts; tools: string[]; label: string } {
    const key = stepId || fallbackStep;
    const existing = stepData.get(key);
    if (existing) return existing;
    const next = { counts: { ...ZERO_COUNTS }, tools: [], label: stepLabels.get(key) || key };
    stepData.set(key, next);
    return next;
  }

  for (const stepId of stepLabels.keys()) ensureStep(stepId);

  for (const artifact of asArray(item.execution_artifacts)) {
    const record = asRecord(artifact);
    if (!Object.keys(record).length) continue;
    const step = ensureStep(stepKeyForArtifact(record, fallbackStep));
    addCounts(step.counts, evidenceCountsForArtifact(record));
    const activityTool = toolName(record);
    if (activityTool) step.tools.push(activityTool);

    const evidence = asRecord(record.evidence);
    for (const command of asArray(evidence.commands)) {
      const commandName = toolName(asRecord(command));
      if (commandName) step.tools.push(commandName);
    }
  }

  for (const tool of asArray(item.tool_calls)) {
    const record = asRecord(tool);
    const step = ensureStep(stepKeyForTool(record, fallbackStep));
    const name = toolName(record);
    if (name) step.tools.push(name);
  }

  return [...stepData.entries()]
    .map(([stepId, data]) => ({
      stepId,
      label: data.label,
      tools: unique(data.tools, 5),
      evidenceLabel: formatCounts(data.counts),
      counts: data.counts,
    }))
    .filter((step) => step.tools.length || step.evidenceLabel !== 'no evidence')
    .slice(0, 6);
}

export function buildRunEvidenceDebug(item: unknown): RunEvidenceDebug | null {
  const record = asRecord(item);
  const artifacts = asArray(record.execution_artifacts);
  const verifier = selectVerificationRun(record);
  const requirements = contractRequirements(record);
  const warnings = warningMissingEvidence(record);

  const counts = { ...ZERO_COUNTS };
  for (const artifact of artifacts) {
    addCounts(counts, evidenceCountsForArtifact(asRecord(artifact)));
  }

  const missingEvidence = unique([...verifierClaims(verifier, 'missing_evidence'), ...warnings], 5);
  const unsupportedClaims = verifierClaims(verifier, 'unsupported_claims');
  const tone = verificationTone(String(verifier.status || record.contract_status || 'unknown'));
  const contractType = compactText(record.contract_type, 40);
  const steps = buildStepEvidence(record);

  const hasDebugSurface = Boolean(
    contractType ||
      requirements.length ||
      artifacts.length ||
      asArray(record.verification_runs).length ||
      warnings.length ||
      asArray(record.tool_calls).length,
  );
  if (!hasDebugSurface) return null;

  const verifierLabel = verifier.verifier_type
    ? `${compactText(verifier.verifier_type, 48)}: ${compactText(verifier.status || 'unknown', 24)}`
    : record.contract_status
      ? `contract: ${compactText(record.contract_status, 24)}`
      : undefined;

  return {
    tone,
    defaultOpen: tone === 'failed' || missingEvidence.length > 0 || unsupportedClaims.length > 0,
    summaryLabel: formatCounts(counts),
    contractType: contractType || undefined,
    contractRequirements: requirements,
    verifierLabel,
    missingEvidence,
    unsupportedClaims,
    counts,
    steps,
  };
}
