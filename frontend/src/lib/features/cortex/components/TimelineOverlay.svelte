<script lang="ts">
  import { cortex } from '$lib/stores/cortex.svelte';
  import { timelineData } from '$lib/features/cortex/api/cortexApi';
  import { onMount } from 'svelte';

  let { visible = false }: { visible: boolean } = $props();

  interface TimelineEntry {
    idea_id: string;
    title: string;
    created_at: string;
    events: { timestamp: string; status: string }[];
  }

  let data = $state<TimelineEntry[]>([]);
  let loading = $state(false);
  let scrubPosition = $state(1.0); // 0..1, 1 = live
  let isLive = $derived(scrubPosition >= 0.99);
  let trackEl: HTMLDivElement | undefined = $state();
  let dragging = $state(false);
  const TIMELINE_IDEA_LIMIT = 250;

  // Time range
  let minTime = $derived.by(() => {
    if (data.length === 0) return Date.now() - 14 * 86400_000;
    return Math.min(...data.map((d) => new Date(d.created_at).getTime()));
  });
  let maxTime = $derived(Date.now());
  let scrubTime = $derived(minTime + (maxTime - minTime) * scrubPosition);

  const STATUS_COLORS: Record<string, string> = {
    idle: '#57CFA0',
    working: '#E3AA54',
    done: '#57CFA0',
  };

  onMount(async () => {
    if (!visible) return;
    loading = true;
    try {
      data = normalizeTimelinePayload(await timelineData(TIMELINE_IDEA_LIMIT));
    } catch {
      data = [];
    } finally {
      loading = false;
    }
  });

  $effect(() => {
    if (visible && data.length === 0 && !loading) {
      loading = true;
      timelineData(TIMELINE_IDEA_LIMIT)
        .then((d) => { data = normalizeTimelinePayload(d); loading = false; })
        .catch(() => { loading = false; });
    }
  });

  function normalizeTimelinePayload(payload: any): TimelineEntry[] {
    const ideas = Array.isArray(payload) ? payload : (Array.isArray(payload?.ideas) ? payload.ideas : []);
    return ideas
      .map((idea: any) => ({
        idea_id: String(idea.idea_id ?? idea.id ?? ''),
        title: String(idea.title ?? idea.display_title ?? 'Untitled'),
        created_at: String(idea.created_at ?? ''),
        events: (Array.isArray(idea.events) ? idea.events : (idea.transitions ?? []))
          .map((event: any) => ({
            timestamp: String(event.timestamp ?? event.at ?? event.changed_at ?? ''),
            status: String(event.status ?? event.to_state ?? 'idle'),
          }))
          .filter((event: any) => event.timestamp),
      }))
      .filter((entry: TimelineEntry) => entry.idea_id && entry.created_at);
  }

  function handlePointerDown(e: PointerEvent) {
    dragging = true;
    updateScrub(e);
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }

  function handlePointerMove(e: PointerEvent) {
    if (!dragging) return;
    updateScrub(e);
  }

  function handlePointerUp() {
    dragging = false;
  }

  function updateScrub(e: PointerEvent) {
    if (!trackEl) return;
    const rect = trackEl.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    scrubPosition = x;
  }

  function goLive() {
    scrubPosition = 1.0;
  }

  function formatDate(ts: number) {
    return new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function formatScrubDate(ts: number) {
    return new Date(ts).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  // Get idea status at scrub time
  function statusAtTime(entry: TimelineEntry): string | null {
    const t = scrubTime;
    if (new Date(entry.created_at).getTime() > t) return null; // not born yet
    let status = 'idle';
    for (const ev of entry.events) {
      if (new Date(ev.timestamp).getTime() <= t) {
        status = ev.status;
      }
    }
    return status;
  }

  // Compute segment positions for the track
  function trackSegments(entry: TimelineEntry) {
    const range = maxTime - minTime || 1;
    const segs: { left: number; width: number; color: string }[] = [];
    const born = new Date(entry.created_at).getTime();
    let prevTime = born;
    let prevStatus = 'idle';

    for (const ev of entry.events) {
      const evTime = new Date(ev.timestamp).getTime();
      segs.push({
        left: (prevTime - minTime) / range,
        width: (evTime - prevTime) / range,
        color: STATUS_COLORS[prevStatus] ?? '#888',
      });
      prevTime = evTime;
      prevStatus = ev.status;
    }
    // Final segment to now
    segs.push({
      left: (prevTime - minTime) / range,
      width: (maxTime - prevTime) / range,
      color: STATUS_COLORS[prevStatus] ?? '#888',
    });
    return segs;
  }
</script>

{#if visible}
  <div class="timeline-bar">
    <div class="timeline-header">
      <span class="timeline-label">
        {isLive ? 'Live' : formatScrubDate(scrubTime)}
      </span>
      {#if !isLive}
        <button class="btn-live" onclick={goLive}>Go Live</button>
      {/if}
    </div>

    <!-- Track -->
    <div
      class="timeline-track"
      bind:this={trackEl}
      onpointerdown={handlePointerDown}
      onpointermove={handlePointerMove}
      onpointerup={handlePointerUp}
      role="slider"
      aria-valuenow={Math.round(scrubPosition * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      tabindex="0"
    >
      <!-- Idea segments -->
      {#each data.slice(0, 30) as entry, row (entry.idea_id)}
        {#each trackSegments(entry) as seg}
          <div
            class="track-seg"
            style="left: {seg.left * 100}%; width: {seg.width * 100}%; top: {row * 4 + 2}px; background: {seg.color};"
          ></div>
        {/each}
      {/each}

      <!-- Birth dots -->
      {#each data as entry (entry.idea_id)}
        {@const pos = (new Date(entry.created_at).getTime() - minTime) / (maxTime - minTime || 1)}
        <div class="birth-dot" style="left: {pos * 100}%;" title={entry.title}></div>
      {/each}

      <!-- Scrub handle -->
      <div class="scrub-handle" style="left: {scrubPosition * 100}%;"></div>

      <!-- Date ticks -->
      <div class="date-tick" style="left: 0%;">{formatDate(minTime)}</div>
      <div class="date-tick" style="left: 50%;">{formatDate(minTime + (maxTime - minTime) / 2)}</div>
      <div class="date-tick" style="left: 100%; transform: translateX(-100%);">{formatDate(maxTime)}</div>
    </div>
  </div>
{/if}

<style>
  .timeline-bar {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 12;
    background: rgba(8, 11, 18, 0.85);
    backdrop-filter: blur(12px);
    border-top: 1px solid var(--border-1);
    padding: 6px 16px 10px;
  }

  .timeline-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 4px;
  }

  .timeline-label {
    font-size: var(--text-xs);
    color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }

  .btn-live {
    font-size: var(--text-xs);
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--positive-surface);
    color: var(--positive);
    border: 1px solid var(--positive-border);
    cursor: pointer;
  }
  .btn-live:hover { background: var(--positive-glow); }

  .timeline-track {
    position: relative;
    height: 140px;
    background: var(--bg-2);
    border-radius: 6px;
    cursor: pointer;
    overflow: hidden;
    touch-action: none;
    user-select: none;
  }

  .track-seg {
    position: absolute;
    height: 3px;
    border-radius: 1px;
    opacity: 0.7;
    min-width: 1px;
  }

  .birth-dot {
    position: absolute;
    bottom: 20px;
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--accent);
    transform: translateX(-50%);
  }

  .scrub-handle {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 2px;
    background: var(--accent);
    transform: translateX(-50%);
    pointer-events: none;
    box-shadow: 0 0 6px var(--accent-glow);
  }
  .scrub-handle::after {
    content: '';
    position: absolute;
    top: -2px;
    left: 50%;
    transform: translateX(-50%);
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent);
    border: 2px solid var(--bg-2);
  }

  .date-tick {
    position: absolute;
    bottom: 2px;
    font-size: 9px;
    color: var(--text-3);
    pointer-events: none;
  }
</style>
