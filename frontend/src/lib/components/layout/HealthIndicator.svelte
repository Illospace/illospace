<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';

  type ServiceStatus = 'ok' | 'degraded' | 'down' | 'loading';
  type ServiceSnapshot = { label: string; status: ServiceStatus };

  let db: ServiceStatus = $state('loading');
  let embed: ServiceStatus = $state('loading');
  let ollama: ServiceStatus = $state('loading');
  let brain: ServiceStatus = $state('loading');

  async function poll() {
    try {
      const [health, deep] = await Promise.all([
        api.health().catch(() => null),
        api.healthDeep().catch(() => null),
      ]);
      if (health || deep) {
        if (health) parseHealth(health);
        if (deep) parseDeepHealth(deep);
        return;
      }
    } catch { /* fall through */ }

    // Last resort: the full system endpoint is heavier, so only use it if
    // the health-tier endpoints are unavailable.
    try {
      const info = await api.systemInfo();
      if (info) {
        parseSystemInfo(info);
        return;
      }
    } catch { /* fall through */ }

    db = 'down'; embed = 'down'; ollama = 'down'; brain = 'down';
  }

  function parseSystemInfo(info: any): void {
    // Database
    const dbInfo = info?.database;
    db = dbInfo?.status === 'ok' ? 'ok'
      : dbInfo?.status === 'error' ? 'down'
      : dbInfo ? 'degraded' : 'down';

    // Embedding server — check the embedding worker specifically
    const embInfo = info?.embedding;
    const embWorker = embInfo?.server_health?.workers?.embedding;
    if (embWorker?.status === 'ready') embed = 'ok';
    else if (embInfo?.status === 'ok' || embInfo?.status === 'degraded') embed = 'ok';  // degraded = LLM down, embed still works
    else embed = 'down';

    // Ollama — infer from GPU server workers or GPU hardware
    const workers = embInfo?.server_health?.workers;
    const gpuHw = info?.gpu;
    if (workers?.llm?.status === 'ready') ollama = 'ok';
    else if (workers?.llm?.status) ollama = 'degraded';
    else if (gpuHw?.status === 'ok') ollama = 'ok';
    else ollama = 'down';

    // Brain — composite
    brain = db === 'ok' && embed === 'ok' ? 'ok'
      : db === 'ok' ? 'degraded'
      : 'down';
  }

  function parseHealth(health: any): void {
    // /api/health returns { status, database, memory_count, ... }
    db = health?.database === 'connected' ? 'ok'
      : health?.database === 'error' ? 'down'
      : health?.status === 'ok' ? 'ok' : 'down';

    // Can't determine embed/ollama from basic health
    embed = health?.status === 'ok' ? 'loading' : 'down';
    ollama = health?.status === 'ok' ? 'loading' : 'down';
    brain = db === 'ok' ? 'degraded' : 'down';
  }

  function parseDeepHealth(deep: any): void {
    const embedding = deep?.checks?.embedding;
    const embeddingStatus = embedding?.status;
    embed = embeddingStatus === 'ok' ? 'ok'
      : embeddingStatus === 'degraded' || embeddingStatus === 'skipped' ? 'degraded'
      : embedding ? 'down' : embed;

    const workers = embedding?.details?.server?.workers;
    const llmStatus = workers?.llm?.status;
    if (llmStatus === 'ready') ollama = 'ok';
    else if (llmStatus) ollama = 'degraded';
    else if (deep?.checks?.providers?.status === 'ok') ollama = 'ok';
    else if (deep?.checks?.providers) ollama = 'degraded';

    brain = db === 'ok' && embed === 'ok' && deep?.ok !== false ? 'ok'
      : db === 'down' || deep?.status === 'unhealthy' ? 'down'
      : 'degraded';
  }

  onMount(() => {
    poll();
    const interval = setInterval(poll, 30_000);
    return () => clearInterval(interval);
  });

  const statusColor: Record<ServiceStatus, string> = {
    ok: 'var(--green, #22c55e)',
    degraded: 'var(--yellow, #eab308)',
    down: 'var(--red, #ef4444)',
    loading: 'var(--text-3, #666)',
  };

  const services: ServiceSnapshot[] = $derived([
    { label: 'DB', status: db },
    { label: 'Embed', status: embed },
    { label: 'Ollama', status: ollama },
    { label: 'Brain', status: brain },
  ]);

  const downServices = $derived(services.filter(s => s.status === 'down'));
  const degradedServices = $derived(services.filter(s => s.status === 'degraded'));

  const overall: ServiceStatus = $derived(
    services.some(s => s.status === 'down') ? 'down'
    : services.some(s => s.status === 'degraded') ? 'degraded'
    : services.some(s => s.status === 'loading') ? 'loading'
    : 'ok'
  );

  const label = $derived(
    overall === 'loading' ? 'checking...'
    : overall === 'ok' ? 'all systems go'
    : overall === 'down' && downServices.length <= 2
      ? downServices.map(s => s.label).join(', ') + ' down'
    : overall === 'degraded' && degradedServices.length <= 2
      ? degradedServices.map(s => s.label).join(', ') + ' degraded'
    : overall === 'down' ? 'systems down'
    : 'degraded'
  );
</script>

<a href="/system" class="health-indicator" title={services.map(s => `${s.label}: ${s.status}`).join(' · ')}>
  <div class="health-dots">
    {#each services as svc}
      <span
        class="health-dot"
        style="background: {statusColor[svc.status]}"
      ></span>
    {/each}
  </div>
  <span class="health-label">{label}</span>
</a>

<style>
  .health-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    margin-bottom: 8px;
    border-radius: var(--radius-md, 6px);
    background: var(--bg-2, #1a1d2e);
    text-decoration: none;
    cursor: pointer;
    transition: background 0.15s;
  }

  .health-indicator:hover {
    background: var(--bg-3, #252838);
  }

  .health-dots {
    display: flex;
    gap: 4px;
    align-items: center;
  }

  .health-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
    transition: background 0.3s;
  }

  .health-label {
    font-size: 11px;
    color: var(--text-3, #888);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
