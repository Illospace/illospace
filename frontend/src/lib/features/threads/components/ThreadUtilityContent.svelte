<script lang="ts">
  import { onDestroy } from 'svelte';
  import { cortex } from '$lib/stores/cortex.svelte';
  import {
    activityTimeline,
    auditApply,
    auditEval,
    downloadThreadTraceZip,
    getIdea,
    ideaAudit,
    ideaAuditAnalysisResult,
    ideaAuditAnalyze,
    ideaConnections as loadIdeaConnections,
    runHistory,
  } from '$lib/features/threads/api/threadApi';
  import { renderReadableMarkdown } from '$lib/utils/readableMarkdown';
  import { formatDurationMs, formatDurationSeconds, relativeTimeAgo } from '$lib/utils/datetime';

  type UtilityTab = 'activity' | 'details' | 'audit';
  type ActivityListItem = {
    _key: string;
    timestamp: string | null;
    title: string;
    meta: string[];
    error?: string;
    state?: string;
  };

  let {
    idea,
    activeTab = 'activity',
  }: {
    idea: any;
    activeTab?: UtilityTab;
  } = $props();

  let activityItems = $state<ActivityListItem[]>([]);
  let activityLoading = $state(false);
  let lastActivityIdeaId = $state<string | null>(null);
  let activityRequestSeq = 0;

  let detailsData = $state<any>(null);
  let detailsLoading = $state(false);
  let ideaConnections = $state<any[]>([]);
  let metricsData = $state<any>(null);

  let auditData = $state<any>(null);
  let auditLoading = $state(false);
  let auditError = $state('');
  let analysisRunning = $state(false);
  let analysisRunId = $state<number | null>(null);
  let analysisResult = $state<any>(null);
  let proposals = $state<any[]>([]);
  let proposalApplying = $state<Record<number, boolean>>({});
  let proposalResults = $state<Record<number, { ok: boolean; msg: string }>>({});
  let evalLoading = $state<Record<number, boolean>>({});
  let evalResults = $state<Record<number, any>>({});
  let expandedRuns = $state<Set<number>>(new Set());
  let expandedWorkers = $state<Set<string>>(new Set());
  let threadTraceSaving = $state(false);
  let threadTraceSaved = $state<{ bytes?: number; filename?: string } | null>(null);
  let threadTraceError = $state('');

  let pollAborted = $state(false);
  let loadedForIdeaId = $state<string | null>(null);
  let lastLoadedKey = $state('');

  const STATUS_COLORS: Record<string, string> = {
    idle: '#57CFA0',
    working: 'var(--thread-accent, #57CFA0)',
    done: '#57CFA0',
  };

  function resetUtilityState() {
    activityItems = [];
    activityLoading = false;
    lastActivityIdeaId = null;
    activityRequestSeq += 1;

    detailsData = null;
    detailsLoading = false;
    ideaConnections = [];
    metricsData = null;

    auditData = null;
    auditLoading = false;
    auditError = '';
    analysisRunning = false;
    analysisRunId = null;
    analysisResult = null;
    proposals = [];
    proposalApplying = {};
    proposalResults = {};
    evalLoading = {};
    evalResults = {};
    expandedRuns = new Set();
    expandedWorkers = new Set();
    threadTraceSaving = false;
    threadTraceSaved = null;
    threadTraceError = '';
    pollAborted = false;
  }

  const timeAgo = relativeTimeAgo;
  const formatDuration = formatDurationSeconds;
  const EMOJI_PATTERN = /[\u{1F1E6}-\u{1F1FF}\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}]/gu;

  function cleanActivityText(value: unknown, fallback = 'Activity'): string {
    const text = String(value ?? '')
      .replace(EMOJI_PATTERN, '')
      .replace(/\s+/g, ' ')
      .trim();
    return text || fallback;
  }

  function formatActivityKind(value: unknown): string {
    return cleanActivityText(value, 'event')
      .replace(/[._-]+/g, ' ')
      .replace(/\brun\b/i, '')
      .replace(/\s+/g, ' ')
      .trim()
      .toLowerCase() || 'event';
  }

  function compactCost(value: unknown): string | null {
    if (value === null || value === undefined || value === '') return null;
    const amount = Number(value);
    if (!Number.isFinite(amount)) return null;
    return `$${amount.toFixed(4)}`;
  }

  function runDuration(item: any): string | null {
    if (typeof item.duration_sec === 'number') return formatDuration(item.duration_sec);
    if (!item.started_at || !item.completed_at) return null;
    const seconds = Math.round((new Date(item.completed_at).getTime() - new Date(item.started_at).getTime()) / 1000);
    return Number.isFinite(seconds) ? formatDuration(seconds) : null;
  }

  function activityMeta(parts: Array<unknown>): string[] {
    return parts
      .map((part) => cleanActivityText(part, ''))
      .filter(Boolean);
  }

  async function loadActivity() {
    const ideaId = idea?.id;
    if (!ideaId) return;

    const isNewIdea = ideaId !== lastActivityIdeaId;
    if (isNewIdea) {
      lastActivityIdeaId = ideaId;
      activityItems = [];
    }

    const requestId = ++activityRequestSeq;
    const showLoadingState = activityItems.length === 0;
    if (showLoadingState) activityLoading = true;

    try {
      let timelineEvents: ActivityListItem[] = [];
      try {
        const events = await activityTimeline(ideaId);
        timelineEvents = events.map((ev: any, index: number) => ({
          _key: `timeline-${ev.id ?? ev.timestamp ?? index}-${ev.label ?? ev.type ?? ''}`,
          timestamp: ev.timestamp,
          title: cleanActivityText(ev.label || ev.type, 'Activity'),
          meta: activityMeta([formatActivityKind(ev.type)]),
          state: cleanActivityText(ev.type, 'event'),
        }));
      } catch { /* timeline API not available */ }

      let runEvents: ActivityListItem[] = [];
      try {
        const runs = await runHistory(ideaId);
        runEvents = runs.flatMap((dp: any, index: number) => {
          const runId = dp.id ?? dp.run_id ?? index;
          const status = cleanActivityText(dp.status, 'run').toLowerCase();
          const duration = runDuration(dp);
          const cost = compactCost(dp.estimated_cost);
          const tokens = typeof dp.tokens_total === 'number' ? `${dp.tokens_total.toLocaleString()} tok` : null;
          const summary: ActivityListItem = {
            _key: `run-${runId}-${dp.updated_at ?? dp.completed_at ?? dp.created_at ?? dp.started_at}`,
            timestamp: dp.completed_at ?? dp.failed_at ?? dp.canceled_at ?? dp.started_at ?? dp.created_at,
            title: `Run ${status}`,
            meta: activityMeta([dp.skill_used, dp.model_used, duration, tokens, cost]),
            error: dp.error ? cleanActivityText(String(dp.error).slice(0, 200), '') : undefined,
            state: status,
          };
          const trace = Array.isArray(dp.activity_trace) ? dp.activity_trace : [];
          const traceEvents: ActivityListItem[] = trace.map((entry: any, traceIndex: number) => ({
            _key: `run-${runId}-trace-${entry.sequence_no ?? entry.at ?? traceIndex}`,
            timestamp: entry.at ?? dp.started_at ?? dp.created_at,
            title: cleanActivityText(entry.activity || entry.text, 'Run activity'),
            meta: activityMeta([formatActivityKind(entry.kind), entry.tool_name]),
            error: entry.error ? cleanActivityText(String(entry.error).slice(0, 200), '') : undefined,
            state: entry.status ?? status,
          }));
          return [summary, ...traceEvents];
        });
      } catch { /* run API not available */ }

      if (requestId !== activityRequestSeq || idea?.id !== ideaId) return;

      const seen = new Set<string>();
      activityItems = [...timelineEvents, ...runEvents]
        .filter((item: ActivityListItem) => {
          const key = `${item.timestamp || ''}|${item.title}|${item.meta.join('|')}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .sort((a, b) => new Date(b.timestamp || 0).getTime() - new Date(a.timestamp || 0).getTime());
    } catch {
      if (activityItems.length === 0) activityItems = [];
    } finally {
      if (requestId === activityRequestSeq && idea?.id === ideaId) {
        activityLoading = false;
      }
    }
  }

  async function loadDetails() {
    if (!idea) return;
    detailsLoading = true;
    try {
      const [detail, conns] = await Promise.all([
        getIdea(idea.id),
        loadIdeaConnections(idea.id).catch(() => []),
      ]);
      detailsData = detail;
      ideaConnections = conns;

      const timeline = (detail as any).timeline || [];
      const stateTime: Record<string, number> = {};
      for (let i = 0; i < timeline.length; i++) {
        const entry = timeline[i];
        const state = entry.to_state;
        const start = new Date(entry.changed_at).getTime();
        const end = i < timeline.length - 1 ? new Date(timeline[i + 1].changed_at).getTime() : Date.now();
        stateTime[state] = (stateTime[state] || 0) + (end - start);
      }

      let interactionCount = 0;
      let lastInteraction: string | null = null;
      try {
        const events = await activityTimeline(idea.id);
        interactionCount = events.length;
        if (events.length > 0) lastInteraction = events[events.length - 1].timestamp;
      } catch { /* fallback */ }

      metricsData = {
        created: detail.created_at || idea.created_at,
        lastInteraction,
        interactionCount,
        stateTime,
      };
    } catch {
      detailsData = null;
      metricsData = null;
    }
    detailsLoading = false;
  }

  function navigateToIdea(linkedId: string) {
    cortex.selectIdea(linkedId);
  }

  async function downloadThreadTrace() {
    const ideaId = idea?.id;
    if (!ideaId || threadTraceSaving) return;
    threadTraceSaving = true;
    threadTraceError = '';
    try {
      const result = await downloadThreadTraceZip(ideaId);
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = result.filename || `illo-thread-trace-${ideaId}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      threadTraceSaved = {
        bytes: result.bytes,
        filename: result.filename,
      };
    } catch (e: any) {
      threadTraceError = e?.detail || 'Trace download failed';
    } finally {
      threadTraceSaving = false;
    }
  }

  function loadUtilityTab(tab: UtilityTab) {
    if (tab === 'activity') loadActivity();
    else if (tab === 'details') loadDetails();
    else if (tab === 'audit') loadAudit();
  }

  async function loadAudit() {
    if (!idea) return;
    auditLoading = true;
    auditError = '';
    try {
      auditData = await ideaAudit(idea.id);
    } catch (e: any) {
      auditError = e?.detail || 'Failed to load audit data';
      auditData = null;
    }
    auditLoading = false;
    loadAnalysisResult();
  }

  async function runAnalysis() {
    if (!idea || analysisRunning) return;
    analysisRunning = true;
    analysisResult = null;
    pollAborted = false;
    try {
      const res = await ideaAuditAnalyze(idea.id);
      analysisRunId = res.run_id;
      pollAnalysisResult();
    } catch (e: any) {
      auditError = e?.detail || 'Failed to start analysis';
      analysisRunning = false;
    }
  }

  async function pollAnalysisResult() {
    if (!idea) return;
    const maxAttempts = 60;
    for (let i = 0; i < maxAttempts; i++) {
      if (pollAborted) return;
      await new Promise((r) => setTimeout(r, 2000));
      if (pollAborted) return;
      try {
        const res = await ideaAuditAnalysisResult(idea.id);
        if (res.found && (res.status === 'completed' || res.status === 'failed')) {
          analysisResult = res;
          analysisRunning = false;
          return;
        }
      } catch {
        // keep polling
      }
    }
    analysisRunning = false;
  }

  async function loadAnalysisResult() {
    if (!idea) return;
    try {
      const res = await ideaAuditAnalysisResult(idea.id);
      if (res.found && res.content) {
        analysisResult = res;
        analysisRunId = res.run_id;
      }
    } catch {
      // no previous result
    }
  }

  async function applyProposal(idx: number, type: string, payload: any) {
    proposalApplying = { ...proposalApplying, [idx]: true };
    try {
      await auditApply(type, payload);
      proposalResults = { ...proposalResults, [idx]: { ok: true, msg: 'Applied!' } };
    } catch (e: any) {
      proposalResults = { ...proposalResults, [idx]: { ok: false, msg: e?.detail || 'Failed' } };
    }
    proposalApplying = { ...proposalApplying, [idx]: false };
  }

  async function evaluateProposal(idx: number, proposal: any) {
    if (evalResults[idx] || evalLoading[idx]) return;
    evalLoading = { ...evalLoading, [idx]: true };
    try {
      const res = await auditEval(proposal);
      evalResults = { ...evalResults, [idx]: res };
    } catch (e: any) {
      evalResults = { ...evalResults, [idx]: { error: true, detail: e?.detail || 'No past runs available for evaluation — apply with caution' } };
    }
    evalLoading = { ...evalLoading, [idx]: false };
  }

  function evalScoreColor(score: number): string {
    if (score >= 8) return 'var(--positive, #6BC785)';
    if (score >= 5) return 'var(--thread-accent, #57CFA0)';
    return 'var(--negative, #D4808F)';
  }

  function evalBorderColor(avgScore: number): string {
    if (avgScore >= 6) return 'rgba(107, 199, 133, 0.5)';
    if (avgScore >= 3) return 'color-mix(in srgb, var(--thread-accent, #57CFA0) 50%, transparent)';
    return 'rgba(212, 128, 143, 0.5)';
  }

  $effect(() => {
    const ideaId = idea?.id ?? null;
    const tab = activeTab;

    if (ideaId !== loadedForIdeaId) {
      loadedForIdeaId = ideaId;
      lastLoadedKey = '';
      resetUtilityState();
    }

    if (!ideaId) return;
    const key = `${ideaId}:${tab}`;
    if (key === lastLoadedKey) return;
    lastLoadedKey = key;
    loadUtilityTab(tab);
  });

  onDestroy(() => {
    pollAborted = true;
  });
</script>

{#if activeTab === 'activity'}
  <div class="activity-trace-toolbar">
    <div class="activity-trace-copy">
      <div class="activity-trace-title">Conversation trace</div>
      {#if threadTraceSaved}
        <div class="activity-trace-note">
          {threadTraceSaved.filename || 'Trace zip'}
          {#if threadTraceSaved.bytes}
            &middot; {threadTraceSaved.bytes.toLocaleString()} bytes
          {/if}
        </div>
      {:else if threadTraceError}
        <div class="activity-trace-note activity-trace-note-error">{threadTraceError}</div>
      {:else}
        <div class="activity-trace-note">Thread transcript, runs, tools, and artifacts</div>
      {/if}
    </div>
    <button
      type="button"
      class="activity-trace-button activity-trace-button-primary"
      disabled={!idea?.id || threadTraceSaving}
      onclick={downloadThreadTrace}
    >
      {threadTraceSaving ? 'Preparing' : threadTraceSaved ? 'Download again' : 'Download trace'}
    </button>
  </div>

  {#if activityLoading && activityItems.length === 0}
    <div class="tab-empty">Loading activity...</div>
  {:else if activityItems.length === 0}
    <div class="tab-empty">No activity yet.</div>
  {:else}
    <div class="activity-list" aria-label="Thread activity">
      {#each activityItems as item (item._key)}
        <div class="activity-list-item" data-state={item.state}>
          <time class="activity-time" datetime={item.timestamp || undefined}>
            {timeAgo(item.timestamp)}
          </time>
          <div class="activity-body">
            <div class="activity-title-row">
              <div class="activity-title">{item.title}</div>
            </div>
            {#if item.meta.length}
              <div class="activity-meta">
                {#each item.meta as meta}
                  <span class="activity-meta-part">{meta}</span>
                {/each}
              </div>
            {/if}
            {#if item.error}
              <div class="activity-error">Error: {item.error}</div>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
{:else if activeTab === 'details'}
  {#if detailsLoading}
    <div class="tab-empty">Loading details...</div>
  {:else}
    <div class="details-section">
      <h4 class="details-section-title">Info</h4>
      <div class="details-body">
        {#if idea.description}
          <p class="details-desc">{idea.description}</p>
        {:else}
          <p class="details-desc details-empty"><em>No description</em></p>
        {/if}
        <div class="details-meta">
          Origin: <strong>{idea.origin || 'unknown'}</strong><br>
          Salience: {(idea.salience_score || 0).toFixed(1)}
        </div>
      </div>
    </div>

    <div class="details-section">
      <h4 class="details-section-title">Connections</h4>
      {#if ideaConnections.length === 0}
        <p class="details-empty">No connections.</p>
      {:else}
        {#each ideaConnections as conn}
          {@const linkedId = conn.linked_id || (conn.source_id === idea.id ? conn.target_id : conn.source_id)}
          {@const linkedTitle = conn.linked_title || linkedId}
          {@const linkedIdea = cortex.ideas.find(i => i.id === linkedId)}
          <div class="link-item" role="button" tabindex="0" onclick={() => navigateToIdea(linkedId)} onkeydown={(e: KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigateToIdea(linkedId); } }}>
            <div class="link-dot" style="background: {STATUS_COLORS[linkedIdea?.status || 'idle']};"></div>
            <div>
              <div style="color: var(--text-1);">{linkedTitle}</div>
              <div class="link-meta">{conn.type} &middot; weight {conn.weight}</div>
            </div>
          </div>
        {/each}
      {/if}
    </div>

    <div class="details-section">
      <h4 class="details-section-title">Metrics</h4>
      {#if metricsData}
        <div class="metrics-section">
          <div class="metrics-label">Created</div>
          <div class="metrics-value">
            {metricsData.created ? new Date(metricsData.created).toLocaleDateString() + ' ' + new Date(metricsData.created).toLocaleTimeString() : 'Unknown'}
          </div>
        </div>
        <div class="metrics-section">
          <div class="metrics-label">Last Interaction</div>
          <div class="metrics-value">
            {metricsData.lastInteraction ? timeAgo(metricsData.lastInteraction) : 'None'}
          </div>
        </div>
        <div class="metrics-section">
          <div class="metrics-label">Interaction Count</div>
          <div class="metrics-value">{metricsData.interactionCount}</div>
        </div>
        <div class="metrics-section">
          <div class="metrics-label">Time in Each State</div>
          {#if Object.keys(metricsData.stateTime).length > 0}
            {#each Object.entries(metricsData.stateTime) as [state, ms]}
              <div class="metrics-row">
                <span class="metrics-dot" style="background: {STATUS_COLORS[state] || '#888'};"></span>
                <span>{state}</span>
                <span class="metrics-value">{formatDurationMs(ms as number)}</span>
              </div>
            {/each}
          {:else}
            <div class="details-empty">No state transitions recorded</div>
          {/if}
        </div>
      {:else}
        <div class="tab-empty">Failed to load metrics.</div>
      {/if}
    </div>
  {/if}
{:else if activeTab === 'audit'}
  {#if auditLoading}
    <div class="tab-empty">Loading audit data...</div>
  {:else if auditError && !auditData}
    <div class="tab-empty">{auditError}</div>
  {:else if auditData}
    {@const tb = auditData.tokens_breakdown || {}}
    {@const grandTotal = tb.grand_total || 0}
    {@const grandInput = tb.grand_total_input || 0}
    {@const grandOutput = tb.grand_total_output || 0}
    {@const runnerTotal = tb.runner_total || 0}
    {@const workerTotal = tb.worker_total || 0}
    {@const totalCacheRead = auditData.totals?.cache_read || 0}
    {@const workerCount = auditData.worker_totals?.worker_count || 0}
    {@const outputRatio = grandTotal ? grandOutput / grandTotal : 0}

    {#if grandTotal > 200000}
      <div class="audit-burn-alert">
        <span class="audit-burn-icon">⚠</span>
        High token usage session — consider auditing for inefficiencies
      </div>
    {/if}

    <div class="audit-cards">
      <div class="audit-card audit-card-highlight">
        <div class="audit-card-label">Total Tokens</div>
        <div class="audit-card-value">{grandTotal.toLocaleString()}</div>
        <div class="audit-card-sub">
          disp: {runnerTotal.toLocaleString()} · workers: {workerTotal.toLocaleString()}
        </div>
        <div class="audit-token-pills" style="margin-top: 4px;">
          <span class="audit-pill audit-pill-input">↓ {grandInput.toLocaleString()} in</span>
          <span class="audit-pill audit-pill-output">↑ {grandOutput.toLocaleString()} out</span>
          {#if totalCacheRead > 0}
            <span class="audit-pill audit-pill-cache">⚡ {totalCacheRead.toLocaleString()} cached</span>
          {/if}
        </div>
      </div>
      <div class="audit-card">
        <div class="audit-card-label">Output Ratio</div>
        <div class="audit-card-value" style="color: {outputRatio < 0.10 ? 'var(--negative, #D4808F)' : outputRatio <= 0.25 ? 'var(--positive, #6BC785)' : 'var(--text-1)'};">
          {(outputRatio * 100).toFixed(1)}%
        </div>
        <div class="audit-card-sub">
          {outputRatio < 0.10 ? 'Low — heavy context loading' : outputRatio <= 0.25 ? 'Healthy range' : 'High output ratio'}
        </div>
      </div>
      <div class="audit-card">
        <div class="audit-card-label">Est. Cost</div>
        <div class="audit-card-value">${auditData.totals.estimated_cost?.toFixed(4) || '0.00'}</div>
      </div>
      <div class="audit-card">
        <div class="audit-card-label">Duration</div>
        <div class="audit-card-value">{formatDuration(auditData.totals.duration_sec)}</div>
      </div>
      <div class="audit-card">
        <div class="audit-card-label">Runs</div>
        <div class="audit-card-value">{auditData.efficiency.run_count}</div>
      </div>
      <div class="audit-card">
        <div class="audit-card-label">Runner Tokens</div>
        <div class="audit-card-value">{runnerTotal.toLocaleString()}</div>
        <div class="audit-card-sub">{grandTotal ? ((runnerTotal / grandTotal) * 100).toFixed(0) : 0}% of total</div>
      </div>
      <div class="audit-card">
        <div class="audit-card-label">Worker Tokens</div>
        <div class="audit-card-value">{workerTotal.toLocaleString()}</div>
        <div class="audit-card-sub">{grandTotal ? ((workerTotal / grandTotal) * 100).toFixed(0) : 0}% of total</div>
      </div>
      <div class="audit-card">
        <div class="audit-card-label">Workers Spawned</div>
        <div class="audit-card-value">{workerCount}</div>
        <div class="audit-card-sub">{auditData.worker_totals?.success_count || 0} ok / {auditData.worker_totals?.failure_count || 0} failed</div>
      </div>
    </div>

    {#if grandTotal > 0 && workerCount > 0}
      <div class="details-section">
        <h4 class="details-section-title">Token Distribution</h4>
        <div class="audit-token-split">
          <div class="audit-token-bar">
            <div class="audit-token-bar-runner" style="width: {(runnerTotal / grandTotal) * 100}%;"></div>
            <div class="audit-token-bar-worker" style="width: {(workerTotal / grandTotal) * 100}%;"></div>
          </div>
          <div class="audit-token-legend">
            <span class="audit-legend-item"><span class="audit-legend-dot runner"></span> Runner {((runnerTotal / grandTotal) * 100).toFixed(0)}%</span>
            <span class="audit-legend-item"><span class="audit-legend-dot worker"></span> Workers {((workerTotal / grandTotal) * 100).toFixed(0)}%</span>
          </div>
        </div>
      </div>
    {/if}

    <div class="details-section">
      <h4 class="details-section-title">Efficiency</h4>
      <div class="audit-efficiency">
        <div class="metrics-row">
          <span>Cache Hit Rate</span>
          <span class="metrics-value" style="color: {auditData.efficiency.cache_hit_rate > 0.3 ? 'var(--positive, #6BC785)' : 'var(--text-3)'};">
            {(auditData.efficiency.cache_hit_rate * 100).toFixed(1)}%
          </span>
        </div>
        <div class="metrics-row">
          <span>Avg Tokens / Run</span>
          <span class="metrics-value">{auditData.efficiency.avg_tokens_per_run.toLocaleString()}</span>
        </div>
        <div class="metrics-row">
          <span>Cognitive Misses</span>
          <span class="metrics-value" style="color: {auditData.efficiency.cognitive_misses_count > 0 ? 'var(--negative, #D4808F)' : 'var(--text-3)'};">
            {auditData.efficiency.cognitive_misses_count}
          </span>
        </div>
        {#if auditData.efficiency.worker_success_rate !== undefined}
          <div class="metrics-row">
            <span>Worker Success Rate</span>
            <span class="metrics-value" style="color: {auditData.efficiency.worker_success_rate >= 0.8 ? 'var(--positive, #6BC785)' : auditData.efficiency.worker_success_rate >= 0.5 ? 'var(--text-3)' : 'var(--negative, #D4808F)'};">
              {(auditData.efficiency.worker_success_rate * 100).toFixed(1)}%
            </span>
          </div>
        {/if}
      </div>
    </div>

    {#if auditData.tool_summary?.length}
      {@const maxCount = Math.max(...auditData.tool_summary.map((t: any) => t.count))}
      <div class="details-section">
        <h4 class="details-section-title">Tool Usage</h4>
        {#each auditData.tool_summary as tool}
          <div class="audit-bar-row">
            <span class="audit-bar-label" title={tool.tool_name}>{tool.tool_name}</span>
            <div class="audit-bar-track">
              <div
                class="audit-bar-fill"
                style="width: {Math.max((tool.count / maxCount) * 100, 4)}%;"
              ></div>
            </div>
            <span class="audit-bar-count">{tool.count}</span>
          </div>
        {/each}
      </div>
    {/if}

    <div class="details-section">
      <h4 class="details-section-title">Run Timeline</h4>
      {#each auditData.runs as d, dIdx}
        {@const hasWorkers = d.workers && d.workers.length > 0}
        {@const isExpanded = expandedRuns.has(dIdx)}
        {@const dWorkTokens = (d.workers || []).reduce((s: number, w: any) => s + (w.tokens || 0), 0)}
        {@const dDispTokens = d.runner_tokens ?? ((d.tokens_grand_total || d.tokens_total || 0) - dWorkTokens)}
        <div class="audit-run-block">
          <button
            type="button"
            class="audit-run-row"
            class:clickable={hasWorkers}
            onclick={() => { if (hasWorkers) { const next = new Set(expandedRuns); if (next.has(dIdx)) next.delete(dIdx); else next.add(dIdx); expandedRuns = next; } }}
          >
            <span class="audit-run-status" style="color: {d.status === 'completed' ? 'var(--positive, #6BC785)' : d.status === 'failed' ? 'var(--negative, #D4808F)' : 'var(--text-3)'};">
              {d.status === 'completed' ? '✓' : d.status === 'failed' ? '✗' : '●'}
            </span>
            <div class="audit-run-info">
              <div class="audit-run-skill">
                {#if hasWorkers}
                  <span class="audit-expand-icon">{isExpanded ? '▾' : '▸'}</span>
                {/if}
                <span class="skill-dot"></span>
                {d.skill_used || 'unknown'}
                {#if d.model_used}
                  <span class="dbadge dbadge-model" style="font-size:9px;">{d.model_used}</span>
                {/if}
                {#if hasWorkers}
                  <span class="audit-worker-badge">{d.workers.length} worker{d.workers.length !== 1 ? 's' : ''}</span>
                {/if}
              </div>
              <div class="audit-run-meta">
                {#if d.tokens_grand_total || d.tokens_total}<span>{(d.tokens_grand_total || d.tokens_total).toLocaleString()} tok</span>{/if}
                {#if d.duration_sec}<span>{formatDuration(d.duration_sec)}</span>{/if}
                {#if d.estimated_cost}<span>${d.estimated_cost.toFixed(4)}</span>{/if}
                {#if d.tool_count}<span>{d.tool_count} tools{#if d.runner_tool_count || d.worker_tool_count} ({d.runner_tool_count || 0} runner + {d.worker_tool_count || 0} workers){/if}</span>{/if}
              </div>
              {#if d.tokens_input || d.tokens_output || d.cache_read}
                <div class="audit-token-pills">
                  {#if d.tokens_input}<span class="audit-pill audit-pill-input">↓ {d.tokens_input.toLocaleString()} input</span>{/if}
                  {#if d.tokens_output}<span class="audit-pill audit-pill-output">↑ {d.tokens_output.toLocaleString()} output</span>{/if}
                  {#if d.cache_read}<span class="audit-pill audit-pill-cache">⚡ {d.cache_read.toLocaleString()} cached</span>{/if}
                </div>
              {/if}
              {#if hasWorkers && d.tokens_total}
                <div class="audit-run-token-bar">
                  <div class="audit-token-bar-runner" style="width: {Math.max((dDispTokens / d.tokens_total) * 100, 2)}%;"></div>
                  <div class="audit-token-bar-worker" style="width: {Math.max((dWorkTokens / d.tokens_total) * 100, 2)}%;"></div>
                </div>
                <div class="audit-run-token-labels">
                  <span>disp: {dDispTokens.toLocaleString()}</span>
                  <span>workers: {dWorkTokens.toLocaleString()}</span>
                </div>
              {/if}
            </div>
            <span class="audit-run-time">{d.started_at ? timeAgo(d.started_at) : ''}</span>
          </button>

          {#if isExpanded && hasWorkers}
            <div class="audit-workers-panel">
              {#each d.workers as w, wIdx}
                {@const wKey = `${dIdx}-${wIdx}`}
                {@const wExpanded = expandedWorkers.has(wKey)}
                <div class="audit-worker-row">
                  <span class="audit-worker-status" style="color: {w.success ? 'var(--positive, #6BC785)' : w.status === 'skipped' ? 'var(--text-3)' : 'var(--negative, #D4808F)'};">
                    {w.success ? '✓' : w.status === 'skipped' ? '○' : '✗'}
                  </span>
                  <div class="audit-worker-info">
                    <div class="audit-worker-skill">
                      {w.skill || 'general'}
                      {#if w.node_id}
                        <span class="audit-phase-badge">{w.node_id}</span>
                      {/if}
                      {#if w.status && w.status !== 'completed' && w.status !== 'failed'}
                        <span class="audit-status-badge" style="color: {w.status === 'running' ? 'var(--thread-accent, #57CFA0)' : 'var(--text-3)'};">{w.status}</span>
                      {/if}
                    </div>
                    <div class="audit-worker-meta">
                      {#if w.tokens}<span>{w.tokens.toLocaleString()} tok</span>{/if}
                      {#if w.tokens_input || w.tokens_output}
                        <span class="audit-pill audit-pill-input" style="font-size:8px;padding:1px 4px;">↓{(w.tokens_input || 0).toLocaleString()}</span>
                        <span class="audit-pill audit-pill-output" style="font-size:8px;padding:1px 4px;">↑{(w.tokens_output || 0).toLocaleString()}</span>
                      {/if}
                      {#if w.estimated_cost}<span>${w.estimated_cost.toFixed(4)}</span>{/if}
                      {#if w.duration_sec}<span>{formatDuration(w.duration_sec)}</span>{/if}
                      {#if w.attempts && w.attempts > 1}<span>{w.attempts} attempts</span>{/if}
                    </div>
                    {#if w.task}
                      <div class="audit-worker-task">{w.task.length > 120 ? w.task.slice(0, 120) + '…' : w.task}</div>
                    {/if}
                    {#if w.error}
                      <div class="audit-worker-error">{w.error}</div>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/each}
    </div>

    <div class="details-section">
      <h4 class="details-section-title">Self-Critique Analysis</h4>
      <button
        class="audit-analyze-btn"
        onclick={runAnalysis}
        disabled={analysisRunning}
      >
        {#if analysisRunning}
          <span class="thinking-orb-inline"></span> Analyzing...
        {:else}
          {analysisResult?.content ? '↻ Re-run Analysis' : '▶ Run Analysis'}
        {/if}
      </button>

      {#if analysisRunning}
        <div class="audit-analysis-live">
          <div class="audit-analysis-pulse"></div>
          <span>Critique in progress — run #{analysisRunId}</span>
        </div>
      {/if}

      {#if analysisResult?.content}
        <div class="audit-critique-report">
          <div class="audit-critique-header">
            <span class="audit-critique-badge" class:is-failed={analysisResult.status === 'failed'}>
              {analysisResult.status === 'failed' ? '✗ Failed' : '✓ Complete'}
            </span>
            {#if analysisResult.completed_at}
              <span class="audit-critique-time">{timeAgo(analysisResult.completed_at)}</span>
            {/if}
          </div>
          <div class="audit-critique-body">
            {@html renderReadableMarkdown(analysisResult.content)}
          </div>
        </div>
      {:else if analysisResult?.status === 'failed' && analysisResult?.error}
        <div class="audit-critique-report audit-critique-failed">
          <div class="audit-critique-header">
            <span class="audit-critique-badge is-failed">✗ Failed</span>
          </div>
          <div class="audit-critique-body">
            <p>{analysisResult.error}</p>
          </div>
        </div>
      {/if}
    </div>

    {#if proposals.length > 0}
      <div class="details-section">
        <h4 class="details-section-title">Proposals</h4>
        {#each proposals as proposal, idx}
          {@const ev = evalResults[idx]}
          {@const evOk = ev && !ev.error}
          <div class="audit-proposal-card">
            <div class="audit-proposal-issue">{proposal.description}</div>
            <div class="audit-proposal-fix">{proposal.improvement}</div>
            <div class="audit-proposal-actions">
              {#if proposalResults[idx]}
                <span class="audit-proposal-result" class:success={proposalResults[idx].ok} class:failure={!proposalResults[idx].ok}>
                  {proposalResults[idx].msg}
                </span>
              {:else}
                <button
                  class="audit-apply-btn"
                  style={evOk ? `border-color: ${evalBorderColor(ev.avg_score)};` : ''}
                  onclick={() => applyProposal(idx, proposal.type, proposal.payload)}
                  disabled={proposalApplying[idx] || (evOk && ev.avg_score < 3)}
                >
                  {proposalApplying[idx] ? '...' : 'Apply'}
                </button>
              {/if}
              {#if !evalResults[idx]}
                <button
                  class="audit-eval-btn"
                  onclick={() => evaluateProposal(idx, proposal)}
                  disabled={evalLoading[idx]}
                >
                  {#if evalLoading[idx]}
                    <span class="thinking-orb-inline"></span> Evaluating…
                  {:else}
                    Evaluate
                  {/if}
                </button>
              {/if}
              {#if evOk && ev.avg_score < 3}
                <span class="audit-eval-warning">⚠ Low confidence</span>
              {/if}
            </div>
            {#if ev}
              <div class="audit-eval-panel">
                {#if ev.error}
                  <div class="audit-eval-empty">{ev.detail}</div>
                {:else}
                  {#each ev.benchmarks as bm}
                    <div class="audit-eval-row">
                      <span class="audit-eval-id">#{bm.run_id}</span>
                      <span class="audit-eval-task">{bm.task_summary.length > 40 ? bm.task_summary.slice(0, 40) + '…' : bm.task_summary}</span>
                      <span class="audit-eval-score" style="color: {evalScoreColor(bm.score)};">{bm.score}/10</span>
                      <span class="audit-eval-detail">{bm.task_solved} → {bm.output_better === 'yes' ? 'would improve' : bm.output_better === 'no' ? 'would regress' : 'neutral'}</span>
                    </div>
                  {/each}
                  <div class="audit-eval-summary">
                    <span>Avg: <strong style="color: {evalScoreColor(ev.avg_score)};">{ev.avg_score}/10</strong></span>
                    <span class="audit-eval-rec" class:rec-apply={ev.recommendation === 'apply'} class:rec-caution={ev.recommendation === 'caution'} class:rec-reject={ev.recommendation === 'reject'}>{ev.recommendation}</span>
                  </div>
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  {:else}
    <div class="tab-empty">No audit data available. Send a message to start.</div>
  {/if}
{/if}

<style>
  .tab-empty {
    color: rgba(231, 238, 247, 0.48);
    font-size: 13px;
    line-height: 1.55;
    padding: 24px 0 14px;
    text-align: left;
    opacity: 1;
    letter-spacing: 0.01em;
  }

  .skill-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #6366f1;
    flex-shrink: 0;
  }

  .dbadge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 9px;
    font-weight: 500;
    color: rgba(255, 255, 255, 0.85);
    white-space: nowrap;
  }
  .dbadge-model { background: #0891b2; }

  /* ── Thinking orb (inline in run header) ────────── */
  .thinking-orb-inline {
    display: inline-block;
    width: 14px;
    height: 14px;
    border-radius: 50%;
    position: relative;
    flex-shrink: 0;
  }

  .thinking-orb-inline::before {
    content: '';
    position: absolute;
    inset: -2px;
    border-radius: 50%;
    background: conic-gradient(
      from 0deg,
      var(--thread-accent, #57CFA0) 0%,
      color-mix(in srgb, var(--thread-accent, #57CFA0) 25%, transparent) 30%,
      transparent 30%,
      transparent 100%
    );
    animation: orb-rotate 4s linear infinite;
    mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1.5px));
    -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1.5px));
  }

  .thinking-orb-inline::after {
    content: '';
    position: absolute;
    inset: 1px;
    border-radius: 50%;
    background: radial-gradient(
      circle at 35% 35%,
      color-mix(in srgb, var(--thread-accent, #57CFA0) 15%, transparent),
      color-mix(in srgb, var(--thread-accent, #57CFA0) 3%, transparent) 60%
    );
    animation: orb-breathe 3s ease-in-out infinite;
  }

  @keyframes orb-rotate { to { transform: rotate(360deg); } }
  @keyframes orb-breathe {
    0%, 100% { transform: scale(1); opacity: 0.7; }
    50% { transform: scale(1.05); opacity: 1; }
  }


  /* Activity tab */
  .activity-trace-toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
    width: 100%;
    margin-bottom: 10px;
    padding: 10px 2px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  }

  .activity-trace-copy {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .activity-trace-title {
    color: rgba(239, 244, 251, 0.92);
    font-size: 12px;
    font-weight: 600;
    line-height: 1.3;
  }

  .activity-list {
    display: flex;
    flex-direction: column;
    width: 100%;
    min-width: 0;
  }

  .activity-list-item {
    display: grid;
    grid-template-columns: 48px minmax(0, 1fr);
    gap: 10px;
    width: 100%;
    min-width: 0;
    padding: 8px 2px 9px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    font-size: 12px;
  }

  .activity-list-item:last-child {
    border-bottom-color: transparent;
  }

  .activity-time {
    min-width: 0;
    color: rgba(231, 238, 247, 0.48);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.35;
    letter-spacing: 0.02em;
    white-space: normal;
    overflow-wrap: anywhere;
  }

  .activity-body {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .activity-title-row {
    min-width: 0;
    display: flex;
    align-items: flex-start;
    gap: 8px;
  }

  .activity-title {
    flex: 1 1 auto;
    min-width: 0;
    color: rgba(239, 244, 251, 0.9);
    font-size: 12px;
    font-weight: 500;
    line-height: 1.35;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .activity-trace-button {
    flex: 0 0 auto;
    min-height: 20px;
    padding: 2px 7px;
    border: 0;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.055);
    color: rgba(231, 238, 247, 0.66);
    font: inherit;
    font-size: 10px;
    line-height: 1.4;
    cursor: pointer;
    transition:
      background 150ms ease,
      color 150ms ease,
      transform 150ms ease;
  }

  .activity-trace-button-primary {
    min-height: 28px;
    padding: 5px 10px;
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 16%, rgba(255, 255, 255, 0.055));
    color: rgba(239, 244, 251, 0.92);
    font-size: 11px;
    font-weight: 600;
  }

  .activity-trace-button:hover:not(:disabled),
  .activity-trace-button:focus-visible {
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 14%, transparent);
    color: rgba(239, 244, 251, 0.9);
  }

  .activity-trace-button:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--thread-accent, #57CFA0) 35%, transparent);
    outline-offset: 2px;
  }

  .activity-trace-button:active:not(:disabled) {
    transform: translateY(1px);
  }

  .activity-trace-button:disabled {
    cursor: default;
    opacity: 0.62;
  }

  .activity-meta {
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    gap: 2px 8px;
    color: rgba(231, 238, 247, 0.52);
    font-size: 10px;
    line-height: 1.35;
  }

  .activity-meta-part {
    min-width: 0;
    max-width: 100%;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .activity-trace-note {
    min-width: 0;
    color: rgba(231, 238, 247, 0.48);
    font-family: var(--constellation-font-mono, var(--font-mono));
    font-size: 9px;
    line-height: 1.35;
    overflow-wrap: anywhere;
  }

  .activity-trace-note-error {
    color: var(--negative, #D4808F);
  }

  .activity-error {
    min-width: 0;
    color: var(--negative, #D4808F);
    font-size: 11px;
    line-height: 1.4;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  /* ── Details tab ─────────────────────────────────────── */
  .details-section {
    margin-bottom: 14px;
    padding: 14px;
    border-radius: 18px;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0.015));
    border: 1px solid rgba(255, 255, 255, 0.04);
  }
  .details-section:first-child {
    padding-top: 14px;
  }

  .details-section-title {
    font-size: 9px;
    font-weight: 600;
    color: rgba(240, 240, 250, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    text-transform: uppercase;
    letter-spacing: 0.16em;
    margin: 0 0 10px 0;
  }

  .details-body {
    font-size: 12px;
  }

  .details-desc {
    color: rgba(239, 244, 251, 0.86);
    margin: 0 0 10px;
    font-size: 13px;
    line-height: 1.6;
  }

  .details-empty {
    color: rgba(231, 238, 247, 0.48);
    font-size: 12px;
  }

  .details-meta {
    font-size: 12px;
    color: rgba(231, 238, 247, 0.58);
    line-height: 1.6;
  }
  .details-meta strong {
    color: rgba(243, 247, 255, 0.88);
  }

  /* ── Connection links ────────────────────────────────── */
  .link-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 0;
    cursor: pointer;
    font-size: 12px;
    color: rgba(239, 244, 251, 0.84);
    transition: color 0.15s;
  }
  .link-item:hover { color: rgba(243, 247, 255, 0.96); }
  .link-item:last-child { border-bottom: 0; }

  .link-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 7px;
    box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.03);
  }

  .link-meta {
    font-size: 10px;
    color: rgba(231, 238, 247, 0.42);
    font-family: var(--font-mono);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-top: 4px;
  }

  /* ── Metrics ─────────────────────────────────────────── */
  .metrics-section {
    margin-bottom: 0;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  }
  .metrics-section:last-child { border-bottom: 0; }

  .metrics-label {
    font-size: 10px;
    color: rgba(231, 238, 247, 0.44);
    font-weight: 500;
    margin-bottom: 0;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    font-family: var(--font-mono);
  }

  .metrics-value {
    font-size: 13px;
    color: rgba(239, 244, 251, 0.84);
    text-align: right;
  }

  .metrics-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 0;
    font-size: 12px;
    color: rgba(239, 244, 251, 0.82);
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
  }
  .metrics-row:last-child { border-bottom: 0; }

  .metrics-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .metrics-row .metrics-value {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: 11px;
    color: rgba(231, 238, 247, 0.58);
    letter-spacing: 0.08em;
  }

  /* ── Audit tab ─────────────────────────────────────────── */
  .audit-cards {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    padding: 0 0 4px;
  }

  .audit-card {
    display: grid;
    gap: 7px;
    background: linear-gradient(180deg, rgba(255, 255, 255, 0.04), rgba(255, 255, 255, 0.015));
    border: 1px solid rgba(255, 255, 255, 0.04);
    border-radius: 18px;
    padding: 14px 14px 15px;
  }

  .audit-card-label {
    font-size: 9px;
    font-weight: 600;
    color: rgba(240, 240, 250, 0.56);
    font-family: var(--constellation-font-mono, var(--font-mono));
    text-transform: uppercase;
    letter-spacing: 0.16em;
    margin-bottom: 0;
  }

  .audit-card-value {
    font-size: 18px;
    font-weight: 600;
    color: rgba(243, 247, 255, 0.92);
    font-family: var(--font-mono);
  }

  .audit-card-sub {
    font-size: 11px;
    color: rgba(231, 238, 247, 0.56);
    margin-top: 4px;
    line-height: 1.5;
  }

  .audit-efficiency {
    padding: 0;
  }

  .audit-bar-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }

  .audit-bar-label {
    font-size: 10px;
    color: var(--text-2);
    width: 100px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .audit-bar-track {
    flex: 1;
    height: 6px;
    background: rgba(255, 255, 255, 0.04);
    border-radius: 3px;
    overflow: hidden;
  }

  .audit-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #6366f1, #818cf8);
    border-radius: 3px;
    transition: width 0.3s ease;
  }

  .audit-bar-count {
    font-size: 10px;
    color: var(--text-3);
    font-family: var(--font-mono);
    width: 28px;
    text-align: right;
    flex-shrink: 0;
  }

  /* Session burn alert */
  .audit-burn-alert {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0 0 12px;
    padding: 12px 14px;
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 8%, transparent);
    border: 1px solid color-mix(in srgb, var(--thread-accent, #57CFA0) 20%, transparent);
    border-radius: 14px;
    font-size: 12px;
    color: var(--thread-accent, #57CFA0);
    letter-spacing: 0.2px;
  }
  .audit-burn-icon {
    font-size: 13px;
    flex-shrink: 0;
  }

  /* Highlight card (Total Tokens) */
  .audit-card-highlight {
    grid-column: 1 / -1;
    border-color: color-mix(in srgb, var(--thread-accent, #57CFA0) 18%, transparent);
    background: linear-gradient(
      180deg,
      color-mix(in srgb, var(--thread-accent, #57CFA0) 8%, transparent),
      rgba(255, 255, 255, 0.02)
    );
  }

  /* Token breakdown pills */
  .audit-token-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  .audit-pill {
    display: inline-flex;
    align-items: center;
    gap: 2px;
    font-size: 9px;
    font-family: var(--font-mono);
    padding: 2px 6px;
    border-radius: 4px;
    line-height: 1.3;
    white-space: nowrap;
  }
  .audit-pill-input {
    background: rgba(255, 255, 255, 0.06);
    color: var(--text-3);
  }
  .audit-pill-output {
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 10%, transparent);
    color: var(--thread-accent, #57CFA0);
  }
  .audit-pill-cache {
    background: rgba(107, 199, 133, 0.1);
    color: #6BC785;
  }

  /* Token distribution bar */
  .audit-token-split {
    padding: 0;
  }
  .audit-token-bar {
    display: flex;
    height: 10px;
    border-radius: 5px;
    overflow: hidden;
    background: rgba(255, 255, 255, 0.04);
  }
  .audit-token-bar-runner {
    height: 100%;
    background: linear-gradient(90deg, #6366f1, #818cf8);
    transition: width 0.3s ease;
  }
  .audit-token-bar-worker {
    height: 100%;
    background: linear-gradient(
      90deg,
      color-mix(in srgb, var(--thread-accent, #57CFA0) 72%, #6366f1 28%),
      var(--thread-accent, #57CFA0)
    );
    transition: width 0.3s ease;
  }
  .audit-token-legend {
    display: flex;
    gap: 16px;
    margin-top: 6px;
    font-size: 10px;
    color: var(--text-3);
  }
  .audit-legend-item {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .audit-legend-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .audit-legend-dot.runner {
    background: #818cf8;
  }
  .audit-legend-dot.worker {
    background: var(--thread-accent, #57CFA0);
  }

  /* Run timeline */
  .audit-run-block {
    border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  }
  .audit-run-block:last-child {
    border-bottom: none;
  }

  .audit-run-row {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 0;
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    text-align: left;
    width: 100%;
  }
  .audit-run-row.clickable {
    cursor: pointer;
  }
  .audit-run-row.clickable:hover {
    background: rgba(255, 255, 255, 0.02);
    border-radius: 4px;
  }

  .audit-run-status {
    font-size: 12px;
    flex-shrink: 0;
    margin-top: 1px;
  }

  .audit-run-info {
    flex: 1;
    min-width: 0;
  }

  .audit-expand-icon {
    font-size: 9px;
    color: var(--text-3);
    width: 10px;
    flex-shrink: 0;
  }

  .audit-run-skill {
    font-size: 11px;
    color: var(--text-1);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .audit-worker-badge {
    font-size: 9px;
    padding: 1px 5px;
    background: rgba(245, 158, 11, 0.12);
    border: 1px solid rgba(245, 158, 11, 0.25);
    border-radius: 8px;
    color: var(--thread-accent, #57CFA0);
    font-weight: 500;
  }

  .audit-run-meta {
    display: flex;
    gap: 8px;
    font-size: 10px;
    color: var(--text-3);
    margin-top: 2px;
  }

  .audit-run-token-bar {
    display: flex;
    height: 4px;
    border-radius: 2px;
    overflow: hidden;
    margin-top: 4px;
    background: rgba(255, 255, 255, 0.04);
  }

  .audit-run-token-labels {
    display: flex;
    justify-content: space-between;
    font-size: 9px;
    color: var(--text-3);
    margin-top: 1px;
  }

  .audit-run-time {
    font-size: 9px;
    color: var(--text-3);
    flex-shrink: 0;
    white-space: nowrap;
  }

  /* Worker panel (expanded) */
  .audit-workers-panel {
    margin-left: 20px;
    padding: 4px 0 8px 12px;
    border-left: 2px solid rgba(245, 158, 11, 0.2);
  }

  .audit-worker-row {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    padding: 4px 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.02);
  }
  .audit-worker-row:last-child {
    border-bottom: none;
  }

  .audit-worker-status {
    font-size: 10px;
    flex-shrink: 0;
    margin-top: 1px;
  }

  .audit-worker-info {
    flex: 1;
    min-width: 0;
  }

  .audit-worker-skill {
    font-size: 10px;
    color: var(--text-1);
    display: flex;
    align-items: center;
    gap: 4px;
  }

  .audit-phase-badge {
    font-size: 8px;
    padding: 0 4px;
    background: rgba(99, 102, 241, 0.1);
    border-radius: 4px;
    color: #818cf8;
  }

  .audit-status-badge {
    font-size: 8px;
    font-weight: 500;
  }

  .audit-worker-meta {
    display: flex;
    gap: 6px;
    font-size: 9px;
    color: var(--text-3);
    margin-top: 1px;
  }

  .audit-worker-task {
    font-size: 9px;
    color: var(--text-3);
    margin-top: 2px;
    line-height: 1.3;
    opacity: 0.7;
  }

  .audit-worker-error {
    font-size: 9px;
    color: var(--negative, #D4808F);
    margin-top: 2px;
    line-height: 1.3;
  }

  .audit-analyze-btn {
    width: 100%;
    padding: 8px 12px;
    background: rgba(99, 102, 241, 0.12);
    border: 1px solid rgba(99, 102, 241, 0.25);
    border-radius: 8px;
    color: #818cf8;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    transition: background 0.15s;
  }
  .audit-analyze-btn:hover:not(:disabled) {
    background: rgba(99, 102, 241, 0.2);
  }
  .audit-analyze-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  /* Analysis live indicator */
  .audit-analysis-live {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
    padding: 8px 10px;
    background: rgba(99, 102, 241, 0.05);
    border: 1px solid rgba(99, 102, 241, 0.12);
    border-radius: 6px;
    font-size: 10px;
    color: var(--text-3);
  }
  .audit-analysis-pulse {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #818cf8;
    animation: audit-pulse 1.4s ease-in-out infinite;
    flex-shrink: 0;
  }
  @keyframes audit-pulse {
    0%, 100% { opacity: 0.3; transform: scale(0.8); }
    50% { opacity: 1; transform: scale(1.2); }
  }

  /* Critique report */
  .audit-critique-report {
    margin-top: 10px;
    border: 1px solid rgba(107, 199, 133, 0.15);
    border-radius: 8px;
    overflow: hidden;
    background: rgba(107, 199, 133, 0.03);
  }
  .audit-critique-report.audit-critique-failed {
    border-color: rgba(212, 128, 143, 0.15);
    background: rgba(212, 128, 143, 0.03);
  }
  .audit-critique-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 10px;
    background: rgba(255, 255, 255, 0.02);
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .audit-critique-badge {
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--positive, #6BC785);
  }
  .audit-critique-badge.is-failed {
    color: var(--negative, #D4808F);
  }
  .audit-critique-time {
    font-size: 9px;
    color: var(--text-3);
  }
  .audit-critique-body {
    padding: 10px 12px;
    font-size: 11px;
    line-height: 1.55;
    color: var(--text-2);
    max-height: 400px;
    overflow-y: auto;
  }
  .audit-critique-body :global(h1),
  .audit-critique-body :global(h2),
  .audit-critique-body :global(h3) {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-1);
    margin: 10px 0 4px;
  }
  .audit-critique-body :global(h1:first-child),
  .audit-critique-body :global(h2:first-child),
  .audit-critique-body :global(h3:first-child) {
    margin-top: 0;
  }
  .audit-critique-body :global(ul),
  .audit-critique-body :global(ol) {
    padding-left: 16px;
    margin: 4px 0;
  }
  .audit-critique-body :global(li) {
    margin-bottom: 3px;
  }
  .audit-critique-body :global(p) {
    margin: 4px 0;
  }
  .audit-critique-body :global(code) {
    font-size: 10px;
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 4px;
    border-radius: 3px;
  }
  .audit-critique-body :global(strong) {
    color: var(--text-1);
  }

  .audit-proposal-card {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 8px;
  }

  .audit-proposal-issue {
    font-size: 11px;
    color: var(--text-2);
    margin-bottom: 6px;
    line-height: 1.4;
  }

  .audit-proposal-fix {
    font-size: 10px;
    color: var(--text-3);
    margin-bottom: 8px;
    line-height: 1.4;
    padding-left: 8px;
    border-left: 2px solid rgba(99, 102, 241, 0.3);
  }

  .audit-proposal-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .audit-apply-btn {
    padding: 4px 12px;
    background: rgba(99, 102, 241, 0.15);
    border: 1px solid rgba(99, 102, 241, 0.3);
    border-radius: 6px;
    color: #818cf8;
    font-size: 10px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s;
  }
  .audit-apply-btn:hover:not(:disabled) {
    background: rgba(99, 102, 241, 0.25);
  }
  .audit-apply-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .audit-proposal-result {
    font-size: 10px;
    font-weight: 500;
  }
  .audit-proposal-result.success {
    color: var(--positive, #6BC785);
  }
  .audit-proposal-result.failure {
    color: var(--negative, #D4808F);
  }

  /* Eval Button */
  .audit-eval-btn {
    padding: 4px 12px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    color: var(--text-3);
    font-size: 10px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .audit-eval-btn:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.08);
    color: var(--text-2);
  }
  .audit-eval-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .audit-eval-warning {
    font-size: 9px;
    color: var(--negative, #D4808F);
    font-weight: 500;
  }

  /* Eval Results Panel */
  .audit-eval-panel {
    margin-top: 8px;
    padding: 8px 10px;
    background: rgba(255, 255, 255, 0.02);
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.04);
  }

  .audit-eval-empty {
    font-size: 10px;
    color: var(--text-3);
    font-style: italic;
  }

  .audit-eval-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 0;
    font-size: 10px;
    line-height: 1.3;
  }
  .audit-eval-row + .audit-eval-row {
    border-top: 1px solid rgba(255, 255, 255, 0.03);
  }

  .audit-eval-id {
    color: var(--text-3);
    font-family: 'JetBrains Mono', 'SF Mono', monospace;
    font-size: 9px;
    min-width: 36px;
  }

  .audit-eval-task {
    color: var(--text-2);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .audit-eval-score {
    font-weight: 600;
    font-size: 10px;
    min-width: 32px;
    text-align: right;
  }

  .audit-eval-detail {
    color: var(--text-3);
    font-size: 9px;
    min-width: 80px;
  }

  .audit-eval-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 6px;
    padding-top: 6px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    font-size: 10px;
    color: var(--text-2);
  }

  .audit-eval-rec {
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .audit-eval-rec.rec-apply {
    background: rgba(107, 199, 133, 0.15);
    color: var(--positive, #6BC785);
  }
  .audit-eval-rec.rec-caution {
    background: color-mix(in srgb, var(--thread-accent, #57CFA0) 15%, transparent);
    color: var(--thread-accent, #57CFA0);
  }
  .audit-eval-rec.rec-reject {
    background: rgba(212, 128, 143, 0.15);
    color: var(--negative, #D4808F);
  }

  .tab-empty,
  .activity-time,
  .activity-meta,
  .activity-trace-note,
  .details-empty,
  .details-meta,
  .link-meta,
  .metrics-label,
  .metrics-row .metrics-value,
  .audit-card-label,
  .audit-card-sub,
  .audit-run-meta,
  .audit-run-time,
  .audit-worker-meta,
  .audit-bar-count,
  .audit-legend-item,
  .audit-critique-time,
  .audit-eval-empty,
  .audit-eval-id,
  .audit-eval-detail {
    color: var(--panel-utility-muted-text);
  }

  .activity-title,
  .details-desc,
  .details-meta strong,
  .link-item,
  .metrics-value,
  .metrics-row,
  .audit-card-value,
  .audit-run-skill,
  .audit-worker-skill,
  .audit-worker-task,
  .audit-critique-body,
  .audit-proposal-issue,
  .audit-proposal-fix,
  .audit-eval-task,
  .audit-eval-summary {
    color: var(--panel-utility-primary-text);
  }

  .link-item:hover {
    color: var(--panel-utility-primary-hover-text);
  }

  .details-section,
  .audit-card,
  .audit-efficiency,
  .audit-run-row,
  .audit-workers-panel,
  .audit-worker-row,
  .audit-critique-report,
  .audit-proposal-card,
  .audit-eval-panel {
    border-color: var(--panel-utility-card-border);
    background: var(--panel-utility-card-background);
  }

  .link-item,
  .activity-list-item,
  .metrics-section,
  .metrics-row,
  .audit-eval-row + .audit-eval-row,
  .audit-eval-summary {
    border-color: var(--panel-utility-divider-border);
  }

  .link-dot {
    box-shadow: var(--panel-utility-link-dot-shadow);
  }

  .audit-eval-btn {
    border-color: var(--panel-utility-eval-button-border);
    background: var(--panel-utility-eval-button-background);
    color: var(--panel-utility-eval-button-text);
  }

  .audit-eval-btn:hover:not(:disabled) {
    background: var(--panel-utility-eval-button-hover-background);
    color: var(--panel-utility-eval-button-hover-text);
  }

</style>
