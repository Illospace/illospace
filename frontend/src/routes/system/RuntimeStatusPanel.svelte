<script lang="ts">
  import { onMount } from 'svelte';

  import { ConstellationButton } from '$lib/components/constellation';

  import type { RuntimeHealthState, RuntimeStatusSnapshot } from '$lib/types/runtimeStatus';

  let {
    status,
    loading = false,
    error = '',
    onrefresh,
  }: {
    status: RuntimeStatusSnapshot | null;
    loading?: boolean;
    error?: string;
    onrefresh: () => void | Promise<void>;
  } = $props();

  let clockMs = $state(Date.now());

  onMount(() => {
    const interval = window.setInterval(() => {
      clockMs = Date.now();
    }, 10_000);
    return () => window.clearInterval(interval);
  });

  const stateLabel: Record<RuntimeHealthState, string> = {
    good: 'good',
    late: 'late',
    stalled: 'stalled',
  };

  function duration(seconds: number | null): string {
    if (seconds === null) return 'not recorded';
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86_400)}d`;
  }

  function timestamp(value: string | null): string {
    if (!value) return 'not recorded';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return 'not recorded';
    return parsed.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  }

  function snapshotAge(value: string): number | null {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return null;
    return Math.max(0, Math.floor((clockMs - parsed.getTime()) / 1000));
  }

  function money(value: number): string {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 6,
    }).format(value);
  }
</script>

<section class="runtime-status-panel" aria-labelledby="runtime-status-title">
  <header class="runtime-status-header">
    <div>
      <p class="runtime-status-kicker">Live evidence</p>
      <div class="runtime-status-heading-row">
        <h2 id="runtime-status-title">Runtime status</h2>
        {#if status}
          <span class="runtime-overall-state" data-state={error ? 'stalled' : status.overall.state}>
            {error ? 'stalled' : stateLabel[status.overall.state]}
          </span>
        {/if}
      </div>
      <p>Six signals from the running system. Container state is not used as health proof.</p>
    </div>
    <ConstellationButton variant="secondary" size="sm" onclick={onrefresh} {loading} loadingLabel="Refreshing">
      Refresh
    </ConstellationButton>
  </header>

  {#if error}
    <div class="runtime-status-message" data-state="stalled">
      <strong>{status ? 'Refresh failed. The evidence below is stale.' : 'Status evidence is unavailable.'}</strong>
      <span>{error}</span>
    </div>
  {/if}
  {#if loading && !status}
    <div class="runtime-status-message">Reading runtime evidence...</div>
  {:else if status}
    <p class="runtime-snapshot-age">
      Snapshot captured {timestamp(status.captured_at)} · {duration(snapshotAge(status.captured_at))} old
    </p>
    <div class="runtime-status-rows">
      <article class="runtime-status-row">
        <div class="runtime-status-name">
          <span>Worker claiming</span>
          <span class="runtime-row-state" data-state={status.worker.state}>{stateLabel[status.worker.state]}</span>
        </div>
        <div class="runtime-status-evidence">
          <strong>Last claim {duration(status.worker.last_claim_age_seconds)} ago</strong>
          <span>heartbeat {duration(status.worker.heartbeat_age_seconds)} ago · {status.worker.queued} queued · oldest wait {duration(status.worker.oldest_queued_age_seconds)}</span>
        </div>
        <p>{status.worker.reason}</p>
      </article>

      <article class="runtime-status-row">
        <div class="runtime-status-name">
          <span>Scheduler ticking</span>
          <span class="runtime-row-state" data-state={status.scheduler.state}>{stateLabel[status.scheduler.state]}</span>
        </div>
        <div class="runtime-status-evidence">
          <strong>Last tick {duration(status.scheduler.tick_age_seconds)} ago</strong>
          <span>{status.scheduler.overdue_jobs} overdue · max lag {duration(status.scheduler.max_lag_seconds)}</span>
        </div>
        <p>{status.scheduler.reason}</p>
      </article>

      <article class="runtime-status-row">
        <div class="runtime-status-name">
          <span>Runs</span>
          <span class="runtime-row-state" data-state={status.runs.state}>{stateLabel[status.runs.state]}</span>
        </div>
        <div class="runtime-status-evidence">
          <strong>{status.runs.queued} queued · {status.runs.running} running</strong>
          <span>oldest queue {duration(status.runs.oldest_queued_age_seconds)} · oldest active {duration(status.runs.oldest_running_age_seconds)}</span>
        </div>
        <p>{status.runs.reason}</p>
      </article>

      <article class="runtime-status-row">
        <div class="runtime-status-name">
          <span>Cycles</span>
          <span class="runtime-row-state" data-state={status.cycles.state}>{stateLabel[status.cycles.state]}</span>
        </div>
        <div class="runtime-status-evidence">
          <strong>{status.cycles.overdue} overdue · {status.cycles.enabled} enabled</strong>
          <span>last fired {timestamp(status.cycles.last_fired_at)}</span>
        </div>
        <p>{status.cycles.reason}</p>
        {#if status.cycles.items.length}
          <div class="runtime-cycle-items">
            {#each status.cycles.items as cycle (cycle.id)}
              <div class="runtime-cycle-item">
                <strong>{cycle.name}</strong>
                <span>{duration(cycle.overdue_seconds)} overdue · last fired {timestamp(cycle.last_run_at)}</span>
              </div>
            {/each}
            {#if status.cycles.items_truncated}
              <p>Showing the 20 most overdue cycles.</p>
            {/if}
          </div>
        {/if}
      </article>

      <article class="runtime-status-row">
        <div class="runtime-status-name">
          <span>Spend today</span>
          <span class="runtime-row-state" data-state={status.spend.state}>{stateLabel[status.spend.state]}</span>
        </div>
        <div class="runtime-status-evidence">
          <strong>{money(status.spend.amount_usd)}</strong>
          <span>{status.spend.runs} runs · {status.spend.tokens.toLocaleString()} tokens</span>
        </div>
        <p>{status.spend.reason}</p>
      </article>

      <article class="runtime-status-row">
        <div class="runtime-status-name">
          <span>Deployment</span>
          <span class="runtime-row-state" data-state={status.deploy.state}>{stateLabel[status.deploy.state]}</span>
        </div>
        <div class="runtime-status-evidence">
          <strong>{status.deploy.sha ? status.deploy.sha.slice(0, 12) : 'SHA not reported'}</strong>
          <span>deployed {timestamp(status.deploy.deployed_at)} · built {timestamp(status.deploy.built_at)}</span>
        </div>
        <p>{status.deploy.reason} API process started {timestamp(status.deploy.process_started_at)}.</p>
      </article>
    </div>
  {/if}
</section>

<style>
  .runtime-status-panel {
    width: 100%;
    max-width: 1540px;
    margin: 0 auto;
    border: 1px solid var(--constellation-surface-panel-border);
    background: var(--constellation-surface-panel-background);
  }

  .runtime-status-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 24px;
    padding: clamp(18px, 2.4vw, 28px);
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .runtime-status-kicker {
    margin: 0 0 7px;
    color: var(--constellation-color-text-tertiary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .runtime-status-heading-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  h2 {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-size: clamp(1.25rem, 1.8vw, 1.65rem);
    font-weight: 560;
    letter-spacing: -0.025em;
  }

  .runtime-status-header p:last-child {
    margin: 7px 0 0;
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
  }

  .runtime-overall-state,
  .runtime-row-state {
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    letter-spacing: 0.08em;
    text-transform: lowercase;
  }

  [data-state='good'] { color: var(--constellation-color-success); }
  [data-state='late'] { color: var(--constellation-color-warning); }
  [data-state='stalled'] { color: var(--constellation-color-danger); }

  .runtime-status-rows {
    display: grid;
  }

  .runtime-snapshot-age {
    margin: 0;
    padding: 10px clamp(18px, 2.4vw, 28px);
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
    color: var(--constellation-color-text-tertiary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
  }

  .runtime-status-row {
    display: grid;
    grid-template-columns: minmax(160px, 0.58fr) minmax(260px, 1fr) minmax(240px, 1.25fr);
    gap: 24px;
    align-items: center;
    min-width: 0;
    padding: 18px clamp(18px, 2.4vw, 28px);
  }

  .runtime-status-row + .runtime-status-row {
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .runtime-status-name,
  .runtime-status-evidence {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .runtime-status-name > span:first-child,
  .runtime-status-evidence strong {
    color: var(--constellation-color-text-primary);
    font-size: var(--constellation-type-body-sm);
    font-weight: 560;
  }

  .runtime-status-evidence span,
  .runtime-status-row p {
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .runtime-status-evidence span {
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
  }

  .runtime-status-row p {
    margin: 0;
  }

  .runtime-cycle-items {
    display: grid;
    grid-column: 2 / -1;
    gap: 8px;
    padding-top: 4px;
  }

  .runtime-cycle-item {
    display: flex;
    justify-content: space-between;
    gap: 18px;
    padding-top: 8px;
    border-top: 1px solid var(--constellation-surface-panel-separator);
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
  }

  .runtime-cycle-item strong {
    color: var(--constellation-color-text-primary);
    font-weight: 560;
  }

  .runtime-cycle-item span {
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    text-align: right;
  }

  .runtime-status-message {
    display: grid;
    gap: 4px;
    min-height: 110px;
    place-content: center;
    padding: 20px;
    color: var(--constellation-color-text-secondary);
    text-align: center;
  }

  @media (max-width: 880px) {
    .runtime-status-row {
      grid-template-columns: minmax(140px, 0.7fr) 1.3fr;
      gap: 12px 20px;
    }

    .runtime-status-row p {
      grid-column: 1 / -1;
    }

    .runtime-cycle-items {
      grid-column: 1 / -1;
    }
  }

  @media (max-width: 620px) {
    .runtime-status-header {
      align-items: stretch;
      flex-direction: column;
    }

    .runtime-status-row {
      grid-template-columns: 1fr;
      gap: 10px;
    }

    .runtime-status-row p {
      grid-column: auto;
    }

    .runtime-cycle-item {
      align-items: flex-start;
      flex-direction: column;
      gap: 3px;
    }

    .runtime-cycle-item span {
      text-align: left;
    }
  }
</style>
