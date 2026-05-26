import { LIVE_RUN_STATUSES } from '../constants/statuses.ts';

const ACTIVE_RUN_STATUSES: ReadonlySet<string> = new Set(LIVE_RUN_STATUSES);

type RunActivityCandidate = {
  idea_id?: string | null;
  thread_id?: string | null;
  status?: string | null;
  run_id?: string | number | null;
  id?: string | number | null;
  root_run_id?: string | number | null;
  parent_run_id?: string | number | null;
};

function normalizedRunId(run: RunActivityCandidate): string | null {
  const value = run.run_id ?? run.id;
  return value == null || value === '' ? null : String(value);
}

function isRootRun(run: RunActivityCandidate): boolean {
  if (run.parent_run_id != null && String(run.parent_run_id) !== '') return false;
  const runId = normalizedRunId(run);
  if (run.root_run_id == null || String(run.root_run_id) === '') return true;
  if (!runId) return true;
  return String(run.root_run_id) === runId;
}

export function isActiveRootRun(run: RunActivityCandidate): boolean {
  const status = String(run.status || '').toLowerCase();
  return ACTIVE_RUN_STATUSES.has(status) && isRootRun(run);
}

export function activeRootRunCountsByIdea(runs: RunActivityCandidate[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const run of runs) {
    if (!isActiveRootRun(run)) continue;
    const ideaId = run.idea_id || run.thread_id;
    if (!ideaId) continue;
    counts.set(String(ideaId), (counts.get(String(ideaId)) || 0) + 1);
  }
  return counts;
}
