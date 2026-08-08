export type RuntimeHealthState = 'good' | 'late' | 'stalled';

export interface RuntimeStatusSnapshot {
  captured_at: string;
  overall: { state: RuntimeHealthState; reason: string };
  worker: {
    state: RuntimeHealthState;
    heartbeat_at: string | null;
    heartbeat_age_seconds: number | null;
    last_claimed_at: string | null;
    last_claim_age_seconds: number | null;
    queued: number;
    oldest_queued_age_seconds: number | null;
    reason: string;
  };
  scheduler: {
    state: RuntimeHealthState;
    last_tick_at: string | null;
    tick_age_seconds: number | null;
    overdue_jobs: number;
    max_lag_seconds: number;
    reason: string;
  };
  runs: {
    state: RuntimeHealthState;
    queued: number;
    running: number;
    oldest_queued_age_seconds: number | null;
    oldest_running_age_seconds: number | null;
    overdue_deadlines: number;
    reason: string;
  };
  cycles: {
    state: RuntimeHealthState;
    enabled: number;
    overdue: number;
    last_fired_at: string | null;
    max_overdue_seconds: number | null;
    items: Array<{
      id: number;
      name: string;
      next_run_at: string | null;
      overdue_seconds: number;
      last_run_at: string | null;
      last_status: string | null;
    }>;
    items_truncated: boolean;
    reason: string;
  };
  spend: {
    state: RuntimeHealthState;
    amount_usd: number;
    tokens: number;
    runs: number;
    since: string;
    reason: string;
  };
  deploy: {
    state: RuntimeHealthState;
    sha: string | null;
    deployed_at: string | null;
    built_at: string | null;
    process_started_at: string | null;
    reason: string;
  };
}
