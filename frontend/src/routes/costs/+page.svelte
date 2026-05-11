<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { api } from '$lib/api/client';
  import { ui } from '$lib/stores/ui.svelte';
  import { relativeTimeAgo } from '$lib/utils/datetime';

  let data = $state<any>(null);
  let loading = $state(true);
  let dailyCanvas = $state<HTMLCanvasElement | null>(null);

  onMount(async () => {
    try {
      data = await api.listCosts();
    } catch (err: any) {
      ui.toast(err.detail || 'Failed to load costs', 'error');
    } finally {
      loading = false;
    }

    await tick();
    drawDailyChart();
  });

  function fmtCost(v: any): string {
    const n = parseFloat(v) || 0;
    if (n === 0) return '$0.00';
    if (n < 0.01) return `$${n.toFixed(4)}`;
    if (n < 1) return `$${n.toFixed(3)}`;
    return `$${n.toFixed(2)}`;
  }

  function fmtTokens(v: any): string {
    const n = parseInt(v) || 0;
    if (n === 0) return '0';
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return n.toLocaleString();
  }

  function shortModel(m: string): string {
    if (!m) return 'unknown';
    return m.replace('anthropic/', '').replace('claude-', '').replace(/-\d{8}$/, '');
  }

  function modelColor(m: string): string {
    const s = shortModel(m);
    if (s.includes('pro')) return 'var(--purple)';
    if (s.includes('mini') || s.includes('nano')) return 'var(--cyan)';
    if (s.includes('gpt-') || s.includes('claude-')) return 'var(--accent)';
    return 'var(--text-3)';
  }

  function timeAgo(ts: string): string {
    return relativeTimeAgo(ts) || 'just now';
  }

  function drawDailyChart() {
    const canvas = dailyCanvas;
    if (!canvas || !data?.daily?.length) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);
    const W = rect.width;
    const H = rect.height;

    const pad = { top: 20, right: 20, bottom: 30, left: 50 };
    const cW = W - pad.left - pad.right;
    const cH = H - pad.top - pad.bottom;

    // Last 30 days of data
    const dailySlice = data.daily.slice(-30);
    if (dailySlice.length === 0) return;

    const maxCost = Math.max(...dailySlice.map((d: any) => d.cost), 0.001);

    ctx.clearRect(0, 0, W, H);

    // Grid lines
    ctx.strokeStyle = 'rgba(255,255,255,0.06)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + (cH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(W - pad.right, y);
      ctx.stroke();
    }

    // Y-axis labels
    ctx.fillStyle = 'rgba(255,255,255,0.35)';
    ctx.font = '10px system-ui';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const val = maxCost * (1 - i / 4);
      const y = pad.top + (cH / 4) * i;
      ctx.fillText(fmtCost(val), pad.left - 6, y + 3);
    }

    const step = cW / Math.max(dailySlice.length - 1, 1);

    // Area fill
    ctx.beginPath();
    ctx.moveTo(pad.left, pad.top + cH);
    dailySlice.forEach((d: any, i: number) => {
      const x = pad.left + i * step;
      const y = pad.top + cH - (d.cost / maxCost) * cH;
      ctx.lineTo(x, y);
    });
    ctx.lineTo(pad.left + (dailySlice.length - 1) * step, pad.top + cH);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, pad.top, 0, pad.top + cH);
    grad.addColorStop(0, 'rgba(91,141,239,0.2)');
    grad.addColorStop(1, 'rgba(91,141,239,0.01)');
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.strokeStyle = '#5b8def';
    ctx.lineWidth = 2;
    ctx.beginPath();
    dailySlice.forEach((d: any, i: number) => {
      const x = pad.left + i * step;
      const y = pad.top + cH - (d.cost / maxCost) * cH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Dots
    ctx.fillStyle = '#5b8def';
    dailySlice.forEach((d: any, i: number) => {
      const x = pad.left + i * step;
      const y = pad.top + cH - (d.cost / maxCost) * cH;
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    });

    // X-axis: first and last date
    ctx.fillStyle = 'rgba(255,255,255,0.35)';
    ctx.font = '10px system-ui';
    ctx.textAlign = 'left';
    ctx.fillText(
      new Date(dailySlice[0].date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      pad.left, H - 6
    );
    ctx.textAlign = 'right';
    ctx.fillText(
      new Date(dailySlice[dailySlice.length - 1].date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      W - pad.right, H - 6
    );
  }
</script>

<div class="page-header animate-in">
  <h2 class="page-title">Costs</h2>
  {#if data?.summary}
    <p class="page-subtitle">
      {(data.summary.total_runs || 0).toLocaleString()} runs &middot;
      {fmtTokens(data.summary.total_tokens)} tokens
    </p>
  {:else}
    <p class="page-subtitle">Token &amp; cost analytics</p>
  {/if}
</div>

{#if loading}
  <div class="grid grid-4 section">
    {#each Array(4) as _}
      <div class="card"><div class="skeleton skeleton-stat" style="height: 80px;"></div></div>
    {/each}
  </div>
  <div class="card section"><div class="skeleton skeleton-card" style="height: 200px;"></div></div>
{:else if data}
  {@const s = data.summary || {}}
  {@const m = data.month || {}}
  {@const today = new Date()}
  {@const dayOfMonth = today.getDate()}
  {@const dailyAvg = dayOfMonth > 0 ? (parseFloat(m.month_cost) || 0) / dayOfMonth : 0}
  {@const cacheEff = s.total_tokens > 0 ? Math.round((s.total_cache_read / s.total_tokens) * 100) : 0}

  <!-- Tracking coverage notice -->
  {#if s.tracking_coverage !== undefined && s.tracking_coverage < 0.5 && s.total_runs > 0}
    <div class="tracking-warning section animate-in">
      <span style="font-weight: var(--weight-semibold);">Low tracking coverage</span>
      — Only {Math.round(s.tracking_coverage * 100)}% of runs have token data.
      Cost estimates may be incomplete.
    </div>
  {/if}

  <!-- Stat cards -->
  <div class="grid grid-4 section animate-in">
    <div class="card" style="padding: var(--sp-4);">
      <div style="font-size: var(--text-xs); color: var(--text-3); text-transform: uppercase; letter-spacing: var(--tracking-wide); margin-bottom: var(--sp-1);">Month Spend</div>
      <div style="font-size: var(--text-2xl); font-weight: var(--weight-bold); color: var(--positive); font-variant-numeric: tabular-nums;">{fmtCost(m.month_cost)}</div>
      <div style="font-size: var(--text-xs); color: var(--text-3); margin-top: var(--sp-1);">~{fmtCost(dailyAvg)}/day avg</div>
    </div>
    <div class="card" style="padding: var(--sp-4);">
      <div style="font-size: var(--text-xs); color: var(--text-3); text-transform: uppercase; letter-spacing: var(--tracking-wide); margin-bottom: var(--sp-1);">All-Time</div>
      <div style="font-size: var(--text-2xl); font-weight: var(--weight-bold); color: var(--accent); font-variant-numeric: tabular-nums;">{fmtCost(s.total_cost)}</div>
      <div style="font-size: var(--text-xs); color: var(--text-3); margin-top: var(--sp-1);">{(s.total_runs || 0).toLocaleString()} runs</div>
    </div>
    <div class="card" style="padding: var(--sp-4);">
      <div style="font-size: var(--text-xs); color: var(--text-3); text-transform: uppercase; letter-spacing: var(--tracking-wide); margin-bottom: var(--sp-1);">Tokens</div>
      <div style="font-size: var(--text-2xl); font-weight: var(--weight-bold); color: var(--purple); font-variant-numeric: tabular-nums;">{fmtTokens(s.total_tokens)}</div>
      <div style="font-size: var(--text-xs); color: var(--text-3); margin-top: var(--sp-1);">In: {fmtTokens(s.total_input_tokens)} · Out: {fmtTokens(s.total_output_tokens)}</div>
    </div>
    <div class="card" style="padding: var(--sp-4);">
      <div style="font-size: var(--text-xs); color: var(--text-3); text-transform: uppercase; letter-spacing: var(--tracking-wide); margin-bottom: var(--sp-1);">Cache Hit</div>
      <div style="font-size: var(--text-2xl); font-weight: var(--weight-bold); color: var(--cyan); font-variant-numeric: tabular-nums;">{cacheEff}%</div>
      <div style="font-size: var(--text-xs); color: var(--text-3); margin-top: var(--sp-1);">{fmtTokens(s.total_cache_read)} read · {fmtTokens(s.total_cache_write)} write</div>
    </div>
  </div>

  <!-- Daily spend chart -->
  {#if data.daily?.length}
    <div class="card section animate-in stagger-1">
      <div class="card-header">
        <span class="card-title">Daily Spend</span>
        <span style="font-size: var(--text-xs); color: var(--text-3);">Last 30 days</span>
      </div>
      <div style="padding: var(--sp-3);">
        <canvas bind:this={dailyCanvas} style="width: 100%; height: 200px;"></canvas>
      </div>
    </div>
  {/if}

  <!-- Top spending ideas -->
  {#if data.top_ideas?.length}
    {@const topMax = Math.max(...data.top_ideas.map((i: any) => i.cost), 0.001)}
    <div class="card section animate-in stagger-2">
      <div class="card-header">
        <span class="card-title">Top Spending Ideas</span>
        <span style="font-size: var(--text-xs); color: var(--text-3);">{data.top_ideas.length} ideas</span>
      </div>
      <div style="display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-3);">
        {#each data.top_ideas as idea, idx}
          {@const pct = topMax > 0 ? (idea.cost / topMax) * 100 : 0}
          <div style="display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-2); border-radius: var(--radius-md); background: var(--bg-3);">
            <span style="font-size: var(--text-xs); color: var(--text-3); min-width: 18px; text-align: right; font-variant-numeric: tabular-nums;">#{idx + 1}</span>
            <div style="flex: 1; min-width: 0;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sp-1);">
                <code style="font-size: var(--text-sm); color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px;">{idea.idea_id}</code>
                <div style="display: flex; align-items: center; gap: var(--sp-3); font-size: var(--text-xs); color: var(--text-3); flex-shrink: 0;">
                  <span>{idea.runs} runs</span>
                  <span>{fmtTokens(idea.tokens)} tok</span>
                  <strong style="color: var(--accent);">{fmtCost(idea.cost)}</strong>
                </div>
              </div>
              <div style="height: 4px; background: var(--bg-4); border-radius: var(--radius-full); overflow: hidden;">
                <div style="height: 100%; width: {pct}%; background: var(--accent); border-radius: var(--radius-full);"></div>
              </div>
            </div>
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <!-- Model breakdown -->
  {#if data.by_model?.length}
    {@const maxCost = Math.max(...data.by_model.map((m: any) => parseFloat(m.cost) || 0), 0.001)}
    <div class="card section animate-in stagger-3">
      <div class="card-header"><span class="card-title">Model Mix</span></div>
      <div style="display: flex; flex-direction: column; gap: var(--sp-3); padding: var(--sp-3);">
        {#each data.by_model as model}
          {@const cost = parseFloat(model.cost) || 0}
          {@const pct = maxCost > 0 ? (cost / maxCost) * 100 : 0}
          {@const color = modelColor(model.model)}
          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: var(--sp-1);">
              <code style="font-size: var(--text-sm); color: var(--text-1);">{shortModel(model.model)}</code>
              <div style="display: flex; align-items: center; gap: var(--sp-3); font-size: var(--text-xs); color: var(--text-3);">
                <span>{(model.runs || 0).toLocaleString()} runs</span>
                <span>{fmtTokens((model.input_tokens || 0) + (model.output_tokens || 0))} tok</span>
                <strong style="color: {color};">{fmtCost(model.cost)}</strong>
              </div>
            </div>
            <div style="height: 6px; background: var(--bg-4); border-radius: var(--radius-full); overflow: hidden;">
              <div style="height: 100%; width: {pct}%; background: {color}; border-radius: var(--radius-full);"></div>
            </div>
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <!-- Per-run cost table -->
  {#if data.runs?.length}
    <div class="card section animate-in stagger-4">
      <div class="card-header">
        <span class="card-title">Recent Runs</span>
        <span style="font-size: var(--text-xs); color: var(--text-3);">{data.runs.length} entries</span>
      </div>
      <div style="overflow-x: auto;">
        <table class="data-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Skill</th>
              <th>Model</th>
              <th>Tokens</th>
              <th>Cost</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {#each data.runs as d}
              <tr>
                <td style="white-space: nowrap;">{d.timestamp ? timeAgo(d.timestamp) : '—'}</td>
                <td><code style="font-size: var(--text-xs);">{d.skill || '—'}</code></td>
                <td style="font-size: var(--text-xs); color: {modelColor(d.model)};">{shortModel(d.model)}</td>
                <td style="font-variant-numeric: tabular-nums;">{fmtTokens((d.input_tokens || 0) + (d.output_tokens || 0))}</td>
                <td style="font-variant-numeric: tabular-nums; font-weight: var(--weight-medium);">{fmtCost(d.cost)}</td>
                <td>
                  <span class="badge badge-{d.status === 'completed' ? 'positive' : d.status === 'error' ? 'negative' : 'neutral'}">
                    {d.status || '—'}
                  </span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}

  <!-- Skill efficiency -->
  {#if data.by_skill?.length}
    <div class="card section animate-in stagger-5">
      <div class="card-header"><span class="card-title">Skill Efficiency</span></div>
      <div style="display: flex; flex-direction: column; gap: var(--sp-2); padding: var(--sp-3);">
        {#each data.by_skill as skill}
          {@const total = (skill.successes || 0) + (skill.failures || 0)}
          {@const rate = total > 0 ? Math.round((skill.successes / total) * 100) : 0}
          {@const rateColor = rate >= 80 ? 'var(--positive)' : rate >= 50 ? 'var(--warning)' : 'var(--negative)'}
          <div style="display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-2); border-radius: var(--radius-md); background: var(--bg-3);">
            <code style="font-size: var(--text-sm); color: var(--text-1); min-width: 100px;">{skill.skill || '—'}</code>
            <div style="flex: 1; display: flex; align-items: center; gap: var(--sp-2);">
              <span style="font-size: var(--text-xs); color: var(--text-3);">{(skill.runs || 0).toLocaleString()} runs</span>
              <span style="font-size: var(--text-xs); color: {rateColor}; font-weight: var(--weight-semibold);">{rate}%</span>
            </div>
            <div style="display: flex; align-items: center; gap: var(--sp-3); font-size: var(--text-xs);">
              <span style="color: var(--text-3);">{fmtTokens(skill.tokens)} tok</span>
              <strong style="color: var(--text-1);">{fmtCost(skill.cost)}</strong>
            </div>
          </div>
        {/each}
      </div>
    </div>
  {/if}
{:else}
  <div class="card" style="padding: var(--sp-8); color: var(--text-3);">
    No cost data available.
  </div>
{/if}

<style>
  .tracking-warning {
    padding: var(--sp-3) var(--sp-4);
    background: color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 10%, transparent);
    border: 1px solid color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 30%, transparent);
    border-radius: var(--radius-md);
    color: #57CFA0;
    font-size: var(--text-sm);
  }
</style>
