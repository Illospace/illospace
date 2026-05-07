<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import { cortex } from '$lib/stores/cortex.svelte';
  import { cancelRun as cancelRunRequest, opsActive, opsRecent } from '$lib/features/threads/api/threadApi';
  import { wsClient } from '$lib/stores/ws.svelte';
  import { onMount, onDestroy, tick, untrack } from 'svelte';

  let { visible = false, onclose }: { visible: boolean; onclose: () => void } = $props();

  // ── State ─────────────────────────────────────────────────
  let activeRuns = $state<any[]>([]);
  let recentRuns = $state<any[]>([]);
  let loading = $state(true);
  let expandedIds = $state<Set<number>>(new Set());
  let tab = $state<'live' | 'recent'>('live');
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let tickTimer: ReturnType<typeof setInterval> | null = null;
  let wsUnsubs: (() => void)[] = [];
  let now = $state(Date.now());
  let recentExpandedIds = $state<Set<number>>(new Set());

  // ── Derived ───────────────────────────────────────────────
  let runningCount = $derived(activeRuns.filter(d => d.status === 'running').length);
  let queuedCount = $derived(activeRuns.filter(d => d.status === 'queued').length);
  let totalTokens = $derived(activeRuns.reduce((s, d) => s + (d.tokens_total || 0), 0));
  let totalCost = $derived(activeRuns.reduce((s, d) => s + (d.estimated_cost || 0), 0));
  let totalToolCalls = $derived(activeRuns.reduce((s, d) => s + (d.tool_calls?.length || 0), 0));

  // ── Live clock (ticks every second for elapsed timers) ────
  function startTicking() {
    if (tickTimer) return;
    tickTimer = setInterval(() => { now = Date.now(); }, 1000);
  }
  function stopTicking() {
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
  }

  // ── Helpers ───────────────────────────────────────────────
  function liveElapsed(isoStr: string | null): string {
    if (!isoStr) return '--:--';
    const sec = Math.max(0, Math.floor((now - new Date(isoStr).getTime()) / 1000));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function timeAgo(isoStr: string | null): string {
    if (!isoStr) return '';
    const diff = now - new Date(isoStr).getTime();
    const s = Math.floor(diff / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }

  function fmtDuration(sec: number | null): string {
    if (!sec) return '—';
    if (sec < 60) return `${Math.round(sec)}s`;
    const m = Math.floor(sec / 60);
    const rs = Math.round(sec % 60);
    if (m < 60) return `${m}m ${rs}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  function traceTime(isoStr: string): string {
    try {
      return new Date(isoStr).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    } catch { return ''; }
  }

  function healthScore(d: any): { pct: number; label: string; color: string } {
    // Liveness: is the run making progress?
    const trace = d.activity_trace || [];
    if (trace.length === 0) return { pct: 50, label: 'unknown', color: '#888' };
    const lastEntry = trace[trace.length - 1];
    const lastTime = new Date(lastEntry.at).getTime();
    const staleSec = (now - lastTime) / 1000;
    if (staleSec < 30) return { pct: 100, label: 'active', color: '#34d399' };
    if (staleSec < 120) return { pct: 75, label: 'working', color: '#E8A94B' };
    if (staleSec < 300) return { pct: 40, label: 'slow', color: '#fb923c' };
    return { pct: 15, label: 'stalled', color: '#f87171' };
  }

  function runGraphProgress(d: any): { done: number; total: number; pct: number } {
    const steps = d.run_steps || [];
    if (steps.length === 0) return { done: 0, total: 0, pct: 0 };
    const done = steps.filter((p: any) => p.status === 'completed' || p.status === 'skipped').length;
    return { done, total: steps.length, pct: Math.round((done / steps.length) * 100) };
  }

  function cacheEfficiency(d: any): number | null {
    const total = (d.tokens_input || 0);
    const cached = (d.cache_read || 0);
    if (total === 0) return null;
    return Math.round((cached / total) * 100);
  }

  // ── Data loading ─────────────────────────────────────────
  // Primary: ops_update WS events push full state — no polling needed.
  // Fallback: 60s poll catches anything missed (reconnects, edge cases).
  const FALLBACK_POLL_MS = 60000;

  function applyOpsSnapshot(runs: any[]) {
    activeRuns = runs;
    if (runs.length <= 2 && expandedIds.size === 0) {
      expandedIds = new Set(runs.map((d: any) => d.id));
    }
    loading = false;
  }

  async function fetchOpsOnce() {
    try {
      const active = await opsActive();
      applyOpsSnapshot(active);
    } catch { /* silent — WS is the primary source */ }
  }

  async function loadRecent() {
    try {
      recentRuns = await opsRecent();
    } catch { /* silent */ }
  }

  function toggleExpand(id: number) {
    const next = new Set(expandedIds);
    if (next.has(id)) next.delete(id); else next.add(id);
    expandedIds = next;
  }

  function toggleRecentExpand(id: number) {
    const next = new Set(recentExpandedIds);
    if (next.has(id)) next.delete(id); else next.add(id);
    recentExpandedIds = next;
  }

  function navigateToIdea(ideaId: string) {
    cortex.selectIdea(ideaId);
    onclose();
  }

  async function cancelRun(runId: number) {
    try {
      await cancelRunRequest(runId);
      // Next ops_update WS event will refresh state; fetch once as immediate feedback
      fetchOpsOnce();
    } catch { /* silent */ }
  }

  // ── Lifecycle ─────────────────────────────────────────────
  $effect(() => {
    if (visible) {
      loading = untrack(() => activeRuns.length === 0);
      fetchOpsOnce();
      if (tab === 'recent') loadRecent();
      // Fallback poll — WS is the primary source, this catches edge cases
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      pollTimer = setInterval(() => {
        fetchOpsOnce();
        if (tab === 'recent') loadRecent();
      }, FALLBACK_POLL_MS);
      startTicking();
    } else {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      stopTicking();
    }
  });

  onMount(() => {
    wsUnsubs.push(
      // Primary: server pushes full ops state on every run change
      wsClient.on('ops_update', (msg: any) => {
        if (visible && msg.runs) applyOpsSnapshot(msg.runs);
      }),
      // Reconnect: fetch once to hydrate after WS reconnects
      wsClient.onReconnect(() => { if (visible) fetchOpsOnce(); }),
    );
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
    stopTicking();
    wsUnsubs.forEach(fn => fn());
  });
</script>

{#if visible}
  <div class="ops-backdrop" role="presentation" onclick={onclose}></div>
  <div class="ops-console">

    <!-- ═══ HEADER BAR ═══ -->
    <div class="ops-header">
      <div class="ops-header-left">
        <div class="ops-logo">
          <span class="ops-logo-diamond"></span>
          <span class="ops-logo-text">OPS</span>
        </div>

        <!-- Live stat tiles -->
        <div class="ops-stats-row">
          <div class="ops-stat-tile" class:stat-active={runningCount > 0}>
            <span class="ops-stat-value">{runningCount}</span>
            <span class="ops-stat-label">LIVE</span>
            {#if runningCount > 0}<span class="ops-stat-glow"></span>{/if}
          </div>
          <div class="ops-stat-tile" class:stat-queued={queuedCount > 0}>
            <span class="ops-stat-value">{queuedCount}</span>
            <span class="ops-stat-label">QUEUE</span>
          </div>
          <div class="ops-stat-tile">
            <span class="ops-stat-value">{totalToolCalls}</span>
            <span class="ops-stat-label">CALLS</span>
          </div>
          <div class="ops-stat-tile">
            <span class="ops-stat-value">{totalTokens > 999 ? `${(totalTokens / 1000).toFixed(1)}k` : totalTokens}</span>
            <span class="ops-stat-label">TOKENS</span>
          </div>
          <div class="ops-stat-tile">
            <span class="ops-stat-value">${totalCost.toFixed(3)}</span>
            <span class="ops-stat-label">COST</span>
          </div>
        </div>
      </div>

      <div class="ops-header-right">
        <div class="ops-tabs">
          <button class="ops-tab" class:active={tab === 'live'} onclick={() => { tab = 'live'; }}>
            {#if runningCount > 0}<span class="tab-live-dot"></span>{/if}
            Live
          </button>
          <button class="ops-tab" class:active={tab === 'recent'} onclick={() => { tab = 'recent'; if (recentRuns.length === 0) loadRecent(); }}>
            Recent
          </button>
        </div>
        <kbd class="ops-shortcut">O</kbd>
        <button class="ops-close" onclick={onclose} title="Close (Esc)">
          <ConstellationIcon name="close" size={16} stroke={2} />
        </button>
      </div>
    </div>

    <!-- ═══ BODY ═══ -->
    <div class="ops-body">

      {#if loading && activeRuns.length === 0 && tab === 'live'}
        <div class="ops-empty">
          <div class="ops-empty-scanner"></div>
          <span>Scanning runs...</span>
        </div>

      {:else if tab === 'live'}
        {#if activeRuns.length === 0}
          <div class="ops-empty">
            <div class="ops-idle-indicator">
              <div class="ops-idle-ring"></div>
              <span class="ops-idle-label">IDLE</span>
            </div>
            <span class="ops-idle-text">No active runs. All systems quiet.</span>
          </div>
        {:else}
          {#each activeRuns as d, i (d.id)}
            {@const expanded = expandedIds.has(d.id)}
            {@const health = healthScore(d)}
            {@const progress = runGraphProgress(d)}
            {@const cache = cacheEfficiency(d)}
            {@const isRunning = d.status === 'running'}

            <div
              class="ops-lane"
              class:lane-running={isRunning}
              class:lane-queued={d.status === 'queued'}
              class:lane-expanded={expanded}
              style="animation-delay: {i * 60}ms"
            >
              <!-- ── Lane header ── -->
              <button class="lane-header" onclick={() => toggleExpand(d.id)}>
                <!-- Status indicator -->
                <div class="lane-status-col">
                  {#if isRunning}
                    <div class="lane-pulse-ring">
                      <div class="lane-pulse-core"></div>
                    </div>
                  {:else}
                    <div class="lane-queued-dot"></div>
                  {/if}
                </div>

                <!-- Info -->
                <div class="lane-info">
                  <div class="lane-title-row">
                    <!-- svelte-ignore a11y_no_static_element_interactions -->
                    <span
                      class="lane-idea-link"
                      role="link"
                      tabindex="0"
                      onclick={(e: MouseEvent) => { e.stopPropagation(); navigateToIdea(d.idea_id); }}
                      onkeydown={(e: KeyboardEvent) => { if (e.key === 'Enter') { e.stopPropagation(); navigateToIdea(d.idea_id); } }}
                      title="Open in Cortex"
                    >
                      {d.idea_title || `#${d.id}`}
                    </span>
                    {#if d.cognitive_misses?.length}
                      <span class="lane-flag lane-flag-miss" title="Cognitive misses: {d.cognitive_misses.join(', ')}">
                        {d.cognitive_misses.length} miss{d.cognitive_misses.length > 1 ? 'es' : ''}
                      </span>
                    {/if}
                  </div>
                  <div class="lane-badges">
                    {#if d.skill_used}<span class="badge badge-skill">{d.skill_used}</span>{/if}
                    {#if d.model_used}<span class="badge badge-model">{d.model_used}</span>{/if}
                    {#if d.thinking_used}<span class="badge badge-think">think:{d.thinking_used}</span>{/if}
                    {#if d.brain_context_loaded}
                      <span class="badge badge-brain" title="{d.preloaded_memory_count} memories loaded">
                        brain:{d.preloaded_memory_count}
                      </span>
                    {/if}
                    {#if d.workers_used?.length}
                      <span class="badge badge-workers">{d.workers_used.length} worker{d.workers_used.length > 1 ? 's' : ''}</span>
                    {/if}
                  </div>
                </div>

                <!-- Health + progress -->
                <div class="lane-gauges">
                  {#if progress.total > 0}
                    <div class="lane-progress" title="{progress.done}/{progress.total} steps">
                      <div class="lane-progress-bar">
                        <div class="lane-progress-fill" style="width: {progress.pct}%"></div>
                      </div>
                      <span class="lane-progress-text">{progress.done}/{progress.total}</span>
                    </div>
                  {/if}
                  {#if isRunning}
                    <div class="lane-health" title="Health: {health.label}">
                      <div class="lane-health-bar">
                        <div class="lane-health-fill" style="width: {health.pct}%; background: {health.color}"></div>
                      </div>
                      <span class="lane-health-label" style="color: {health.color}">{health.label}</span>
                    </div>
                  {/if}
                </div>

                <!-- Elapsed + cost -->
                <div class="lane-metrics">
                  <div class="lane-elapsed" class:elapsed-long={now - new Date(d.started_at || d.created_at).getTime() > 600000}>
                    {liveElapsed(d.started_at || d.created_at)}
                  </div>
                  {#if d.estimated_cost}
                    <div class="lane-cost">${d.estimated_cost.toFixed(3)}</div>
                  {/if}
                </div>

                <!-- Expand chevron -->
                <div class="lane-chevron" class:rotated={expanded}>
                  <ConstellationIcon name="chevron-down" size={14} stroke={2} />
                </div>
              </button>

              <!-- ── Live activity line (collapsed) ── -->
              {#if d.last_activity && !expanded}
                <div class="lane-live-line">
                  <span class="live-caret"></span>
                  <span class="live-text">{d.last_activity}</span>
                </div>
              {/if}

              <!-- ── Run graph bar (collapsed) ── -->
              {#if d.run_steps?.length && !expanded}
                <div class="lane-run-graph-bar">
                  {#each d.run_steps as step}
                    <div
                      class="run-step-seg seg-{step.status}"
                      title="{step.node_id}: {step.skill_name} — {step.status}"
                      style="flex: 1"
                    ></div>
                  {/each}
                </div>
              {/if}

              <!-- ══ EXPANDED DETAIL ══ -->
              {#if expanded}
                <div class="lane-detail">
                  <div class="detail-grid">

                    <!-- Left column: run graph + trace -->
                    <div class="detail-left">
                      {#if d.run_steps?.length}
                        <div class="detail-section">
                          <div class="detail-label">RUN GRAPH</div>
                          <div class="run-step-rows">
                            {#each d.run_steps as step}
                              <div class="run-step-row row-{step.status}">
                                <span class="run-step-icon">
                                  {#if step.status === 'completed'}<span class="icon-check">&#10003;</span>
                                  {:else if step.status === 'running'}<span class="icon-running"></span>
                                  {:else if step.status === 'failed'}<span class="icon-fail">&#10007;</span>
                                  {:else if step.status === 'skipped'}<span class="icon-skip">—</span>
                                  {:else}<span class="icon-pending">&#9675;</span>
                                  {/if}
                                </span>
                                <span class="run-step-id">{step.node_id}</span>
                                <span class="run-step-skill">{step.skill_name}</span>
                                {#if step.task}
                                  <span class="run-step-task" title={step.task}>{step.task.substring(0, 80)}{step.task.length > 80 ? '...' : ''}</span>
                                {/if}
                                <span class="run-step-duration">
                                  {#if step.status === 'running'}
                                    {liveElapsed(step.started_at)}
                                  {:else if step.duration_sec}
                                    {fmtDuration(step.duration_sec)}
                                  {/if}
                                </span>
                                {#if step.error}
                                  <div class="run-step-error">{step.error.substring(0, 120)}</div>
                                {/if}
                              </div>
                            {/each}
                          </div>
                        </div>
                      {/if}

                      <!-- Activity trace -->
                      {#if d.activity_trace?.length}
                        <div class="detail-section">
                          <div class="detail-label">ACTIVITY TRACE <span class="detail-count">{d.activity_trace.length}</span></div>
                          <div class="trace-scroll">
                            {#each d.activity_trace as entry, idx}
                              <div class="trace-line" class:trace-latest={idx === d.activity_trace.length - 1}>
                                <span class="trace-time">{traceTime(entry.at)}</span>
                                <span class="trace-text">{entry.activity}</span>
                              </div>
                            {/each}
                          </div>
                        </div>
                      {/if}
                    </div>

                    <!-- Right column: Tool calls + Metrics + Actions -->
                    <div class="detail-right">
                      <!-- Resource meters -->
                      <div class="detail-section">
                        <div class="detail-label">RESOURCES</div>
                        <div class="meter-grid">
                          <div class="meter">
                            <div class="meter-bar">
                              <div class="meter-fill meter-fill-in" style="width: {d.tokens_input ? Math.min(100, (d.tokens_input / Math.max(d.tokens_total || 1, 1)) * 100) : 0}%"></div>
                            </div>
                            <div class="meter-info">
                              <span class="meter-label">INPUT</span>
                              <span class="meter-value">{(d.tokens_input || 0).toLocaleString()}</span>
                            </div>
                          </div>
                          <div class="meter">
                            <div class="meter-bar">
                              <div class="meter-fill meter-fill-out" style="width: {d.tokens_output ? Math.min(100, (d.tokens_output / Math.max(d.tokens_total || 1, 1)) * 100) : 0}%"></div>
                            </div>
                            <div class="meter-info">
                              <span class="meter-label">OUTPUT</span>
                              <span class="meter-value">{(d.tokens_output || 0).toLocaleString()}</span>
                            </div>
                          </div>
                          {#if cache !== null}
                            <div class="meter">
                              <div class="meter-bar">
                                <div class="meter-fill meter-fill-cache" style="width: {cache}%"></div>
                              </div>
                              <div class="meter-info">
                                <span class="meter-label">CACHE HIT</span>
                                <span class="meter-value">{cache}%</span>
                              </div>
                            </div>
                          {/if}
                        </div>
                      </div>

                      <!-- Tool calls -->
                      {#if d.tool_calls?.length}
                        <div class="detail-section">
                          <div class="detail-label">TOOL CALLS <span class="detail-count">{d.tool_calls.length}</span></div>
                          <div class="tools-scroll">
                            {#each d.tool_calls as tc}
                              <div class="tool-line">
                                <span class="tool-time">{traceTime(tc.called_at)}</span>
                                <span class="tool-name">{tc.tool_name}</span>
                                {#if tc.args_snippet}
                                  <span class="tool-args">{tc.args_snippet}</span>
                                {/if}
                              </div>
                            {/each}
                          </div>
                        </div>
                      {/if}

                      <!-- Cognitive misses -->
                      {#if d.cognitive_misses?.length}
                        <div class="detail-section">
                          <div class="detail-label label-warning">COGNITIVE MISSES</div>
                          <div class="misses">
                            {#each d.cognitive_misses as miss}
                              <div class="miss-item">{miss}</div>
                            {/each}
                          </div>
                        </div>
                      {/if}

                      <!-- Adaptations -->
                      {#if d.adaptations?.length}
                        <div class="detail-section">
                          <div class="detail-label">ADAPTATIONS</div>
                          <div class="adaptations">
                            {#each d.adaptations as adpt}
                              <div class="adapt-item">
                                <span class="adapt-trigger">{adpt.trigger || '?'}</span>
                                <span class="adapt-arrow">&rarr;</span>
                                <span class="adapt-action">{adpt.action_taken || '?'}</span>
                              </div>
                            {/each}
                          </div>
                        </div>
                      {/if}

                      <!-- Actions -->
                      <div class="detail-actions">
                        <button class="action-btn action-cancel" onclick={(e: MouseEvent) => { e.stopPropagation(); cancelRun(d.id); }} title="Cancel this run">
                          <ConstellationIcon name="stop" size={12} />
                          Stop
                        </button>
                        <button class="action-btn action-view" onclick={(e: MouseEvent) => { e.stopPropagation(); navigateToIdea(d.idea_id); }}>
                          Open Idea
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              {/if}
            </div>
          {/each}
        {/if}

      {:else if tab === 'recent'}
        {#if recentRuns.length === 0}
          <div class="ops-empty">Loading recent runs...</div>
        {:else}
          {#each recentRuns as d, i (d.id)}
            {@const expanded = recentExpandedIds.has(d.id)}
            {@const hasMisses = d.cognitive_misses?.length > 0}
            {@const hasPostmortem = d.postmortem && Object.keys(d.postmortem).length > 0}
            {@const hasError = !!d.error}
            {@const cache = cacheEfficiency(d)}

            <div
              class="ops-lane lane-recent"
              class:lane-completed={d.status === 'completed'}
              class:lane-failed={d.status === 'failed'}
              class:lane-expanded={expanded}
              style="animation-delay: {i * 40}ms"
            >
              <button class="lane-header" onclick={() => toggleRecentExpand(d.id)}>
                <div class="lane-status-col">
                  {#if d.status === 'completed'}
                    <div class="lane-done-dot"></div>
                  {:else}
                    <div class="lane-fail-dot"></div>
                  {/if}
                </div>

                <div class="lane-info">
                  <div class="lane-title-row">
                    <!-- svelte-ignore a11y_no_static_element_interactions -->
                    <span
                      class="lane-idea-link"
                      role="link"
                      tabindex="0"
                      onclick={(e: MouseEvent) => { e.stopPropagation(); if (d.idea_id) navigateToIdea(d.idea_id); }}
                      onkeydown={(e: KeyboardEvent) => { if (e.key === 'Enter') { e.stopPropagation(); if (d.idea_id) navigateToIdea(d.idea_id); } }}
                    >
                      {d.idea_title || `#${d.id}`}
                    </span>
                    {#if d.skill_outcome}
                      <span class="lane-flag" class:lane-flag-good={d.skill_outcome === 'good'} class:lane-flag-bad={d.skill_outcome === 'bad'}>
                        {d.skill_outcome}
                      </span>
                    {/if}
                    {#if hasMisses}
                      <span class="lane-flag lane-flag-miss">{d.cognitive_misses.length} miss</span>
                    {/if}
                    {#if hasPostmortem}
                      <span class="lane-flag lane-flag-postmortem">postmortem</span>
                    {/if}
                  </div>
                  <div class="lane-badges">
                    {#if d.skill_used}<span class="badge badge-skill">{d.skill_used}</span>{/if}
                    {#if d.model_used}<span class="badge badge-model">{d.model_used}</span>{/if}
                    {#if d.duration_sec}<span class="badge badge-duration">{fmtDuration(d.duration_sec)}</span>{/if}
                    {#if d.tokens_total}<span class="badge badge-tokens">{d.tokens_total > 999 ? `${(d.tokens_total / 1000).toFixed(1)}k` : d.tokens_total} tok</span>{/if}
                    {#if d.estimated_cost}<span class="badge badge-cost">${d.estimated_cost.toFixed(3)}</span>{/if}
                    {#if cache !== null}<span class="badge badge-cache">{cache}% cache</span>{/if}
                  </div>
                </div>

                <div class="lane-metrics">
                  <div class="lane-timestamp">{timeAgo(d.completed_at)}</div>
                </div>

                <div class="lane-chevron" class:rotated={expanded}>
                  <ConstellationIcon name="chevron-down" size={14} stroke={2} />
                </div>
              </button>

              {#if hasError && !expanded}
                <div class="lane-error-line">{d.error.substring(0, 150)}</div>
              {/if}

              {#if expanded}
                <div class="lane-detail">
                  {#if hasError}
                    <div class="detail-section">
                      <div class="detail-label label-error">ERROR</div>
                      <div class="error-block">{d.error}</div>
                    </div>
                  {/if}

                  {#if d.error_classification}
                    <div class="detail-section">
                      <div class="detail-label label-warning">ERROR CLASSIFICATION</div>
                      <div class="classification-block">
                        {#if d.error_classification.category}
                          <span class="class-badge">{d.error_classification.category}</span>
                        {/if}
                        {#if d.error_classification.is_retryable !== undefined}
                          <span class="class-badge" class:class-retry={d.error_classification.is_retryable} class:class-no-retry={!d.error_classification.is_retryable}>
                            {d.error_classification.is_retryable ? 'retryable' : 'not retryable'}
                          </span>
                        {/if}
                        {#if d.error_classification.suggestion}
                          <div class="class-suggestion">{d.error_classification.suggestion}</div>
                        {/if}
                      </div>
                    </div>
                  {/if}

                  {#if hasPostmortem}
                    <div class="detail-section">
                      <div class="detail-label">POSTMORTEM</div>
                      <div class="postmortem-block">
                        {#each Object.entries(d.postmortem) as [key, val]}
                          <div class="pm-row">
                            <span class="pm-key">{key}</span>
                            <span class="pm-val">{typeof val === 'object' ? JSON.stringify(val) : val}</span>
                          </div>
                        {/each}
                      </div>
                    </div>
                  {/if}

                  {#if d.run_steps?.length}
                    <div class="detail-section">
                      <div class="detail-label">PIPELINE</div>
                      <div class="run-step-rows">
                        {#each d.run_steps as step}
                          <div class="run-step-row row-{step.status}">
                            <span class="run-step-icon">
                              {#if step.status === 'completed'}<span class="icon-check">&#10003;</span>
                              {:else if step.status === 'failed'}<span class="icon-fail">&#10007;</span>
                              {:else if step.status === 'skipped'}<span class="icon-skip">—</span>
                              {:else}<span class="icon-pending">&#9675;</span>
                              {/if}
                            </span>
                            <span class="run-step-id">{step.node_id}</span>
                            <span class="run-step-skill">{step.skill_name}</span>
                            <span class="run-step-duration">{fmtDuration(step.duration_sec)}</span>
                            {#if step.error}
                              <div class="run-step-error">{step.error.substring(0, 100)}</div>
                            {/if}
                          </div>
                        {/each}
                      </div>
                    </div>
                  {/if}

                  {#if d.activity_trace?.length}
                    <div class="detail-section">
                      <div class="detail-label">TRACE <span class="detail-count">{d.activity_trace.length}</span></div>
                      <div class="trace-scroll">
                        {#each d.activity_trace as entry, idx}
                          <div class="trace-line">
                            <span class="trace-time">{traceTime(entry.at)}</span>
                            <span class="trace-text">{entry.activity}</span>
                          </div>
                        {/each}
                      </div>
                    </div>
                  {/if}

                  {#if d.cognitive_misses?.length}
                    <div class="detail-section">
                      <div class="detail-label label-warning">COGNITIVE MISSES</div>
                      <div class="misses">
                        {#each d.cognitive_misses as miss}
                          <div class="miss-item">{miss}</div>
                        {/each}
                      </div>
                    </div>
                  {/if}
                </div>
              {/if}
            </div>
          {/each}
        {/if}
      {/if}
    </div>
  </div>
{/if}

<style>
  /* ══════════════════════════════════════════════════════════
     OPS CONSOLE — Mission Control for Cortex Runs
     ══════════════════════════════════════════════════════════ */

  .ops-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.3);
    backdrop-filter: blur(3px);
    z-index: 149;
  }

  .ops-console {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    max-height: 60vh;
    min-height: 200px;
    background: #060810;
    border-top: 1px solid rgba(200, 175, 100, 0.12);
    z-index: 150;
    display: flex;
    flex-direction: column;
    font-family: var(--font-mono);
    animation: ops-enter 0.3s cubic-bezier(0.22, 1, 0.36, 1);
    box-shadow: 0 -12px 60px rgba(0, 0, 0, 0.7), 0 -2px 20px rgba(0, 0, 0, 0.4);
    /* Subtle scan-line texture */
    background-image:
      repeating-linear-gradient(
        0deg,
        transparent,
        transparent 2px,
        rgba(200, 175, 100, 0.008) 2px,
        rgba(200, 175, 100, 0.008) 4px
      );
  }

  @keyframes ops-enter {
    from { transform: translateY(100%); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }

  /* ── HEADER ───────────────────────────────────────────────── */

  .ops-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 14px;
    border-bottom: 1px solid rgba(200, 175, 100, 0.06);
    flex-shrink: 0;
    background: rgba(200, 175, 100, 0.015);
  }

  .ops-header-left, .ops-header-right {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .ops-logo {
    display: flex;
    align-items: center;
    gap: 6px;
    user-select: none;
  }

  .ops-logo-diamond {
    width: 7px;
    height: 7px;
    background: rgba(200, 175, 100, 0.6);
    transform: rotate(45deg);
    border-radius: 1px;
    box-shadow: 0 0 8px rgba(200, 175, 100, 0.2);
  }

  .ops-logo-text {
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0.22em;
    color: rgba(200, 175, 100, 0.6);
  }

  /* ── Stat tiles ── */

  .ops-stats-row {
    display: flex;
    gap: 3px;
  }

  .ops-stat-tile {
    position: relative;
    padding: 3px 10px;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 4px;
    text-align: center;
    min-width: 48px;
    overflow: hidden;
    transition: all 0.2s;
  }

  .ops-stat-tile.stat-active {
    border-color: rgba(232, 169, 75, 0.2);
    background: rgba(232, 169, 75, 0.04);
  }

  .ops-stat-tile.stat-queued {
    border-color: rgba(140, 160, 200, 0.15);
  }

  .ops-stat-value {
    display: block;
    font-size: 13px;
    font-weight: 700;
    color: rgba(255, 255, 255, 0.75);
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
  }

  .stat-active .ops-stat-value { color: #E8A94B; }

  .ops-stat-label {
    display: block;
    font-size: 7px;
    font-weight: 600;
    letter-spacing: 0.12em;
    color: rgba(255, 255, 255, 0.2);
    margin-top: 1px;
  }

  .ops-stat-glow {
    position: absolute;
    inset: 0;
    background: radial-gradient(circle at center, rgba(232, 169, 75, 0.08), transparent 70%);
    animation: stat-glow-pulse 3s ease-in-out infinite;
    pointer-events: none;
  }

  @keyframes stat-glow-pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 1; }
  }

  /* ── Tabs ── */

  .ops-tabs {
    display: flex;
    gap: 1px;
    background: rgba(255, 255, 255, 0.03);
    border-radius: 5px;
    padding: 2px;
  }

  .ops-tab {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.05em;
    padding: 4px 12px;
    border: none;
    background: transparent;
    color: rgba(255, 255, 255, 0.25);
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .ops-tab:hover { color: rgba(255, 255, 255, 0.45); }
  .ops-tab.active {
    background: rgba(200, 175, 100, 0.1);
    color: rgba(200, 175, 100, 0.85);
  }

  .tab-live-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #E8A94B;
    box-shadow: 0 0 6px rgba(232, 169, 75, 0.5);
    animation: stat-glow-pulse 2s ease-in-out infinite;
  }

  .ops-shortcut {
    font-family: var(--font-mono);
    font-size: 9px;
    padding: 2px 5px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 3px;
    color: rgba(255, 255, 255, 0.2);
  }

  .ops-close {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 26px;
    height: 26px;
    border: 1px solid rgba(255, 255, 255, 0.06);
    background: rgba(255, 255, 255, 0.02);
    color: rgba(255, 255, 255, 0.2);
    cursor: pointer;
    border-radius: 5px;
    transition: all 0.15s;
  }
  .ops-close:hover {
    background: rgba(248, 113, 113, 0.08);
    border-color: rgba(248, 113, 113, 0.2);
    color: rgba(248, 113, 113, 0.7);
  }

  /* ── BODY ─────────────────────────────────────────────────── */

  .ops-body {
    flex: 1;
    overflow-y: auto;
    padding: 8px 12px 12px;
    scrollbar-width: thin;
    scrollbar-color: rgba(200, 175, 100, 0.12) transparent;
  }

  /* ── Empty states ── */

  .ops-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 40px 16px;
    color: rgba(255, 255, 255, 0.15);
    font-size: 11px;
    letter-spacing: 0.04em;
  }

  .ops-empty-scanner {
    width: 40px;
    height: 2px;
    background: linear-gradient(90deg, transparent, rgba(200, 175, 100, 0.4), transparent);
    border-radius: 1px;
    animation: scanner-sweep 1.2s ease-in-out infinite;
  }
  @keyframes scanner-sweep {
    0%, 100% { transform: scaleX(0.3); opacity: 0.3; }
    50% { transform: scaleX(1); opacity: 1; }
  }

  .ops-idle-indicator {
    position: relative;
    width: 44px;
    height: 44px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .ops-idle-ring {
    position: absolute;
    inset: 0;
    border: 1px solid rgba(100, 120, 100, 0.2);
    border-radius: 50%;
    animation: idle-breathe 4s ease-in-out infinite;
  }
  @keyframes idle-breathe {
    0%, 100% { transform: scale(0.9); opacity: 0.3; }
    50% { transform: scale(1.1); opacity: 0.6; }
  }

  .ops-idle-label {
    font-size: 8px;
    font-weight: 800;
    letter-spacing: 0.15em;
    color: rgba(100, 120, 100, 0.4);
  }

  .ops-idle-text {
    color: rgba(255, 255, 255, 0.12);
  }

  /* ── LANE (run row) ──────────────────────────────────── */

  .ops-lane {
    border: 1px solid rgba(255, 255, 255, 0.03);
    border-radius: 8px;
    margin-bottom: 6px;
    background: rgba(255, 255, 255, 0.01);
    overflow: hidden;
    animation: lane-enter 0.35s cubic-bezier(0.22, 1, 0.36, 1) both;
    transition: border-color 0.2s, background 0.2s, box-shadow 0.3s;
  }

  @keyframes lane-enter {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .ops-lane.lane-running {
    border-color: rgba(232, 169, 75, 0.12);
    background: rgba(232, 169, 75, 0.015);
  }

  .ops-lane.lane-queued {
    border-color: rgba(140, 160, 200, 0.08);
    opacity: 0.65;
  }

  .ops-lane.lane-expanded {
    border-color: rgba(200, 175, 100, 0.18);
    background: rgba(200, 175, 100, 0.02);
    box-shadow: 0 2px 20px rgba(0, 0, 0, 0.3), inset 0 1px 0 rgba(200, 175, 100, 0.04);
  }

  .ops-lane.lane-completed { border-color: rgba(52, 211, 153, 0.08); }
  .ops-lane.lane-failed { border-color: rgba(248, 113, 113, 0.1); }

  /* ── Lane header ── */

  .lane-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 12px;
    width: 100%;
    border: none;
    background: transparent;
    cursor: pointer;
    text-align: left;
    font-family: var(--font-mono);
    color: var(--text-1);
    transition: background 0.15s;
  }
  .lane-header:hover { background: rgba(255, 255, 255, 0.015); }

  .lane-status-col {
    flex-shrink: 0;
    width: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  /* Pulse ring animation for running runs */
  .lane-pulse-ring {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 1.5px solid rgba(232, 169, 75, 0.25);
    display: flex;
    align-items: center;
    justify-content: center;
    animation: pulse-ring 2.5s ease-in-out infinite;
  }

  .lane-pulse-core {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #E8A94B;
    box-shadow: 0 0 8px rgba(232, 169, 75, 0.5);
    animation: pulse-core 2.5s ease-in-out infinite;
  }

  @keyframes pulse-ring {
    0%, 100% { border-color: rgba(232, 169, 75, 0.15); transform: scale(0.95); }
    50% { border-color: rgba(232, 169, 75, 0.35); transform: scale(1.1); }
  }
  @keyframes pulse-core {
    0%, 100% { opacity: 0.7; }
    50% { opacity: 1; }
  }

  .lane-queued-dot { width: 6px; height: 6px; border-radius: 50%; background: rgba(140, 160, 200, 0.4); }
  .lane-done-dot { width: 6px; height: 6px; border-radius: 50%; background: #34d399; box-shadow: 0 0 4px rgba(52, 211, 153, 0.3); }
  .lane-fail-dot { width: 6px; height: 6px; border-radius: 50%; background: #f87171; box-shadow: 0 0 4px rgba(248, 113, 113, 0.3); }

  .lane-info { flex: 1; min-width: 0; }

  .lane-title-row {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }

  .lane-idea-link {
    border: none;
    background: transparent;
    color: rgba(220, 200, 140, 0.85);
    cursor: pointer;
    font-family: var(--font-sans);
    font-size: 12px;
    font-weight: 600;
    padding: 0;
    transition: color 0.15s;
    text-align: left;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 300px;
  }
  .lane-idea-link:hover { color: #E8D08C; text-decoration: underline; }

  .lane-flag {
    font-size: 8px;
    font-weight: 600;
    letter-spacing: 0.06em;
    padding: 1px 5px;
    border-radius: 3px;
    text-transform: uppercase;
  }
  .lane-flag-miss { background: rgba(251, 191, 36, 0.12); color: rgba(251, 191, 36, 0.7); }
  .lane-flag-good { background: rgba(52, 211, 153, 0.1); color: rgba(52, 211, 153, 0.7); }
  .lane-flag-bad { background: rgba(248, 113, 113, 0.1); color: rgba(248, 113, 113, 0.7); }
  .lane-flag-postmortem { background: rgba(167, 139, 250, 0.1); color: rgba(167, 139, 250, 0.7); }

  /* ── Badges ── */

  .lane-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 3px;
    margin-top: 3px;
  }

  .badge {
    display: inline-block;
    font-size: 9px;
    font-weight: 500;
    letter-spacing: 0.02em;
    padding: 1px 6px;
    border-radius: 3px;
    font-family: var(--font-mono);
  }

  .badge-skill { background: rgba(99, 102, 241, 0.15); color: rgba(165, 160, 255, 0.75); }
  .badge-model { background: rgba(8, 145, 178, 0.15); color: rgba(100, 200, 230, 0.75); }
  .badge-think { background: rgba(139, 92, 246, 0.12); color: rgba(180, 160, 255, 0.65); }
  .badge-brain { background: rgba(244, 114, 182, 0.1); color: rgba(244, 114, 182, 0.65); }
  .badge-workers { background: rgba(34, 211, 238, 0.1); color: rgba(34, 211, 238, 0.65); }
  .badge-duration { background: rgba(255, 255, 255, 0.04); color: rgba(255, 255, 255, 0.35); }
  .badge-tokens { background: rgba(255, 255, 255, 0.03); color: rgba(255, 255, 255, 0.25); }
  .badge-cost { background: rgba(200, 175, 100, 0.08); color: rgba(200, 175, 100, 0.5); }
  .badge-cache { background: rgba(52, 211, 153, 0.08); color: rgba(52, 211, 153, 0.5); }

  /* ── Gauges (health + progress) ── */

  .lane-gauges {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex-shrink: 0;
    width: 100px;
  }

  .lane-progress, .lane-health {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .lane-progress-bar, .lane-health-bar {
    flex: 1;
    height: 3px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 2px;
    overflow: hidden;
  }

  .lane-progress-fill {
    height: 100%;
    background: linear-gradient(90deg, rgba(52, 211, 153, 0.5), rgba(52, 211, 153, 0.8));
    border-radius: 2px;
    transition: width 0.5s ease;
  }

  .lane-health-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.5s ease, background 0.5s ease;
  }

  .lane-progress-text {
    font-size: 9px;
    color: rgba(52, 211, 153, 0.6);
    font-variant-numeric: tabular-nums;
    min-width: 20px;
  }

  .lane-health-label {
    font-size: 8px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    min-width: 36px;
  }

  /* ── Elapsed + cost ── */

  .lane-metrics {
    flex-shrink: 0;
    text-align: right;
    min-width: 56px;
  }

  .lane-elapsed {
    font-size: 13px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: rgba(200, 175, 100, 0.7);
    letter-spacing: -0.02em;
  }

  .lane-elapsed.elapsed-long {
    color: rgba(248, 113, 113, 0.7);
  }

  .lane-cost {
    font-size: 9px;
    color: rgba(255, 255, 255, 0.2);
    font-variant-numeric: tabular-nums;
  }

  .lane-timestamp {
    font-size: 10px;
    color: rgba(255, 255, 255, 0.2);
  }

  .lane-chevron {
    flex-shrink: 0;
    color: rgba(255, 255, 255, 0.15);
    transition: transform 0.2s ease, color 0.15s;
  }
  .lane-chevron.rotated { transform: rotate(180deg); color: rgba(200, 175, 100, 0.5); }

  /* ── Live activity line (collapsed) ── */

  .lane-live-line {
    padding: 0 12px 6px 42px;
    font-size: 10px;
    color: rgba(200, 175, 100, 0.4);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .live-caret {
    color: rgba(232, 169, 75, 0.5);
    margin-right: 4px;
    animation: caret-blink 1s step-end infinite;
  }
  @keyframes caret-blink {
    50% { opacity: 0; }
  }

  .live-text { letter-spacing: 0.01em; }

  /* ── Run graph bar (collapsed) ── */

  .lane-run-graph-bar {
    display: flex;
    gap: 2px;
    padding: 0 12px 8px 42px;
  }

  .run-step-seg {
    height: 3px;
    border-radius: 1.5px;
    background: rgba(255, 255, 255, 0.05);
    transition: background 0.3s;
  }

  .run-step-seg.seg-completed { background: rgba(52, 211, 153, 0.55); }
  .run-step-seg.seg-running {
    background: rgba(232, 169, 75, 0.4);
    animation: seg-pulse 1.5s ease-in-out infinite;
  }
  .run-step-seg.seg-failed { background: rgba(248, 113, 113, 0.5); }
  .run-step-seg.seg-skipped { background: rgba(255, 255, 255, 0.03); }

  @keyframes seg-pulse {
    0%, 100% { background: rgba(232, 169, 75, 0.25); }
    50% { background: rgba(232, 169, 75, 0.55); }
  }

  /* ── Error line (collapsed) ── */

  .lane-error-line {
    padding: 2px 12px 6px 42px;
    font-size: 10px;
    color: rgba(248, 113, 113, 0.55);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* ══ EXPANDED DETAIL ════════════════════════════════════════ */

  .lane-detail {
    padding: 8px 12px 12px;
    border-top: 1px solid rgba(200, 175, 100, 0.05);
    animation: detail-enter 0.25s ease;
  }
  @keyframes detail-enter {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .detail-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  @media (max-width: 900px) {
    .detail-grid { grid-template-columns: 1fr; }
  }

  .detail-section { margin-bottom: 10px; }

  .detail-label {
    font-size: 8px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: rgba(200, 175, 100, 0.35);
    margin-bottom: 5px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .detail-label.label-warning { color: rgba(251, 191, 36, 0.5); }
  .detail-label.label-error { color: rgba(248, 113, 113, 0.5); }

  .detail-count {
    font-weight: 500;
    color: rgba(255, 255, 255, 0.2);
    font-size: 9px;
  }

  /* ── Run step rows ── */

  .run-step-rows { display: flex; flex-direction: column; gap: 1px; }

  .run-step-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 8px;
    border-radius: 4px;
    font-size: 10px;
    transition: background 0.15s;
  }
  .run-step-row:hover { background: rgba(255, 255, 255, 0.02); }

  .run-step-row.row-completed { background: rgba(52, 211, 153, 0.03); }
  .run-step-row.row-running { background: rgba(232, 169, 75, 0.04); }
  .run-step-row.row-failed { background: rgba(248, 113, 113, 0.03); }

  .run-step-icon { width: 14px; text-align: center; flex-shrink: 0; }
  .icon-check { color: #34d399; }
  .icon-fail { color: #f87171; }
  .icon-skip { color: rgba(255, 255, 255, 0.2); }
  .icon-pending { color: rgba(255, 255, 255, 0.15); }

  .icon-running {
    display: inline-block;
    width: 8px;
    height: 8px;
    border: 1.5px solid rgba(232, 169, 75, 0.4);
    border-top-color: #E8A94B;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .run-step-id { color: rgba(255, 255, 255, 0.45); flex-shrink: 0; min-width: 60px; }
  .run-step-skill { color: rgba(165, 160, 255, 0.55); flex-shrink: 0; }
  .run-step-task {
    color: rgba(255, 255, 255, 0.2);
    flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .run-step-duration {
    color: rgba(255, 255, 255, 0.3);
    flex-shrink: 0; font-variant-numeric: tabular-nums; min-width: 44px; text-align: right;
  }
  .run-step-error {
    width: 100%;
    padding: 2px 0 2px 22px;
    font-size: 9px;
    color: rgba(248, 113, 113, 0.55);
  }

  /* ── Trace ── */

  .trace-scroll {
    max-height: 200px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(200, 175, 100, 0.08) transparent;
    background: rgba(0, 0, 0, 0.25);
    border-radius: 5px;
    border: 1px solid rgba(255, 255, 255, 0.02);
    padding: 4px 0;
  }

  .trace-line {
    display: flex;
    gap: 8px;
    padding: 2px 10px;
    font-size: 10px;
    line-height: 1.5;
    transition: background 0.1s;
  }
  .trace-line:hover { background: rgba(200, 175, 100, 0.03); }
  .trace-line.trace-latest {
    background: rgba(232, 169, 75, 0.04);
    border-left: 2px solid rgba(232, 169, 75, 0.3);
  }

  .trace-time {
    color: rgba(255, 255, 255, 0.18);
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
    min-width: 60px;
  }

  .trace-text { color: rgba(200, 195, 170, 0.5); word-break: break-word; }

  /* ── Tool calls ── */

  .tools-scroll {
    max-height: 160px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(200, 175, 100, 0.08) transparent;
    background: rgba(0, 0, 0, 0.25);
    border-radius: 5px;
    border: 1px solid rgba(255, 255, 255, 0.02);
    padding: 4px 0;
  }

  .tool-line {
    display: flex;
    gap: 8px;
    padding: 2px 10px;
    font-size: 10px;
    line-height: 1.5;
  }

  .tool-time {
    color: rgba(255, 255, 255, 0.15);
    flex-shrink: 0;
    font-variant-numeric: tabular-nums;
    min-width: 60px;
  }
  .tool-name { color: rgba(100, 200, 230, 0.65); flex-shrink: 0; }
  .tool-args {
    color: rgba(255, 255, 255, 0.15);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;
  }

  /* ── Resource meters ── */

  .meter-grid { display: flex; flex-direction: column; gap: 6px; }

  .meter { display: flex; flex-direction: column; gap: 2px; }

  .meter-bar {
    height: 4px;
    background: rgba(255, 255, 255, 0.04);
    border-radius: 2px;
    overflow: hidden;
  }

  .meter-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.5s ease;
  }

  .meter-fill-in { background: linear-gradient(90deg, rgba(91, 141, 239, 0.4), rgba(91, 141, 239, 0.7)); }
  .meter-fill-out { background: linear-gradient(90deg, rgba(167, 139, 250, 0.4), rgba(167, 139, 250, 0.7)); }
  .meter-fill-cache { background: linear-gradient(90deg, rgba(52, 211, 153, 0.4), rgba(52, 211, 153, 0.7)); }

  .meter-info {
    display: flex;
    justify-content: space-between;
    font-size: 9px;
  }

  .meter-label {
    font-weight: 600;
    letter-spacing: 0.08em;
    color: rgba(255, 255, 255, 0.18);
  }

  .meter-value {
    font-variant-numeric: tabular-nums;
    color: rgba(255, 255, 255, 0.35);
  }

  /* ── Cognitive misses ── */

  .misses { display: flex; flex-direction: column; gap: 2px; }

  .miss-item {
    font-size: 10px;
    padding: 3px 8px;
    background: rgba(251, 191, 36, 0.04);
    border-left: 2px solid rgba(251, 191, 36, 0.2);
    border-radius: 0 3px 3px 0;
    color: rgba(251, 191, 36, 0.55);
  }

  /* ── Adaptations ── */

  .adaptations { display: flex; flex-direction: column; gap: 2px; }

  .adapt-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    padding: 3px 8px;
    background: rgba(34, 211, 238, 0.03);
    border-radius: 3px;
  }
  .adapt-trigger { color: rgba(34, 211, 238, 0.5); }
  .adapt-arrow { color: rgba(255, 255, 255, 0.15); }
  .adapt-action { color: rgba(255, 255, 255, 0.35); }

  /* ── Actions ── */

  .detail-actions {
    display: flex;
    gap: 6px;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid rgba(255, 255, 255, 0.03);
  }

  .action-btn {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 4px 12px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 5px;
  }

  .action-cancel {
    background: rgba(248, 113, 113, 0.06);
    color: rgba(248, 113, 113, 0.6);
    border-color: rgba(248, 113, 113, 0.15);
  }
  .action-cancel:hover {
    background: rgba(248, 113, 113, 0.12);
    color: rgba(248, 113, 113, 0.85);
  }

  .action-view {
    background: rgba(200, 175, 100, 0.06);
    color: rgba(200, 175, 100, 0.6);
    border-color: rgba(200, 175, 100, 0.15);
  }
  .action-view:hover {
    background: rgba(200, 175, 100, 0.12);
    color: rgba(200, 175, 100, 0.85);
  }

  /* ── Error block ── */

  .error-block {
    font-size: 10px;
    padding: 8px 10px;
    background: rgba(248, 113, 113, 0.04);
    border: 1px solid rgba(248, 113, 113, 0.1);
    border-radius: 5px;
    color: rgba(248, 113, 113, 0.65);
    line-height: 1.5;
    max-height: 120px;
    overflow-y: auto;
    word-break: break-word;
  }

  /* ── Error classification ── */

  .classification-block {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    align-items: flex-start;
  }

  .class-badge {
    font-size: 9px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 3px;
    background: rgba(251, 191, 36, 0.08);
    color: rgba(251, 191, 36, 0.6);
    letter-spacing: 0.04em;
  }
  .class-retry { background: rgba(52, 211, 153, 0.08); color: rgba(52, 211, 153, 0.6); }
  .class-no-retry { background: rgba(248, 113, 113, 0.08); color: rgba(248, 113, 113, 0.6); }

  .class-suggestion {
    width: 100%;
    font-size: 10px;
    color: rgba(255, 255, 255, 0.3);
    margin-top: 4px;
    line-height: 1.4;
  }

  /* ── Postmortem ── */

  .postmortem-block {
    background: rgba(167, 139, 250, 0.03);
    border: 1px solid rgba(167, 139, 250, 0.08);
    border-radius: 5px;
    padding: 6px 10px;
  }

  .pm-row {
    display: flex;
    gap: 8px;
    font-size: 10px;
    padding: 2px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.02);
  }
  .pm-row:last-child { border-bottom: none; }

  .pm-key {
    color: rgba(167, 139, 250, 0.5);
    flex-shrink: 0;
    min-width: 100px;
    font-weight: 600;
  }

  .pm-val {
    color: rgba(255, 255, 255, 0.3);
    word-break: break-word;
  }
</style>
