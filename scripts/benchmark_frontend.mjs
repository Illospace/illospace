#!/usr/bin/env node
/**
 * Benchmark the Cortex frontend in a real Chrome page with deterministic API
 * responses. This isolates browser/render/orchestration cost from backend
 * variance while still preserving the app's normal fetch and navigation flow.
 */

import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { gzipSync } from 'node:zlib';

const DEFAULT_BASE_URL = 'http://127.0.0.1:5178';
const DEFAULT_CHROME_PATHS = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean);

function modalReadyExpression(id, title, readySelector, loadingSelector = null) {
  return `(() => {
    const modalTitle = document.querySelector('#workspace-page-modal-title')?.textContent?.trim();
    const expectedModal = new URLSearchParams(location.search).get('modal');
    return Boolean(
      expectedModal === '${id}' &&
      modalTitle === '${title}' &&
      document.querySelector('.workspace-page-modal__surface[role="dialog"]') &&
      document.querySelector('${readySelector}') &&
      ${loadingSelector ? `!document.querySelector('${loadingSelector}')` : 'true'}
    );
  })()`;
}

const WORKSPACE_READY_EXPRESSION = `(() => {
  const body = document.body?.textContent || '';
  return Boolean(
    document.querySelector('.cortex-page') &&
    document.querySelector('.workspace-composer-shell') &&
    !document.querySelector('.loading-overlay') &&
    body.includes('Benchmark thread 1')
  );
})()`;

const THREAD_READY_EXPRESSION = `(() => {
  const body = document.body?.textContent || '';
  return Boolean(
    document.querySelector('.thread-stage-shell .workspace-stage-shell.ready') &&
    document.querySelector('[data-cortex-thread-column="main"]') &&
    !body.includes('Loading thread...') &&
    body.includes('Benchmark assistant reply')
  );
})()`;

const VAULT_PANE_READY_EXPRESSION = `(() => Boolean(
  document.querySelector('.cortex-thread-stage-right-dock[data-dock-state="vault"]') &&
  document.querySelector('.right-dock-content[data-active-tab="vault"] .vault-list') &&
  !document.querySelector('.right-dock-content[data-active-tab="vault"] .vault-row-skeleton')
))()`;

const VAULT_PANE_MOUNTED_EXPRESSION = `(() => Boolean(
  document.querySelector('.cortex-thread-stage-right-dock[data-dock-state="vault"]') &&
  document.querySelector('.right-dock-content[data-active-tab="vault"] .vault-constellation-frame') &&
  !document.querySelector('.right-dock-content[data-active-tab="vault"] .thread-lazy-pane-state')
))()`;

const THREAD_STAGE_PREWARM_OBSERVATION_MS = 2200;
const RARE_PANE_OPEN_BUDGET_MS = 250;
const DIRECT_THREAD_STARTUP_CALL_BUDGET = 4;
const DIRECT_THREAD_STARTUP_PAYLOAD_BUDGET_KB = 35.5;
const DIRECT_ROUTE_P50_GAP_BUDGET_PCT = 5;
const MAX_REGRESSION_PCT = 5;
const THREAD_HISTORY_WINDOW_SIZE = 200;
const THREAD_PAGE_RAW_BUDGET_BYTES = 180 * 1024;
const THREAD_PAGE_FETCH_BUDGET_MS = 250;
const THREAD_STARTUP_PAYLOAD_REDUCTION_TARGET_PCT = 75;
const THREAD_HISTORY_REVEAL_BUDGET_MS = 150;
const THREAD_HISTORY_VIEWPORT_DRIFT_BUDGET_PX = 2;
const STRESS_READY_IMPROVEMENT_TARGET_PCT = 10;
const STRESS_DOM_NODE_BUDGET = 4000;
const STRESS_TASK_P50_BUDGET_MS = 450;
const STRESS_LONG_TASK_P95_BUDGET_MS = 250;
const WORKSPACE_IDLE_WAKE_BUDGET_MS = 34;
const APP_ASSET_RESOURCE_TYPES = new Set(['Script', 'Stylesheet']);
const VITE_CLIENT_MANIFEST_URL = new URL(
  '../frontend/.svelte-kit/output/client/.vite/manifest.json',
  import.meta.url,
);

const SCENARIOS = {
  workspace: {
    name: 'cortex-workspace',
    path: '/cortex',
    readyExpression: WORKSPACE_READY_EXPRESSION,
  },
  thread: {
    name: 'cortex-thread-direct',
    path: '/cortex?idea=idea-1',
    directThread: true,
    ideaId: 'idea-1',
    readyExpression: THREAD_READY_EXPRESSION,
  },
  threadCanonical: {
    name: 'cortex-thread-direct-canonical',
    path: '/threads/idea-1',
    directThread: true,
    ideaId: 'idea-1',
    readyExpression: THREAD_READY_EXPRESSION,
  },
  cycles: {
    name: 'cortex-modal-cycles',
    path: '/cortex?modal=cycles',
    modalId: 'cycles',
    readyExpression: modalReadyExpression('cycles', 'Cycles', '.workspace-page-modal__body .cycle-list', '.cycle-row-skeleton'),
  },
  skills: {
    name: 'cortex-modal-skills',
    path: '/cortex?modal=skills',
    modalId: 'skills',
    readyExpression: modalReadyExpression('skills', 'Skills', '.workspace-page-modal__body .skill-list', '.skill-row-skeleton'),
  },
  team: {
    name: 'cortex-modal-team',
    path: '/cortex?modal=team',
    modalId: 'team',
    readyExpression: modalReadyExpression('team', 'Team', '.workspace-page-modal__body .team-member-list', '[aria-label="Team loading"]'),
  },
  vault: {
    name: 'cortex-modal-vault',
    path: '/cortex?modal=vault',
    modalId: 'vault',
    readyExpression: modalReadyExpression('vault', 'Vault', '.workspace-page-modal__body .vault-list', '.vault-row-skeleton'),
  },
  system: {
    name: 'cortex-modal-system',
    path: '/cortex?modal=system',
    modalId: 'system',
    readyExpression: modalReadyExpression('system', 'AI Runtime', '.workspace-page-modal__body .runtime-config-layout', '.system-loading'),
  },
};

const ALL_SCENARIO_KEYS = Object.keys(SCENARIOS);

function parseArgs(argv) {
  const options = {
    baseUrl: DEFAULT_BASE_URL,
    runs: 5,
    warmups: 1,
    scenarios: ALL_SCENARIO_KEYS,
    ideas: 120,
    connections: 240,
    streamItems: 80,
    streamContract: 'paged',
    apiLatencyMs: 0,
    sidecarLatencyMs: null,
    timeoutMs: 20000,
    chromePath: null,
    phase: 'current',
    out: null,
    compare: null,
    allowUnknownApi: false,
    json: false,
    keepChromeProfile: false,
    manifest: null,
    workspaceIdle: false,
    idleSettleMs: 10_000,
    idleWindowMs: 10_000,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      i += 1;
      if (i >= argv.length) throw new Error(`Missing value for ${arg}`);
      return argv[i];
    };

    if (arg === '--base-url') options.baseUrl = next();
    else if (arg === '--runs') options.runs = Number(next());
    else if (arg === '--warmups') options.warmups = Number(next());
    else if (arg === '--scenario') {
      const value = next();
      options.scenarios = value === 'all' ? ALL_SCENARIO_KEYS : value.split(',').map((item) => item.trim());
    } else if (arg === '--ideas') options.ideas = Number(next());
    else if (arg === '--connections') options.connections = Number(next());
    else if (arg === '--stream-items') options.streamItems = Number(next());
    else if (arg === '--stream-contract') options.streamContract = next();
    else if (arg === '--api-latency-ms') options.apiLatencyMs = Number(next());
    else if (arg === '--sidecar-latency-ms') options.sidecarLatencyMs = Number(next());
    else if (arg === '--timeout-ms') options.timeoutMs = Number(next());
    else if (arg === '--chrome-path') options.chromePath = next();
    else if (arg === '--phase') options.phase = next();
    else if (arg === '--out') options.out = next();
    else if (arg === '--compare') options.compare = next();
    else if (arg === '--allow-unknown-api') options.allowUnknownApi = true;
    else if (arg === '--json') options.json = true;
    else if (arg === '--keep-chrome-profile') options.keepChromeProfile = true;
    else if (arg === '--manifest') options.manifest = next();
    else if (arg === '--workspace-idle') options.workspaceIdle = true;
    else if (arg === '--idle-settle-ms') options.idleSettleMs = Number(next());
    else if (arg === '--idle-window-ms') options.idleWindowMs = Number(next());
    else if (arg === '--help' || arg === '-h') {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  for (const scenario of options.scenarios) {
    if (!SCENARIOS[scenario]) {
      throw new Error(`Unknown scenario "${scenario}". Use one of: ${Object.keys(SCENARIOS).join(', ')}`);
    }
  }
  if (!Number.isFinite(options.runs) || options.runs < 1) throw new Error('--runs must be >= 1');
  if (!Number.isFinite(options.warmups) || options.warmups < 0) throw new Error('--warmups must be >= 0');
  if (!Number.isFinite(options.ideas) || options.ideas < 1) throw new Error('--ideas must be >= 1');
  if (!Number.isFinite(options.connections) || options.connections < 0) throw new Error('--connections must be >= 0');
  if (!Number.isFinite(options.streamItems) || options.streamItems < 0) throw new Error('--stream-items must be >= 0');
  if (![options.idleSettleMs, options.idleWindowMs].every((value) => Number.isFinite(value) && value >= 0)) {
    throw new Error('workspace idle durations must be >= 0');
  }
  if (options.workspaceIdle && options.scenarios.some((scenario) => !['workspace', 'threadCanonical'].includes(scenario))) {
    throw new Error('--workspace-idle supports only workspace and threadCanonical scenarios');
  }
  if (!['legacy', 'paged'].includes(options.streamContract)) {
    throw new Error('--stream-contract must be legacy or paged');
  }
  return options;
}

function printHelp() {
  console.log(`Usage: node scripts/benchmark_frontend.mjs [options]

Options:
  --base-url URL              Frontend dev/preview URL (default: ${DEFAULT_BASE_URL})
  --runs N                    Measured runs per scenario (default: 5)
  --warmups N                 Warmup runs per scenario before measuring (default: 1)
  --scenario NAME             ${ALL_SCENARIO_KEYS.join(', ')}, comma-separated names, or all (default: all)
  --ideas N                   Mock thread count (default: 120)
  --connections N             Mock connection count (default: 240)
  --stream-items N            Mock thread transcript item count (default: 80)
  --stream-contract NAME      legacy full array or paged response (default: paged)
  --api-latency-ms N          Artificial latency for every mocked API call (default: 0)
  --sidecar-latency-ms N      Latency for non-critical sidecar calls (default: api latency)
  --timeout-ms N              Per-run ready timeout (default: 20000)
  --chrome-path PATH          Chrome/Chromium executable path
  --phase NAME                Label stored in JSON output (default: current)
  --out PATH                  Write the full JSON report to PATH
  --compare BEFORE,AFTER      Print a before/after win chart from two JSON reports
  --manifest PATH             Vite client manifest for the target build
  --workspace-idle            Measure settled workspace/browser idle behavior
  --idle-settle-ms N          Settle delay before idle measurement (default: 10000)
  --idle-window-ms N          Idle measurement window (default: 10000)
  --allow-unknown-api         Do not fail when the app calls an unmocked API route
  --json                      Print machine-readable JSON
`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function percentile(values, pct) {
  if (!values.length) return 0;
  const ordered = [...values].sort((a, b) => a - b);
  const index = (ordered.length - 1) * pct;
  const lower = Math.floor(index);
  const upper = Math.min(lower + 1, ordered.length - 1);
  if (lower === upper) return ordered[lower];
  const weight = index - lower;
  return ordered[lower] * (1 - weight) + ordered[upper] * weight;
}

function summarizeNumbers(values) {
  if (!values.length) {
    return { min: 0, p50: 0, p75: 0, avg: 0, p95: 0, max: 0 };
  }
  return {
    min: Math.min(...values),
    p50: percentile(values, 0.5),
    p75: percentile(values, 0.75),
    avg: values.reduce((sum, value) => sum + value, 0) / values.length,
    p95: percentile(values, 0.95),
    max: Math.max(...values),
  };
}

function manifestEntry(manifest, predicate, label) {
  const matches = Object.entries(manifest).filter(([key, entry]) => predicate(key, entry));
  if (matches.length !== 1) {
    throw new Error(`Expected one ${label} entry in the Vite client manifest; found ${matches.length}`);
  }
  return matches[0][0];
}

function manifestAssetClosure(manifest, rootKey) {
  const visited = new Set();
  const assets = new Set();

  function visit(key) {
    if (visited.has(key)) return;
    const entry = manifest[key];
    if (!entry) throw new Error(`Vite client manifest is missing imported entry ${key}`);
    visited.add(key);
    if (entry.file) assets.add(`/${entry.file}`);
    for (const css of entry.css ?? []) assets.add(`/${css}`);
    for (const importedKey of entry.imports ?? []) visit(importedKey);
  }

  visit(rootKey);
  return [...assets].sort();
}

function manifestEntryAssets(manifest, key) {
  const entry = manifest[key];
  return [entry.file ? `/${entry.file}` : null, ...(entry.css ?? []).map((css) => `/${css}`)]
    .filter(Boolean)
    .sort();
}

async function loadExpectedLazyAssetClosures(manifestPath = VITE_CLIENT_MANIFEST_URL) {
  let manifest;
  try {
    manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
  } catch (error) {
    throw new Error(
      `The production Vite client manifest is required for lazy asset contracts. Run the frontend build first. ${error.message}`,
    );
  }

  const threadStageKey = manifestEntry(
    manifest,
    (_key, entry) => entry.name === 'ThreadStageScreen' && entry.isDynamicEntry === true,
    'ThreadStageScreen dynamic module',
  );
  const vaultKey = manifestEntry(
    manifest,
    (_key, entry) => entry.src === 'src/routes/vault/+page.svelte' && entry.isDynamicEntry === true,
    'Vault page dynamic module',
  );

  return {
    threadStage: {
      moduleId: threadStageKey,
      assets: manifestAssetClosure(manifest, threadStageKey),
      entryAssets: manifestEntryAssets(manifest, threadStageKey),
    },
    vault: {
      moduleId: vaultKey,
      assets: manifestAssetClosure(manifest, vaultKey),
      entryAssets: manifestEntryAssets(manifest, vaultKey),
    },
  };
}

function normalizeWsData(data) {
  if (typeof data === 'string') return Promise.resolve(data);
  if (data instanceof ArrayBuffer) return Promise.resolve(Buffer.from(data).toString('utf8'));
  if (ArrayBuffer.isView(data)) return Promise.resolve(Buffer.from(data.buffer).toString('utf8'));
  if (data && typeof data.text === 'function') return data.text();
  return Promise.resolve(String(data));
}

class CdpClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
  }

  async connect() {
    this.ws = new WebSocket(this.wsUrl);
    await new Promise((resolve, reject) => {
      const cleanup = () => {
        this.ws.removeEventListener('open', onOpen);
        this.ws.removeEventListener('error', onError);
      };
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onError = (event) => {
        cleanup();
        reject(new Error(`CDP WebSocket failed to open: ${event.message || 'unknown error'}`));
      };
      this.ws.addEventListener('open', onOpen);
      this.ws.addEventListener('error', onError);
    });

    this.ws.addEventListener('message', (event) => {
      void this.handleMessage(event.data);
    });
    this.ws.addEventListener('close', () => {
      for (const { reject } of this.pending.values()) {
        reject(new Error('CDP WebSocket closed'));
      }
      this.pending.clear();
    });
  }

  async handleMessage(data) {
    const text = await normalizeWsData(data);
    const message = JSON.parse(text);
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(`${message.error.message}: ${JSON.stringify(message.error.data ?? '')}`));
      else pending.resolve(message.result ?? {});
      return;
    }

    if (message.method) {
      const callbacks = this.handlers.get(message.method);
      if (!callbacks) return;
      for (const callback of callbacks) callback(message.params ?? {});
    }
  }

  send(method, params = {}) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error(`CDP is not connected for ${method}`));
    }
    const id = this.nextId;
    this.nextId += 1;
    const payload = JSON.stringify({ id, method, params });
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.ws.send(payload);
    return promise;
  }

  on(method, callback) {
    const callbacks = this.handlers.get(method) ?? new Set();
    callbacks.add(callback);
    this.handlers.set(method, callbacks);
    return () => callbacks.delete(callback);
  }

  close() {
    this.ws?.close();
  }
}

async function launchChrome(options) {
  const chromePath = options.chromePath || DEFAULT_CHROME_PATHS.find(Boolean);
  if (!chromePath) {
    throw new Error('Could not find Chrome. Pass --chrome-path or set CHROME_PATH.');
  }
  const userDataDir = await mkdtemp(path.join(os.tmpdir(), 'illo-frontend-bench-'));
  const args = [
    '--headless=new',
    '--remote-debugging-port=0',
    `--user-data-dir=${userDataDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-timer-throttling',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-extensions',
    '--disable-gpu',
    '--window-size=1440,1000',
    'about:blank',
  ];

  const proc = spawn(chromePath, args, {
    stdio: ['ignore', 'ignore', 'pipe'],
  });

  const browserWsUrl = await new Promise((resolve, reject) => {
    let stderr = '';
    const timeout = setTimeout(() => {
      reject(new Error(`Timed out waiting for Chrome DevTools endpoint. stderr:\n${stderr}`));
    }, 10000);

    proc.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
      const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) {
        clearTimeout(timeout);
        resolve(match[1]);
      }
    });
    proc.on('exit', (code) => {
      clearTimeout(timeout);
      reject(new Error(`Chrome exited before DevTools was ready (code ${code}). stderr:\n${stderr}`));
    });
  });

  const endpoint = new URL(browserWsUrl);
  const httpBase = `http://${endpoint.host}`;
  let targets = await fetchJson(`${httpBase}/json/list`);
  let pageTarget = targets.find((target) => target.type === 'page');
  if (!pageTarget) {
    pageTarget = await fetchJson(`${httpBase}/json/new?about:blank`, { method: 'PUT' });
  }

  return {
    proc,
    userDataDir,
    pageWsUrl: pageTarget.webSocketDebuggerUrl,
    async close() {
      proc.kill('SIGTERM');
      await Promise.race([
        new Promise((resolve) => proc.once('exit', resolve)),
        sleep(1500),
      ]).catch(() => {});
      if (!options.keepChromeProfile) {
        await rm(userDataDir, { recursive: true, force: true });
      }
    },
  };
}

async function fetchJson(url, init) {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ${url}`);
  return response.json();
}

function isoMinutesAgo(minutes) {
  return new Date(Date.now() - minutes * 60 * 1000).toISOString();
}

function buildFixture(options) {
  const colors = ['#57CFA0', '#7DA7FF', '#F0B35B', '#D879A6', '#9BDA78', '#B89CFF'];
  const members = colors.map((color, index) => ({
    id: `user-${index + 1}`,
    user_id: `user-${index + 1}`,
    name: `Bench User ${index + 1}`,
    email: `bench${index + 1}@example.test`,
    color,
    cortex_color: color,
    role: index === 0 ? 'admin' : 'member',
    created_at: isoMinutesAgo(10000 - index * 500),
    approved: true,
    attribution_visible: true,
  }));

  const ideas = Array.from({ length: options.ideas }, (_, index) => {
    const member = members[index % members.length];
    const ring = Math.floor(index / 18) + 1;
    const angle = (index / Math.max(options.ideas, 1)) * Math.PI * 2 * 3;
    return {
      id: `idea-${index + 1}`,
      title: `Benchmark thread ${index + 1}`,
      display_title: `Benchmark thread ${index + 1}`,
      description: `Synthetic benchmark thread ${index + 1}`,
      status: index % 11 === 0 ? 'done' : 'idle',
      origin: 'user',
      salience_score: 0.4 + (index % 7) / 10,
      position_x: Math.round(Math.cos(angle) * ring * 120),
      position_y: Math.round(Math.sin(angle) * ring * 88),
      created_at: isoMinutesAgo(2000 + index),
      updated_at: isoMinutesAgo(index + 1),
      user_id: member.id,
      author_name: member.name,
      author_color: member.color,
      thread_count: 2 + (index % 6),
      active_agents: 0,
      attachments: [],
      metadata: {},
      archived_at: null,
    };
  });

  const connections = [];
  for (let index = 0; index < options.connections; index += 1) {
    const source = ideas[index % ideas.length];
    const target = ideas[(index * 7 + 13) % ideas.length];
    if (!source || !target || source.id === target.id) continue;
    connections.push({
      id: `connection-${index + 1}`,
      source_id: source.id,
      target_id: target.id,
      type: index % 4 === 0 ? 'reference' : 'related',
      weight: 0.35 + (index % 5) / 10,
    });
  }

  return {
    user: {
      id: 'user-1',
      name: 'Bench User 1',
      email: 'bench@example.test',
      role: 'admin',
      color: '#57CFA0',
      org_id: 'org-bench',
      org_name: 'Benchmark Org',
      attribution_enabled: true,
      approved: true,
      default_provider: 'openai',
      cortex_concurrency_limit: 4,
    },
    members,
    ideas,
    connections,
    streamItems: buildStreamItems(options.streamItems),
    streamContract: options.streamContract,
    chat: buildChatFixture(members),
    runtimeSettings: buildRuntimeSettingsFixture(),
    teamTokenAnalytics: buildTeamTokenAnalyticsFixture(members),
  };
}

function buildRuntimeSettingsFixture() {
  return {
    connection: {
      status: 'connected',
      setup_required: false,
      method: 'api_key',
      source: 'user_openai',
      label: 'Personal OpenAI connection',
      detail: 'Deterministic frontend benchmark fixture',
      has_personal_connection: true,
      has_org_key: false,
    },
    models: {
      default: 'openai/gpt-5.4',
      options: [
        { key: 'openai/gpt-5.4', label: 'GPT-5.4', group: 'OpenAI' },
      ],
    },
    memory: {
      scope: 'installation',
      embedder: 'local_cpu',
      embedding_model: null,
      embedding_dimensions: 384,
      embedding_status: 'ready',
      embedding_detail: 'Local benchmark fixture',
      indexed_vectors: 128,
      api_key_statuses: {},
      reranker: 'weighted',
      embedder_options: [
        { key: 'local_cpu', label: 'Local CPU' },
      ],
      embedding_model_options: [],
      reranker_options: [
        { key: 'weighted', label: 'Weighted' },
      ],
    },
    voice: {
      provider: 'local',
      model: 'whisper',
      source: 'memory',
      language: 'auto',
      model_size: 'base',
      status: 'ready',
      detail: 'Local benchmark fixture',
      provider_options: [
        { key: 'local', label: 'Local' },
      ],
      language_options: [
        { key: 'auto', label: 'Automatic' },
      ],
      model_size_options: [
        { key: 'base', label: 'Base' },
      ],
    },
    permissions: {
      can_manage_settings: true,
    },
  };
}

function buildTeamTokenAnalyticsFixture(members) {
  const memberUsage = members.map((member, index) => ({
    user_id: member.id,
    runs: 10 + index,
    api_calls: 20 + index * 2,
    input_tokens: 10000 + index * 1000,
    output_tokens: 5000 + index * 500,
    total_tokens: 15000 + index * 1500,
    cache_read: 1000 + index * 100,
    cache_write: 200 + index * 20,
    estimated_cost: 0.1 + index * 0.01,
    last_used_at: isoMinutesAgo(index + 1),
  }));
  const totals = memberUsage.reduce((total, usage) => ({
    ...total,
    runs: total.runs + usage.runs,
    api_calls: total.api_calls + usage.api_calls,
    input_tokens: total.input_tokens + usage.input_tokens,
    output_tokens: total.output_tokens + usage.output_tokens,
    total_tokens: total.total_tokens + usage.total_tokens,
    cache_read: total.cache_read + usage.cache_read,
    cache_write: total.cache_write + usage.cache_write,
    estimated_cost: total.estimated_cost + usage.estimated_cost,
    last_used_at: usage.last_used_at,
  }), {
    user_id: null,
    runs: 0,
    api_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read: 0,
    cache_write: 0,
    estimated_cost: 0,
    last_used_at: null,
  });
  const unattributed = {
    user_id: null,
    runs: 0,
    api_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read: 0,
    cache_write: 0,
    estimated_cost: 0,
    last_used_at: null,
  };
  return {
    window_days: 30,
    generated_at: new Date().toISOString(),
    members: memberUsage,
    unattributed,
    totals,
  };
}

function buildStreamItems(count) {
  const items = [];
  const total = Math.max(count, 1);
  for (let index = 0; index < total; index += 1) {
    if (index % 12 === 5) {
      items.push({
        type: 'run',
        id: `run-${index}`,
        timestamp: isoMinutesAgo(total - index),
        title: `Benchmark run ${index}`,
        status: 'completed',
        skill_name: 'benchmark-runner',
        model_used: 'gpt-bench',
        thinking_used: 'medium',
        tokens_total: 1200 + index * 21,
        duration_sec: 8 + index,
        outcome: 'ok',
        estimated_cost: 0.01,
        run_steps: [
          { id: `phase-${index}-1`, node_id: 'plan', status: 'completed', duration_sec: 1.2 },
          { id: `phase-${index}-2`, node_id: 'execute', status: 'completed', duration_sec: 4.8 },
        ],
        worker_lanes: [],
        tool_calls: [
          { tool: 'benchmark_tool', args: '{"ok":true}', at: isoMinutesAgo(total - index) },
        ],
        live_lines: ['Prepared benchmark data', 'Rendered synthetic transcript'],
      });
      continue;
    }

    const assistant = index % 2 === 1;
    items.push({
      type: 'message',
      id: `message-${index}`,
      timestamp: isoMinutesAgo(total - index),
      role: assistant ? 'assistant' : 'user',
      content: assistant
        ? `Benchmark assistant reply ${index}. This paragraph includes **markdown**, a short list, and enough text to exercise readable rendering.\\n\\n- Render item ${index}\\n- Preserve transcript behavior\\n- Keep the benchmark deterministic`
        : `Benchmark user prompt ${index}. Please reason about workspace performance for this synthetic thread.`,
      attachments: [],
      metadata: {},
      user_id: assistant ? null : 'user-1',
      user_name: assistant ? 'Illo' : 'Bench User 1',
      user_color: assistant ? '#57CFA0' : '#57CFA0',
      author_color: '#57CFA0',
      message_type: 'message',
    });
  }
  return items;
}

function buildChatFixture(members) {
  const room = {
    id: 'room-bench',
    type: 'room',
    stable_key: 'org-room',
    title: 'Team room',
    description: null,
    visibility: 'org',
    last_message_seq: 0,
    unread_count: 0,
    participant_count: members.length,
    counterpart: null,
    last_message: null,
    created_at: isoMinutesAgo(5000),
    updated_at: isoMinutesAgo(500),
  };
  return {
    room,
    bootstrap: {
      room,
      dms: [],
      notifications: [],
      unread_summary: { room: 0, dms: 0, total: 0 },
      default_mode: 'room',
      default_conversation_id: room.id,
    },
    conversationPage: {
      conversation: room,
      messages: [],
      has_more: false,
      next_before_seq: null,
    },
  };
}

function threadStreamPage(streamItems, ideaId, before = null, limit = THREAD_HISTORY_WINDOW_SIZE) {
  const cursorMatch = before?.match(/^bench-before-(\d+)$/) ?? null;
  if (before && !cursorMatch) return null;
  const end = cursorMatch ? Number(cursorMatch[1]) : streamItems.length;
  if (!Number.isInteger(end) || end < 0 || end > streamItems.length) return null;
  const start = Math.max(0, end - limit);
  return {
    idea_id: ideaId,
    items: streamItems.slice(start, end),
    has_more: start > 0,
    next_before: start > 0 ? `bench-before-${start}` : null,
  };
}

function mockThreadStreamPayload(fixture, ideaId, url, direct = false) {
  if (fixture.streamContract === 'legacy') {
    return direct
      ? { idea_id: ideaId, stream: fixture.streamItems }
      : fixture.streamItems;
  }
  const requestedLimit = Number(url.searchParams.get('limit') || THREAD_HISTORY_WINDOW_SIZE);
  if (!Number.isInteger(requestedLimit) || requestedLimit < 1 || requestedLimit > THREAD_HISTORY_WINDOW_SIZE) {
    return null;
  }
  return threadStreamPage(
    fixture.streamItems,
    ideaId,
    url.searchParams.get('before'),
    requestedLimit,
  );
}

function jsonResponse(status, body, extra = {}) {
  const threadPayloadText = extra.threadPayload ? JSON.stringify(extra.threadPayload) : null;
  return {
    status,
    body,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      ...extra.headers,
    },
    label: extra.label,
    sidecar: Boolean(extra.sidecar),
    unknown: Boolean(extra.unknown),
    threadPageKind: extra.threadPageKind ?? null,
    threadPageItems: extra.threadPageItems ?? null,
    threadPayloadBytes: threadPayloadText ? Buffer.byteLength(threadPayloadText) : null,
    threadPayloadGzipBytes: threadPayloadText ? gzipSync(threadPayloadText).byteLength : null,
  };
}

function mockApiResponse(method, url, fixture) {
  const pathName = url.pathname;
  const pathWithQuery = `${pathName}${url.search}`;
  const methodUpper = method.toUpperCase();

  if (pathName === '/api/me') return jsonResponse(200, fixture.user, { label: 'GET /api/me' });
  if (pathName === '/api/runtime-settings') {
    return jsonResponse(200, fixture.runtimeSettings, { label: 'GET /api/runtime-settings' });
  }
  if (pathName === '/api/auth/ws-token') {
    return jsonResponse(200, {
      token: 'bench-token',
      expires_at: isoMinutesAgo(-60),
      ttl_seconds: 3600,
      session_id: 'bench-session',
      tab_id: 'bench-tab',
    }, { label: 'POST /api/auth/ws-token', sidecar: true });
  }
  if (pathName === '/api/cortex/keys/auto-import') {
    return jsonResponse(200, { ok: true, imported: false }, { label: 'POST /api/cortex/keys/auto-import', sidecar: true });
  }
  if (pathName === '/api/cortex/auth/status') {
    return jsonResponse(200, { setup_required: false, configured: true, provider: 'openai' }, {
      label: 'GET /api/cortex/auth/status',
      sidecar: true,
    });
  }
  if (pathName === '/api/cortex/notify' && methodUpper === 'POST') {
    return jsonResponse(200, { ok: true }, { label: 'POST /api/cortex/notify', sidecar: true });
  }

  if (pathName === '/api/cycles/') {
    return jsonResponse(200, [], { label: 'GET /api/cycles/' });
  }
  if (pathName === '/api/skills/enhanced') {
    return jsonResponse(200, [], { label: 'GET /api/skills/enhanced' });
  }

  if (pathName === '/api/cortex/ideas') {
    return jsonResponse(200, fixture.ideas, { label: 'GET /api/cortex/ideas' });
  }
  if (pathName === '/api/cortex/ideas/positions' && methodUpper === 'PUT') return jsonResponse(200, { ok: true }, { label: 'PUT /api/cortex/ideas/positions', sidecar: true });
  if (pathName === '/api/cortex/bootstrap') {
    const include = new Set(
      (url.searchParams.get('include') || 'core')
        .split(',')
        .map((part) => part.trim())
        .filter(Boolean),
    );
    if (include.has('core')) {
      include.add('ideas');
      include.add('connections');
      include.add('team_members');
    }
    if (include.has('workspace')) {
      include.add('workspace_apps');
      include.add('workspace_pins');
    }
    const body = {
      ideas: include.has('ideas') ? fixture.ideas : null,
      connections: include.has('connections') ? fixture.connections : null,
      team_members: include.has('team_members') ? fixture.members : null,
      workspace_apps: include.has('workspace_apps') ? [] : null,
      workspace_pins: include.has('workspace_pins') ? [] : null,
      selected_idea: include.has('selected_idea') ? fixture.ideas[0] : null,
      auth_status: include.has('auth_status') ? { setup_required: false, configured: true, provider: 'openai' } : null,
      meta: { include: [...include].sort() },
    };
    let directThreadPayload = null;
    if (include.has('direct_thread')) {
      const ideaId = url.searchParams.get('idea_id') || 'idea-1';
      directThreadPayload = mockThreadStreamPayload(fixture, ideaId, url, true);
      body.direct_thread = directThreadPayload;
    }
    return jsonResponse(200, body, {
      label: 'GET /api/cortex/bootstrap',
      threadPayload: directThreadPayload,
      threadPageKind: directThreadPayload ? 'initial' : null,
      threadPageItems: directThreadPayload
        ? (directThreadPayload.items?.length ?? directThreadPayload.stream?.length ?? null)
        : null,
    });
  }
  if (pathName === '/api/cortex/connections') {
    return jsonResponse(200, fixture.connections, { label: 'GET /api/cortex/connections' });
  }
  if (pathName === '/api/cortex/ideas/archived') {
    return jsonResponse(200, [], { label: 'GET /api/cortex/ideas/archived', sidecar: true });
  }
  const unifiedStreamMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/unified-stream$/);
  if (unifiedStreamMatch) {
    const payload = mockThreadStreamPayload(fixture, unifiedStreamMatch[1], url);
    if (!payload) {
      return jsonResponse(422, { detail: 'Invalid benchmark thread page cursor or limit' }, {
        label: 'GET /api/cortex/ideas/{idea_id}/unified-stream',
      });
    }
    const before = url.searchParams.get('before');
    return jsonResponse(200, payload, {
      label: 'GET /api/cortex/ideas/{idea_id}/unified-stream',
      threadPayload: payload,
      threadPageKind: fixture.streamContract === 'paged' ? (before ? 'older' : 'head') : 'legacy',
      threadPageItems: payload.items?.length ?? payload.length,
    });
  }
  const threadMessageMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/thread$/);
  if (threadMessageMatch && methodUpper === 'POST') {
    return jsonResponse(200, {
      id: 'benchmark-composer-message',
      idea_id: threadMessageMatch[1],
      role: 'user',
      content: 'Benchmark direct-thread composer contract',
      attachments: [],
      metadata: {},
      created_at: new Date().toISOString(),
    }, { label: 'POST /api/cortex/ideas/{idea_id}/thread' });
  }
  const ideaStatusMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/status$/);
  if (ideaStatusMatch && methodUpper === 'PATCH') {
    return jsonResponse(200, {
      ...(fixture.ideas.find((idea) => idea.id === ideaStatusMatch[1]) ?? fixture.ideas[0]),
      status: 'queued',
    }, { label: 'PATCH /api/cortex/ideas/{idea_id}/status', sidecar: true });
  }
  const discussionMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/discussion$/);
  if (discussionMatch && methodUpper === 'GET') {
    return jsonResponse(200, [], { label: 'GET /api/cortex/ideas/{idea_id}/discussion', sidecar: true });
  }
  const handoffSummaryMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/handoff-summary$/);
  if (handoffSummaryMatch && methodUpper === 'GET') {
    return jsonResponse(200, { found: false }, {
      label: 'GET /api/cortex/ideas/{idea_id}/handoff-summary',
      sidecar: true,
    });
  }
  const activityTimelineMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/activity-timeline$/);
  if (activityTimelineMatch) {
    return jsonResponse(200, [], { label: 'GET /api/cortex/ideas/{idea_id}/activity-timeline', sidecar: true });
  }
  const runHistoryMatch = pathName.match(/^\/api\/cortex\/run\/history\/([^/]+)$/);
  if (runHistoryMatch) {
    return jsonResponse(200, [], { label: 'GET /api/cortex/run/history/{idea_id}', sidecar: true });
  }
  const browserSessionMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/browser\/session$/);
  if (browserSessionMatch && methodUpper === 'GET') {
    return jsonResponse(200, null, { label: 'GET /api/cortex/ideas/{idea_id}/browser/session', sidecar: true });
  }
  const markReadMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/mark-read$/);
  if (markReadMatch) {
    return jsonResponse(200, { ok: true }, { label: 'POST /api/cortex/ideas/{idea_id}/mark-read', sidecar: true });
  }
  if (pathName === '/api/cortex/mentions/unread') {
    return jsonResponse(200, [], { label: 'GET /api/cortex/mentions/unread', sidecar: true });
  }
  if (pathName === '/api/cortex/similarity-matrix') {
    return jsonResponse(200, { pairs: [] }, { label: 'GET /api/cortex/similarity-matrix', sidecar: true });
  }
  if (pathName === '/api/cortex/slash-commands') {
    return jsonResponse(200, [
      { name: 'summarize', description: 'Summarize the current thread', tier: 'medium' },
      { name: 'plan', description: 'Turn the thread into a plan', tier: 'local' },
    ], { label: 'GET /api/cortex/slash-commands', sidecar: true });
  }
  if (pathName === '/api/cortex/project-context/profiles') {
    return jsonResponse(200, [], { label: 'GET /api/cortex/project-context/profiles', sidecar: true });
  }
  const ideaProjectContextMatch = pathName.match(/^\/api\/cortex\/ideas\/([^/]+)\/project-context$/);
  if (ideaProjectContextMatch && methodUpper === 'GET') {
    return jsonResponse(200, [], { label: 'GET /api/cortex/ideas/{idea_id}/project-context', sidecar: true });
  }
  if (ideaProjectContextMatch && methodUpper === 'POST') {
    return jsonResponse(200, {
      id: 'bench-project-context-attachment',
      snapshot: {},
      created_at: isoMinutesAgo(0),
    }, { label: 'POST /api/cortex/ideas/{idea_id}/project-context', sidecar: true });
  }

  if (pathName === '/api/team/members') {
    return jsonResponse(200, fixture.members, { label: 'GET /api/team/members', sidecar: true });
  }
  if (pathName === '/api/team/token-analytics') {
    return jsonResponse(200, fixture.teamTokenAnalytics, { label: 'GET /api/team/token-analytics' });
  }
  if (pathName === '/api/workspace-apps/' || pathName === '/api/workspace-apps') {
    return jsonResponse(200, [], { label: 'GET /api/workspace-apps/', sidecar: true });
  }
  if (pathName === '/api/workspace-pins/' || pathName === '/api/workspace-pins') {
    return jsonResponse(200, [], { label: 'GET /api/workspace-pins/', sidecar: true });
  }

  if (pathName === '/api/notifications/summary') {
    return jsonResponse(200, {
      chat_unread_total: 0,
      workspace_attention_total: 0,
      unread_notification_total: 0,
      unread_chat_notification_total: 0,
      unread_workspace_notification_total: 0,
    }, { label: 'GET /api/notifications/summary', sidecar: true });
  }
  if (pathName === '/api/notifications') {
    return jsonResponse(200, [], { label: `GET /api/notifications${url.search}`, sidecar: true });
  }
  if (pathName === '/api/notifications/preferences') {
    return jsonResponse(200, {
      sound_enabled: true,
      message_notifications_enabled: true,
    }, { label: 'GET /api/notifications/preferences', sidecar: true });
  }

  if (pathName === '/api/chat/bootstrap') {
    return jsonResponse(200, fixture.chat.bootstrap, { label: 'GET /api/chat/bootstrap', sidecar: true });
  }
  if (pathName === '/api/chat/conversations') {
    return jsonResponse(200, [fixture.chat.room], { label: 'GET /api/chat/conversations', sidecar: true });
  }
  const chatMessagesMatch = pathName.match(/^\/api\/chat\/conversations\/([^/]+)\/messages$/);
  if (chatMessagesMatch) {
    return jsonResponse(200, fixture.chat.conversationPage, {
      label: 'GET /api/chat/conversations/{conversation_id}/messages',
      sidecar: true,
    });
  }
  const chatReadMatch = pathName.match(/^\/api\/chat\/conversations\/([^/]+)\/read$/);
  if (chatReadMatch) {
    return jsonResponse(200, { room: 0, dms: 0, total: 0 }, {
      label: 'POST /api/chat/conversations/{conversation_id}/read',
      sidecar: true,
    });
  }
  if (pathName === '/api/chat/notifications') {
    return jsonResponse(200, [], { label: `GET /api/chat/notifications${url.search}`, sidecar: true });
  }

  if (pathName === '/api/vault/pin-status') {
    return jsonResponse(200, { has_pin: false, failed_attempts: 0, locked_until: null }, {
      label: 'GET /api/vault/pin-status',
    });
  }
  if (pathName === '/api/vault/' || pathName === '/api/vault/missing') {
    return jsonResponse(200, [], { label: `GET ${pathName}` });
  }
  if (pathName === '/api/vault/agent-grants' || pathName === '/api/vault/project-bindings') {
    return jsonResponse(200, [], { label: `GET ${pathName}`, sidecar: true });
  }
  if (pathName === '/api/agent-connections') {
    return jsonResponse(200, [], { label: 'GET /api/agent-connections', sidecar: true });
  }

  return jsonResponse(200, fallbackBodyFor(pathName, methodUpper), {
    label: `${methodUpper} ${pathWithQuery}`,
    sidecar: true,
    unknown: true,
  });
}

function fallbackBodyFor(pathName, method) {
  if (method !== 'GET') return { ok: true };
  if (
    pathName.endsWith('/notifications') ||
    pathName.endsWith('/members') ||
    pathName.endsWith('/connections') ||
    pathName.endsWith('/ideas')
  ) {
    return [];
  }
  return {};
}

function latencyForResponse(response, options) {
  if (!response.sidecar) return options.apiLatencyMs;
  return options.sidecarLatencyMs ?? options.apiLatencyMs;
}

async function evaluate(client, expression) {
  const result = await client.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || 'Runtime.evaluate failed');
  }
  return result.result?.value;
}

async function performanceMetrics(client) {
  const result = await client.send('Performance.getMetrics');
  return Object.fromEntries((result.metrics ?? []).map((metric) => [metric.name, metric.value]));
}

function performanceDurationMs(before, after, name) {
  const start = before[name];
  const end = after[name];
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return (end - start) * 1000;
}

async function workspaceIdleSnapshot(client) {
  return evaluate(client, `(() => {
    const value = window.__illoBench?.workspaceIdle;
    return value ? { available: value.available, rafExecuted: value.rafExecuted,
      signalStyleWrites: value.signalStyleWrites } : { available: false };
  })()`);
}

function workspaceIdleCounterDelta(before, after) {
  return {
    rafCallbacks: after.rafExecuted - before.rafExecuted,
    signalStyleWrites: after.signalStyleWrites - before.signalStyleWrites,
  };
}

async function measureWorkspaceIdleWindow(client, durationMs) {
  const beforeSnapshot = await workspaceIdleSnapshot(client);
  const beforePerformance = await performanceMetrics(client);
  await sleep(durationMs);
  const afterPerformance = await performanceMetrics(client);
  const afterSnapshot = await workspaceIdleSnapshot(client);
  const counters = workspaceIdleCounterDelta(beforeSnapshot, afterSnapshot);
  const taskDurationMs = performanceDurationMs(beforePerformance, afterPerformance, 'TaskDuration');
  const scriptDurationMs = performanceDurationMs(beforePerformance, afterPerformance, 'ScriptDuration');
  return {
    available: beforeSnapshot.available === true && afterSnapshot.available === true &&
      [taskDurationMs, scriptDurationMs, counters.rafCallbacks, counters.signalStyleWrites].every(Number.isFinite),
    taskDurationMs,
    scriptDurationMs,
    ...counters,
  };
}

async function setWorkspaceIdleVisibility(client, state) {
  return evaluate(client, `window.__illoBench?.workspaceIdle?.setVisibilityState?.(${JSON.stringify(state)}) ?? null`);
}

async function measureWorkspaceIdleHiddenControl(client, hiddenMs) {
  let restored = false;
  try {
    const hiddenState = await setWorkspaceIdleVisibility(client, 'hidden');
    if (hiddenState !== 'hidden') return { available: false, reason: 'visibility override did not enter hidden state' };
    await sleep(100);
    const hiddenBefore = await workspaceIdleSnapshot(client);
    await sleep(hiddenMs);
    const hiddenAfter = await workspaceIdleSnapshot(client);
    const visibleState = await setWorkspaceIdleVisibility(client, 'visible');
    restored = visibleState === 'visible';
    const visibleBefore = await workspaceIdleSnapshot(client);
    await sleep(500);
    const visibleAfter = await workspaceIdleSnapshot(client);
    return {
      available: restored && hiddenBefore.available && hiddenAfter.available,
      ...workspaceIdleCounterDelta(hiddenBefore, hiddenAfter),
      resumeRafCallbacks: visibleAfter.rafExecuted - visibleBefore.rafExecuted,
      resumeSignalStyleWrites: visibleAfter.signalStyleWrites - visibleBefore.signalStyleWrites,
    };
  } finally {
    if (!restored) await setWorkspaceIdleVisibility(client, 'visible').catch(() => {});
  }
}

async function waitForWorkspaceIdleProbe(client, timeoutMs) {
  await waitForExpression(
    client,
    `(() => {
      const probe = window.__illoBench?.workspaceIdle?.activeProbe;
      return Number.isFinite(probe?.firstRafMs) && Number.isFinite(probe?.firstSignalStyleMs);
    })()`,
    timeoutMs,
  ).catch(() => null);
  return evaluate(client, 'window.__illoBench?.workspaceIdle?.activeProbe ?? null');
}

async function finishWorkspaceWakeProbe(client, matchedExpression = 'true') {
  const probe = await waitForWorkspaceIdleProbe(client, 1_200);
  const matched = await evaluate(client, matchedExpression);
  const wakeTimestamps = [probe?.firstRafMs, probe?.firstSignalStyleMs];
  const available = matched === true && wakeTimestamps.every(Number.isFinite);
  return { available, resumeMs: available ? Math.max(...wakeTimestamps) : null };
}

async function measureWorkspaceMutationWake(client) {
  const dispatched = await evaluate(client, `(() => {
    const telemetry = window.__illoBench?.workspaceIdle;
    const socket = telemetry?.sockets?.find((candidate) => candidate.readyState === 1);
    const signal = document.querySelector('[data-constellation-signal-id="idea-2"]');
    if (!telemetry || !socket || !(signal instanceof HTMLElement)) return null;
    telemetry.beginProbe();
    socket.__dispatch({
      type: 'message',
      data: JSON.stringify({ type: 'idea_updated', idea_id: 'idea-2', fields: { status: 'done' } }),
    });
    return true;
  })()`);
  if (!dispatched) return { available: false };
  return finishWorkspaceWakeProbe(client,
    `document.querySelector('[data-constellation-signal-id="idea-2"]')?.classList.contains('constellation-signal-blob-cue-attention') === true`,
  );
}

async function measureWorkspaceDragWake(client) {
  const target = await evaluate(client, `(() => {
    const telemetry = window.__illoBench?.workspaceIdle;
    const signal = document.querySelector('[data-constellation-signal-id="idea-3"]') ||
      document.querySelector('[data-constellation-signal-id]');
    if (!telemetry || !(signal instanceof HTMLElement)) return null;
    const rect = signal.getBoundingClientRect();
    telemetry.beginProbe();
    return {
      x: rect.left + rect.width / 2,
      y: rect.top + rect.height / 2,
    };
  })()`);
  if (!target) return { available: false };
  await client.send('Input.dispatchMouseEvent', {
    type: 'mousePressed', x: target.x, y: target.y, button: 'left', buttons: 1, clickCount: 1,
  });
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseMoved', x: target.x + 36, y: target.y + 18, button: 'left', buttons: 1,
  });
  const probe = await finishWorkspaceWakeProbe(client);
  await client.send('Input.dispatchMouseEvent', {
    type: 'mouseReleased', x: target.x + 36, y: target.y + 18, button: 'left', buttons: 0, clickCount: 1,
  });
  return probe;
}

async function measureWorkspaceIdleContract(client, scenario, options) {
  await sleep(options.idleSettleMs);
  const idle = await measureWorkspaceIdleWindow(client, options.idleWindowMs);
  const hidden = await measureWorkspaceIdleHiddenControl(client, 2_000);
  if (scenario.directThread) return { available: idle.available && hidden.available, idle, hidden };
  const mutation = await measureWorkspaceMutationWake(client);
  await sleep(6_500);
  const drag = await measureWorkspaceDragWake(client);
  return {
    available: idle.available && hidden.available && mutation.available && drag.available,
    idle,
    hidden,
    mutation,
    drag,
  };
}

async function waitForExpression(client, expression, timeoutMs) {
  const started = performance.now();
  const deadline = started + timeoutMs;
  let lastError = null;
  while (performance.now() < deadline) {
    try {
      if (await evaluate(client, expression)) {
        return performance.now() - started;
      }
    } catch (error) {
      lastError = error;
    }
    await sleep(25);
  }
  const diagnostics = await evaluate(client, `(() => ({
    href: location.href,
    readyState: document.readyState,
    title: document.title,
    bodyText: document.body?.innerText?.slice(0, 1000) || '',
    bodyHtml: document.body?.innerHTML?.slice(0, 1000) || '',
    errors: window.__illoBench?.errors || [],
  }))()`).catch(() => null);
  const moduleFetch = await evaluate(client, `fetch('/.svelte-kit/generated/client/nodes/0.js')
    .then(async (response) => ({
      status: response.status,
      contentType: response.headers.get('content-type'),
      text: (await response.text()).slice(0, 500),
    }))
    .catch((error) => ({ error: String(error?.message || error) }))`).catch(() => null);
  const moduleImport = await evaluate(client, `import('/.svelte-kit/generated/client/nodes/0.js')
    .then((module) => ({ ok: true, keys: Object.keys(module) }))
    .catch((error) => ({ ok: false, message: String(error?.message || error), stack: String(error?.stack || '') }))`).catch(() => null);
  throw new Error(
    `Timed out waiting for page readiness.${lastError ? ` Last error: ${lastError.message}` : ''}` +
    `\nDiagnostics:\n${JSON.stringify(diagnostics, null, 2)}` +
    `\nModule fetch:\n${JSON.stringify(moduleFetch, null, 2)}` +
    `\nModule import:\n${JSON.stringify(moduleImport, null, 2)}`,
  );
}

function installPageInstrumentationSource(workspaceIdle = false) {
  return `(() => {
    window.__illoBench = {
      longTasks: [],
      longTaskObserverAvailable: false,
      longTaskObserverError: null,
      largestContentfulPaint: null,
      errors: [],
      wsMessages: [],
    };

    ${workspaceIdle ? `
    const idle = window.__illoBench.workspaceIdle = {
      available: true,
      rafExecuted: 0,
      signalStyleWrites: 0,
      activeProbe: null,
      sockets: [],
    };

    idle.beginProbe = () => idle.activeProbe = { startedAt: performance.now(), firstRafMs: null, firstSignalStyleMs: null };

    const nativeRequestAnimationFrame = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (callback) => nativeRequestAnimationFrame((timestamp) => {
        idle.rafExecuted += 1;
        const probe = idle.activeProbe;
        if (probe && probe.firstRafMs === null) probe.firstRafMs = performance.now() - probe.startedAt;
        return callback(timestamp);
      });

    try {
      const mutationObserver = new MutationObserver((records) => {
        for (const record of records) {
          if (!(record.target instanceof Element) || !record.target.matches('[data-constellation-signal-id]')) continue;
          idle.signalStyleWrites += 1;
          const probe = idle.activeProbe;
          if (probe && probe.firstSignalStyleMs === null) probe.firstSignalStyleMs = performance.now() - probe.startedAt;
        }
      });
      mutationObserver.observe(document, { subtree: true, attributes: true, attributeFilter: ['style'] });
    } catch (error) {
      idle.available = false;
      window.__illoBench.errors.push('workspace idle mutation observer: ' + String(error?.message || error));
    }

    try {
      const nativeVisibilityState = document.visibilityState;
      let visibilityOverride = null;
      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        get: () => visibilityOverride ?? nativeVisibilityState,
      });
      idle.setVisibilityState = (state) => {
        if (state !== 'visible' && state !== 'hidden') throw new Error('unsupported visibility state');
        visibilityOverride = state;
        document.dispatchEvent(new Event('visibilitychange'));
        return document.visibilityState;
      };
    } catch (error) {
      idle.available = false;
      window.__illoBench.errors.push('workspace idle visibility override: ' + String(error?.message || error));
    }
    ` : ''}

    window.addEventListener('error', (event) => {
      window.__illoBench.errors.push(String(event.message || 'unknown error'));
    });
    window.addEventListener('unhandledrejection', (event) => {
      window.__illoBench.errors.push(String(event.reason?.message || event.reason || 'unhandled rejection'));
    });

    try {
      if (!PerformanceObserver.supportedEntryTypes?.includes('longtask')) {
        throw new Error('longtask entries are unsupported');
      }
      const longTaskObserver = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          window.__illoBench.longTasks.push({
            startTime: entry.startTime,
            duration: entry.duration,
          });
        }
      });
      longTaskObserver.observe({ entryTypes: ['longtask'] });
      window.__illoBench.longTaskObserverAvailable = true;
    } catch (error) {
      window.__illoBench.longTaskObserverError = String(error?.message || error);
    }

    try {
      new PerformanceObserver((list) => {
        const entries = list.getEntries();
        const latest = entries[entries.length - 1];
        if (latest) {
          window.__illoBench.largestContentfulPaint = {
            startTime: latest.startTime,
            size: latest.size,
          };
        }
      }).observe({ type: 'largest-contentful-paint', buffered: true });
    } catch {}

    const NativeWebSocket = window.WebSocket;

    class BenchWebSocket {
      static CONNECTING = 0;
      static OPEN = 1;
      static CLOSING = 2;
      static CLOSED = 3;

      constructor(url) {
        this.url = url;
        this.readyState = BenchWebSocket.CONNECTING;
        this.extensions = '';
        this.protocol = '';
        this.binaryType = 'blob';
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        this.onclose = null;
        this.__listeners = new Map();
        window.__illoBench.workspaceIdle?.sockets.push(this);
        queueMicrotask(() => {
          if (this.readyState !== BenchWebSocket.CONNECTING) return;
          this.readyState = BenchWebSocket.OPEN;
          this.__dispatch({ type: 'open' });
        });
      }

      send(data) {
        window.__illoBench.wsMessages.push(String(data));
      }

      close() {
        if (this.readyState === BenchWebSocket.CLOSED) return;
        this.readyState = BenchWebSocket.CLOSED;
        this.__dispatch({ type: 'close', code: 1000, reason: '', wasClean: true });
      }

      addEventListener(type, listener) {
        const listeners = this.__listeners.get(type) || new Set();
        listeners.add(listener);
        this.__listeners.set(type, listeners);
      }

      removeEventListener(type, listener) {
        this.__listeners.get(type)?.delete(listener);
      }

      dispatchEvent(event) {
        this.__dispatch(event);
        return true;
      }

      __dispatch(event) {
        const handler = this['on' + event.type];
        if (typeof handler === 'function') handler.call(this, event);
        for (const listener of this.__listeners.get(event.type) || []) {
          listener.call(this, event);
        }
      }
    }
    function BenchmarkWebSocket(url, protocols) {
      try {
        const parsed = new URL(String(url), location.href);
        if (parsed.pathname === '/ws') return new BenchWebSocket(url);
      } catch {}
      return protocols === undefined ? new NativeWebSocket(url) : new NativeWebSocket(url, protocols);
    }

    BenchmarkWebSocket.CONNECTING = NativeWebSocket.CONNECTING;
    BenchmarkWebSocket.OPEN = NativeWebSocket.OPEN;
    BenchmarkWebSocket.CLOSING = NativeWebSocket.CLOSING;
    BenchmarkWebSocket.CLOSED = NativeWebSocket.CLOSED;
    BenchmarkWebSocket.prototype = NativeWebSocket.prototype;
    window.WebSocket = BenchmarkWebSocket;
  })();`;
}

async function configurePage(client, fixture, options, apiCalls) {
  await client.send('Page.enable');
  await client.send('Runtime.enable');
  await client.send('Network.enable');
  await client.send('Performance.enable');
  await client.send('Fetch.enable', {
    patterns: [{ urlPattern: '*://*/api/*', requestStage: 'Request' }],
  });
  const instrumentation = await client.send('Page.addScriptToEvaluateOnNewDocument', {
    source: installPageInstrumentationSource(options.workspaceIdle),
  });

  const pendingMocks = new Set();
  const unsubscribe = client.on('Fetch.requestPaused', (params) => {
    const requestUrl = new URL(params.request.url);
    const startedAt = performance.now();
    const response = mockApiResponse(params.request.method, requestUrl, fixture);
    const bodyText = JSON.stringify(response.body);
    const call = {
      method: params.request.method,
      path: requestUrl.pathname,
      query: requestUrl.search,
      label: response.label,
      status: response.status,
      bytes: Buffer.byteLength(bodyText),
      gzipBytes: gzipSync(bodyText).byteLength,
      threadPageKind: response.threadPageKind,
      threadPageItems: response.threadPageItems,
      threadPayloadBytes: response.threadPayloadBytes,
      threadPayloadGzipBytes: response.threadPayloadGzipBytes,
      startedAt,
      fulfilledAt: null,
      durationMs: null,
      critical: !response.sidecar,
      sidecar: response.sidecar,
      unknown: response.unknown,
    };
    apiCalls.push(call);
    const task = (async () => {
      const body = Buffer.from(bodyText).toString('base64');
      const latencyMs = latencyForResponse(response, options);
      if (latencyMs > 0) await sleep(latencyMs);
      await client.send('Fetch.fulfillRequest', {
        requestId: params.requestId,
        responseCode: response.status,
        responseHeaders: Object.entries(response.headers).map(([name, value]) => ({ name, value })),
        body,
      });
      call.fulfilledAt = performance.now();
      call.durationMs = call.fulfilledAt - call.startedAt;
    })().catch(async (error) => {
      await client.send('Fetch.failRequest', {
        requestId: params.requestId,
        errorReason: 'Failed',
      }).catch(() => {});
      call.status = 0;
      call.fulfilledAt = performance.now();
      call.durationMs = call.fulfilledAt - call.startedAt;
      call.unknown = true;
      call.error = error.message;
    }).finally(() => {
      pendingMocks.delete(task);
    });
    pendingMocks.add(task);
    void task;
  });

  return async () => {
    while (pendingMocks.size > 0) {
      await Promise.allSettled([...pendingMocks]);
      await sleep(0);
    }
    unsubscribe();
    if (instrumentation.identifier) {
      await client.send('Page.removeScriptToEvaluateOnNewDocument', {
        identifier: instrumentation.identifier,
      }).catch(() => {});
    }
    await client.send('Performance.disable').catch(() => {});
  };
}

function workspaceBootstrapCalls(calls) {
  return calls.filter((call) => call.method === 'GET' && call.path === '/api/cortex/bootstrap');
}

function directThreadBootstrapCalls(calls, ideaId) {
  return workspaceBootstrapCalls(calls).filter((call) => {
    const params = new URLSearchParams(call.query);
    const includes = new Set((params.get('include') || '').split(','));
    return includes.has('selected_idea') &&
      includes.has('direct_thread') &&
      params.get('idea_id') === ideaId;
  });
}

function workspaceStartupCalls(calls) {
  return calls.filter((call) => (
    call.path === '/api/cortex/bootstrap' ||
    call.path.startsWith('/api/workspace-apps') ||
    call.path.startsWith('/api/workspace-pins')
  ));
}

function assertRequestContract(condition, scenario, expectation, calls) {
  if (condition) return;
  const observed = calls.map((call) => `${call.method} ${call.path}${call.query || ''}`);
  throw new Error(
    `Request contract failed for ${scenario.name}: ${expectation}` +
    `\nObserved API requests:\n${observed.length ? observed.map((call) => `- ${call}`).join('\n') : '- none'}`,
  );
}

function directThreadRequestContract(callsAtReady, scenario, streamItems, streamContract) {
  const bootstrapCalls = workspaceBootstrapCalls(callsAtReady);
  const directBootstrapCalls = directThreadBootstrapCalls(callsAtReady, scenario.ideaId);
  const directBootstrap = directBootstrapCalls[0] ?? null;
  const meCall = callsAtReady.find((call) => call.method === 'GET' && call.path === '/api/me') ?? null;
  const startupPayloadKb = callsAtReady.reduce((sum, call) => sum + call.bytes, 0) / 1024;
  const deferredCallsAtReady = callsAtReady.filter((call) => (
    call.path.startsWith('/api/notifications') ||
    call.path === '/api/chat/notifications' ||
    call.path.includes('/project-context')
  )).length;
  const bootstrapBeforeMeFulfilled = Boolean(
    directBootstrap &&
    meCall &&
    Number.isFinite(meCall.fulfilledAt) &&
    directBootstrap.startedAt < meCall.fulfilledAt
  );
  const payloadBudgetKb = streamItems <= 80 ? DIRECT_THREAD_STARTUP_PAYLOAD_BUDGET_KB : null;

  return {
    kind: 'direct-thread-startup',
    passed: bootstrapCalls.length === 1 &&
      directBootstrapCalls.length === 1 &&
      bootstrapBeforeMeFulfilled &&
      deferredCallsAtReady === 0 &&
      callsAtReady.length <= DIRECT_THREAD_STARTUP_CALL_BUDGET &&
      (payloadBudgetKb === null || startupPayloadKb <= payloadBudgetKb),
    bootstrapBeforeMeFulfilled,
    deferredCallsAtReady,
    startupCalls: callsAtReady.length,
    startupPayloadKb,
    payloadBudgetKb,
    streamContract,
    initialBootstrapBytes: directBootstrap?.bytes ?? null,
    initialBootstrapGzipBytes: directBootstrap?.gzipBytes ?? null,
    initialThreadPayloadBytes: directBootstrap?.threadPayloadBytes ?? null,
    initialThreadPayloadGzipBytes: directBootstrap?.threadPayloadGzipBytes ?? null,
    initialThreadItems: directBootstrap?.threadPageItems ?? null,
  };
}

function isAppAssetRequest(params, baseUrl) {
  if (!APP_ASSET_RESOURCE_TYPES.has(params.type)) return false;
  try {
    return new URL(params.request.url).origin === new URL(baseUrl).origin;
  } catch {
    return false;
  }
}

function assetUrlSet(requests) {
  return new Set(requests.map((request) => request.url));
}

function assetPathSet(requests) {
  return new Set(requests.map((request) => request.path));
}

function displayAssetUrl(url, baseUrl) {
  try {
    const parsed = new URL(url);
    return parsed.origin === new URL(baseUrl).origin
      ? `${parsed.pathname}${parsed.search}`
      : parsed.toString();
  } catch {
    return url;
  }
}

function verifyManifestDeferredAssets(
  expectedClosure,
  expectedEntryAssets,
  assetRequests,
  selectionStartedAt,
) {
  const beforeSelection = assetPathSet(
    assetRequests.filter((request) => request.startedAt < selectionStartedAt),
  );
  const afterSelection = assetPathSet(
    assetRequests.filter((request) => request.startedAt >= selectionStartedAt),
  );
  const sharedBeforeSelection = expectedClosure.filter((asset) => beforeSelection.has(asset));
  const deferredBeforeSelection = expectedClosure.filter((asset) => !beforeSelection.has(asset));
  const missingAfterSelection = deferredBeforeSelection.filter((asset) => !afterSelection.has(asset));
  return {
    expectedClosure,
    expectedEntryAssets,
    entryAssetsBeforeSelection: expectedEntryAssets.filter((asset) => beforeSelection.has(asset)),
    entryAssetsMissingAfterSelection: expectedEntryAssets.filter((asset) => !afterSelection.has(asset)),
    sharedBeforeSelection,
    deferredBeforeSelection,
    requestedAfterSelection: deferredBeforeSelection.filter((asset) => afterSelection.has(asset)),
    missingAfterSelection,
  };
}

function assertAssetContract(condition, scenario, expectation, assetRequests, baseUrl) {
  if (condition) return;
  const observed = [...assetUrlSet(assetRequests)].map((url) => displayAssetUrl(url, baseUrl));
  throw new Error(
    `Asset contract failed for ${scenario.name}: ${expectation}` +
    `\nObserved app script/style assets:\n${observed.length ? observed.map((url) => `- ${url}`).join('\n') : '- none'}`,
  );
}

async function activateDefaultThreadPane(client, label, kind, readyExpression, timeoutMs) {
  const startedAt = performance.now();
  const clicked = await evaluate(client, `(() => {
    const tab = document.querySelector('button[role="tab"][title="${label}"]');
    if (!(tab instanceof HTMLElement)) return false;
    tab.click();
    return true;
  })()`);
  if (!clicked) throw new Error(`Default thread pane tab ${label} is unavailable`);
  await waitForExpression(
    client,
    `(() => Boolean(
      document.querySelector('.right-dock-content[data-active-tab="${kind}"]') &&
      (${readyExpression}) &&
      !document.querySelector('.right-dock-content[data-active-tab="${kind}"] .thread-lazy-pane-state')
    ))()`,
    timeoutMs,
  );
  return { pane: kind, readyMs: performance.now() - startedAt };
}

async function waitForApiCall(calls, startIndex, predicate, timeoutMs) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    const match = calls.slice(startIndex).find(predicate);
    if (match) return match;
    await sleep(25);
  }
  throw new Error('Timed out waiting for the deterministic composer API call');
}

async function verifyDirectThreadComposer(client, apiCalls, timeoutMs) {
  const message = 'Benchmark direct-thread composer contract';
  const apiStartIndex = apiCalls.length;
  const sendStartedAt = performance.now();
  const typed = await evaluate(client, `(() => {
    const textarea = document.querySelector('.thread-bridge-textarea');
    if (!(textarea instanceof HTMLTextAreaElement)) return false;
    const valueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    valueSetter?.call(textarea, ${JSON.stringify(message)});
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    return textarea.value === ${JSON.stringify(message)};
  })()`);
  if (!typed) throw new Error('The direct-thread composer could not type its deterministic message');
  await waitForExpression(
    client,
    `(() => {
      const send = document.querySelector('button[aria-label="Send"]');
      return send instanceof HTMLButtonElement && !send.disabled;
    })()`,
    timeoutMs,
  );
  const submitted = await evaluate(client, `(() => {
    const send = document.querySelector('button[aria-label="Send"]');
    if (!(send instanceof HTMLButtonElement) || send.disabled) return false;
    send.click();
    return true;
  })()`);
  if (!submitted) throw new Error('The direct-thread composer could not type and submit its deterministic message');

  await waitForApiCall(
    apiCalls,
    apiStartIndex,
    (call) => call.method === 'POST' && /^\/api\/cortex\/ideas\/[^/]+\/thread$/.test(call.path),
    timeoutMs,
  );
  await waitForApiCall(
    apiCalls,
    apiStartIndex,
    (call) => call.method === 'GET' && /^\/api\/cortex\/ideas\/[^/]+\/unified-stream$/.test(call.path),
    timeoutMs,
  );
  await waitForExpression(
    client,
    `(() => {
      const textarea = document.querySelector('.thread-bridge-textarea');
      return textarea instanceof HTMLTextAreaElement && textarea.value === '' && !textarea.disabled;
    })()`,
    timeoutMs,
  );
  return {
    passed: true,
    message,
    sendReadyMs: performance.now() - sendStartedAt,
  };
}

async function measureThreadHistoryContract(client, scenario, options) {
  if (!scenario.directThread) return { kind: 'not-applicable', passed: true, reveals: [] };

  return evaluate(client, `(async () => {
    const total = ${Math.max(options.streamItems, 1)};
    const batch = ${THREAD_HISTORY_WINDOW_SIZE};
    const transcript = document.querySelector('.thread-content');
    if (!(transcript instanceof HTMLElement)) return {
      kind: 'thread-history-window', passed: false, error: 'transcript unavailable', reveals: [],
    };
    const frame = () => new Promise((resolve) => requestAnimationFrame(resolve));
    const roots = () => [...document.querySelectorAll(
      '.message-stack > .thread-message, .message-stack > .run-insert',
    )];
    const historyButton = () => document.querySelector('.thread-history-window-control button');
    const controlVisible = () => historyButton() instanceof HTMLButtonElement;
    const idFor = (root) => Number(
      (root.textContent || '').match(/Benchmark (?:assistant reply|user prompt|run) (\\d+)/)?.[1],
    );
    const snapshot = () => {
      const rows = roots();
      const ids = rows.map(idFor).filter(Number.isFinite);
      const unique = new Set(ids);
      const firstExpected = total - ids.length;
      return {
        rendered: rows.length,
        ids,
        unique: unique.size,
        duplicates: ids.length - unique.size,
        valid: ids.length === rows.length && ids.every((id, index) => id === firstExpected + index),
      };
    };
    const rootFor = (id) => roots().find((root) => idFor(root) === id) ?? null;
    const bench = window.__illoBench || {};
    const waitForReveal = async (previous) => {
      const deadline = performance.now() + ${Math.min(options.timeoutMs, 5000)};
      while (performance.now() < deadline) {
        const button = historyButton();
        if (snapshot().unique > previous && button?.getAttribute('aria-busy') !== 'true') return true;
        await frame();
      }
      return false;
    };

    const initial = snapshot();
    const controlInitiallyVisible = controlVisible();
    const reveals = [];
    for (let index = 0; index <= Math.ceil(total / batch); index += 1) {
      const button = historyButton();
      if (!(button instanceof HTMLButtonElement)) break;
      transcript.scrollTop = 0;
      transcript.dispatchEvent(new Event('scroll'));
      await frame();
      const before = snapshot();
      const anchorId = before.ids[0];
      const anchorBefore = rootFor(anchorId);
      const topBefore = anchorBefore?.getBoundingClientRect().top;
      const longTaskStart = bench.longTasks?.length ?? 0;
      const startedAt = performance.now();
      button.click();
      const settled = await waitForReveal(before.unique);
      await frame();
      const after = snapshot();
      const beforeSet = new Set(before.ids);
      const afterSet = new Set(after.ids);
      const added = after.ids.filter((id) => !beforeSet.has(id)).length;
      const removed = before.ids.filter((id) => !afterSet.has(id)).length;
      const topAfter = rootFor(anchorId)?.getBoundingClientRect().top;
      const drift = Number.isFinite(topBefore) && Number.isFinite(topAfter)
        ? Math.abs(topAfter - topBefore) : null;
      const revealLongTasks = (bench.longTasks || []).slice(longTaskStart);
      const expectedAdded = Math.min(batch, total - before.unique);
      const behaviorPassed = settled && added === expectedAdded && removed === 0 &&
        after.duplicates === 0 && after.valid && controlVisible() === (after.unique < total) && drift !== null;
      reveals.push({
        revealMs: performance.now() - startedAt,
        viewportDriftPx: drift,
        beforeItems: before.unique,
        afterItems: after.unique,
        addedItems: added,
        removedItems: removed,
        longTaskCount: revealLongTasks.length,
        longTaskTotalMs: revealLongTasks.reduce((sum, task) => sum + task.duration, 0),
        maxLongTaskMs: Math.max(0, ...revealLongTasks.map((task) => task.duration)),
        behaviorPassed,
      });
      if (!settled || after.unique <= before.unique) break;
    }

    const final = snapshot();
    const expectedInitial = Math.min(total, batch);
    const expectedReveals = Math.max(0, Math.ceil(total / batch) - 1);
    const initialWindowPassed = initial.unique === expectedInitial &&
      initial.rendered === expectedInitial && initial.duplicates === 0 && initial.valid;
    const controlVisibilityPassed = controlInitiallyVisible === (total > batch);
    const repeatedRevealPassed = reveals.length === expectedReveals &&
      reveals.every((reveal) => reveal.behaviorPassed) &&
      final.unique === total && final.rendered === total && final.duplicates === 0 &&
      final.valid && !controlVisible();

    return {
      kind: 'thread-history-window',
      passed: initialWindowPassed && controlVisibilityPassed && repeatedRevealPassed,
      initialRenderedIdentityCount: initial.unique,
      initialRenderedItemCount: initial.rendered,
      initialWindowPassed,
      controlVisibilityPassed,
      finalRenderedIdentityCount: final.unique,
      repeatedRevealPassed,
      longTaskObserverAvailable: bench.longTaskObserverAvailable === true,
      reveals,
    };
  })()`);
}

async function measureLazyAssetContract(
  client,
  scenarioKey,
  scenario,
  assetRequests,
  apiCalls,
  options,
  lazyAssetClosures,
) {
  if (scenarioKey !== 'workspace' && scenarioKey !== 'thread') {
    return { kind: 'not-applicable', passed: true };
  }

  await sleep(THREAD_STAGE_PREWARM_OBSERVATION_MS);

  if (scenarioKey === 'workspace') {
    const selectionStartedAt = performance.now();
    const threadClicked = await evaluate(client, `(() => {
      const thread = document.querySelector('[data-constellation-signal-id="idea-1"]');
      if (!(thread instanceof HTMLElement)) return false;
      thread.click();
      return true;
    })()`);
    assertAssetContract(
      threadClicked === true,
      scenario,
      'the deterministic workspace thread control must be available after the prewarm observation window',
      assetRequests,
      options.baseUrl,
    );
    await waitForExpression(client, THREAD_READY_EXPRESSION, options.timeoutMs);
    const firstOpenMs = performance.now() - selectionStartedAt;
    const manifestProof = verifyManifestDeferredAssets(
      lazyAssetClosures.threadStage.assets,
      lazyAssetClosures.threadStage.entryAssets,
      assetRequests,
      selectionStartedAt,
    );
    assertAssetContract(
      manifestProof.entryAssetsBeforeSelection.length === 0 &&
        manifestProof.entryAssetsMissingAfterSelection.length === 0 &&
        manifestProof.deferredBeforeSelection.length > 0 &&
        manifestProof.missingAfterSelection.length === 0,
      scenario,
      `the manifest-derived ThreadStage entry assets must remain absent for ${THREAD_STAGE_PREWARM_OBSERVATION_MS}ms and every deferred closure asset must load after thread selection; entry assets before selection: ${manifestProof.entryAssetsBeforeSelection.join(', ') || 'none'}; missing after selection: ${[...manifestProof.entryAssetsMissingAfterSelection, ...manifestProof.missingAfterSelection].join(', ') || 'none'}`,
      assetRequests,
      options.baseUrl,
    );

    return {
      kind: 'thread-stage-loads-on-selection',
      passed: true,
      observationMs: THREAD_STAGE_PREWARM_OBSERVATION_MS,
      assetsBeforeSelection: assetPathSet(
        assetRequests.filter((request) => request.startedAt < selectionStartedAt),
      ).size,
      manifestModuleId: lazyAssetClosures.threadStage.moduleId,
      manifestExpectedAssetCount: manifestProof.expectedClosure.length,
      manifestExpectedAssets: manifestProof.expectedClosure,
      manifestExpectedEntryAssets: manifestProof.expectedEntryAssets,
      manifestEntryAssetsBeforeSelection: manifestProof.entryAssetsBeforeSelection,
      manifestSharedAssetCountBeforeSelection: manifestProof.sharedBeforeSelection.length,
      manifestDeferredAssetsBeforeSelection: manifestProof.deferredBeforeSelection,
      selectionTriggeredAssetCount: manifestProof.requestedAfterSelection.length,
      selectionTriggeredAssets: manifestProof.requestedAfterSelection,
      threadStageFirstOpenMs: firstOpenMs,
    };
  }

  const panelClicked = await evaluate(client, `(() => {
    const toggle = document.querySelector('button[aria-label="Show side panel"]');
    if (!(toggle instanceof HTMLElement)) return false;
    toggle.click();
    return true;
  })()`);
  assertAssetContract(
    panelClicked === true,
    scenario,
    'the deterministic side-panel toggle must be available',
    assetRequests,
    options.baseUrl,
  );
  await waitForExpression(
    client,
    `Boolean(document.querySelector('.cortex-thread-stage-right-dock'))`,
    options.timeoutMs,
  );

  const defaultPaneInteractions = [];
  await waitForExpression(
    client,
    `(() => {
      const body = document.querySelector('.right-dock-content[data-active-tab="activity"]')?.textContent || '';
      return Boolean(
        document.querySelector('.right-dock-content[data-active-tab="activity"] .panel-utility-content-bare') &&
        !body.includes('Loading activity')
      );
    })()`,
    options.timeoutMs,
  );
  defaultPaneInteractions.push({ pane: 'activity-initial', readyMs: 0 });
  defaultPaneInteractions.push(await activateDefaultThreadPane(
    client,
    'Discussion',
    'discussion',
    `document.querySelector('.right-dock-content[data-active-tab="discussion"] .thread-discussion-pane')`,
    options.timeoutMs,
  ));
  defaultPaneInteractions.push(await activateDefaultThreadPane(
    client,
    'Handoff',
    'handoff-summary',
    `document.querySelector('.right-dock-content[data-active-tab="handoff-summary"] .panel-utility-content-bare') &&
      !(document.querySelector('.right-dock-content[data-active-tab="handoff-summary"]')?.textContent || '').includes('Loading handoff')`,
    options.timeoutMs,
  ));
  defaultPaneInteractions.push(await activateDefaultThreadPane(
    client,
    'Activity',
    'activity',
    `document.querySelector('.right-dock-content[data-active-tab="activity"] .panel-utility-content-bare') &&
      !(document.querySelector('.right-dock-content[data-active-tab="activity"]')?.textContent || '').includes('Loading activity')`,
    options.timeoutMs,
  ));

  const addMenuClicked = await evaluate(client, `(() => {
    const add = document.querySelector('button[aria-label="Add side panel tab"]');
    if (!(add instanceof HTMLElement)) return false;
    add.click();
    return true;
  })()`);
  assertAssetContract(
    addMenuClicked === true,
    scenario,
    'the deterministic add-pane control must be available',
    assetRequests,
    options.baseUrl,
  );
  await waitForExpression(
    client,
    `Boolean(document.querySelector('.right-dock-add-menu[aria-label="Add side panel tab"]'))`,
    options.timeoutMs,
  );
  await sleep(120);

  const selectionStartedAt = performance.now();
  const rarePaneClicked = await evaluate(client, `(() => {
    const items = [...document.querySelectorAll('.right-dock-add-menu [role="menuitem"]')];
    const vault = items.find((item) => item.querySelector('strong')?.textContent?.trim() === 'Vault');
    if (!(vault instanceof HTMLElement)) return false;
    vault.click();
    return true;
  })()`);
  assertAssetContract(
    rarePaneClicked === true,
    scenario,
    'the deterministic Vault pane menu item must be available',
    assetRequests,
    options.baseUrl,
  );

  await waitForExpression(client, VAULT_PANE_MOUNTED_EXPRESSION, options.timeoutMs);
  const firstOpenMs = performance.now() - selectionStartedAt;
  const manifestProof = verifyManifestDeferredAssets(
    lazyAssetClosures.vault.assets,
    lazyAssetClosures.vault.entryAssets,
    assetRequests,
    selectionStartedAt,
  );
  assertAssetContract(
    manifestProof.entryAssetsBeforeSelection.length === 0 &&
      manifestProof.entryAssetsMissingAfterSelection.length === 0 &&
      manifestProof.deferredBeforeSelection.length > 0 &&
      manifestProof.missingAfterSelection.length === 0,
    scenario,
    `the manifest-derived Vault entry assets must be absent before selection and every deferred closure asset must be fetched by the first selection; entry assets before selection: ${manifestProof.entryAssetsBeforeSelection.join(', ') || 'none'}; missing after selection: ${[...manifestProof.entryAssetsMissingAfterSelection, ...manifestProof.missingAfterSelection].join(', ') || 'none'}`,
    assetRequests,
    options.baseUrl,
  );
  await waitForExpression(client, VAULT_PANE_READY_EXPRESSION, options.timeoutMs);
  const dataReadyMs = performance.now() - selectionStartedAt;
  const composerContract = await verifyDirectThreadComposer(client, apiCalls, options.timeoutMs);

  return {
    kind: 'rare-pane-loads-on-selection',
    passed: true,
    observationMs: THREAD_STAGE_PREWARM_OBSERVATION_MS,
    pane: 'vault',
    assetsBeforeSelection: assetPathSet(
      assetRequests.filter((request) => request.startedAt < selectionStartedAt),
    ).size,
    manifestModuleId: lazyAssetClosures.vault.moduleId,
    manifestExpectedAssetCount: manifestProof.expectedClosure.length,
    manifestExpectedAssets: manifestProof.expectedClosure,
    manifestExpectedEntryAssets: manifestProof.expectedEntryAssets,
    manifestEntryAssetsBeforeSelection: manifestProof.entryAssetsBeforeSelection,
    manifestSharedAssetCountBeforeSelection: manifestProof.sharedBeforeSelection.length,
    manifestDeferredAssetsBeforeSelection: manifestProof.deferredBeforeSelection,
    rarePaneAssetsBeforeSelection: 0,
    selectionTriggeredAssetCount: manifestProof.requestedAfterSelection.length,
    selectionTriggeredAssets: manifestProof.requestedAfterSelection,
    rarePaneFirstOpenMs: firstOpenMs,
    rarePaneDataReadyMs: dataReadyMs,
    rarePaneFirstOpenBudgetMs: RARE_PANE_OPEN_BUDGET_MS,
    defaultPaneInteractions,
    composerContract,
  };
}

async function parkPageBetweenRuns(client, timeoutMs) {
  await client.send('Page.navigate', { url: 'about:blank' });
  await waitForExpression(
    client,
    `location.href === 'about:blank' && document.readyState === 'complete'`,
    Math.min(timeoutMs, 5000),
  );
}

async function runScenario(client, scenarioKey, fixture, options, measured, lazyAssetClosures) {
  const scenario = SCENARIOS[scenarioKey];
  const apiCalls = [];
  const assetRequests = [];
  const unsubscribers = [];
  const requestUrls = new Map();
  const networkFailures = [];
  const consoleMessages = [];
  unsubscribers.push(
    client.on('Network.requestWillBeSent', (params) => {
      requestUrls.set(params.requestId, params.request?.url);
      if (isAppAssetRequest(params, options.baseUrl)) {
        assetRequests.push({
          url: params.request.url,
          path: new URL(params.request.url).pathname,
          type: params.type,
          startedAt: performance.now(),
        });
      }
    }),
    client.on('Network.loadingFailed', (params) => {
      networkFailures.push({
        url: requestUrls.get(params.requestId) || params.requestId,
        errorText: params.errorText,
        blockedReason: params.blockedReason,
        canceled: params.canceled,
      });
    }),
    client.on('Runtime.consoleAPICalled', (params) => {
      consoleMessages.push({
        type: params.type,
        text: (params.args || []).map((arg) => arg.value || arg.description || '').join(' '),
      });
    }),
    client.on('Runtime.exceptionThrown', (params) => {
      consoleMessages.push({
        type: 'exception',
        text: params.exceptionDetails?.text || params.exceptionDetails?.exception?.description || 'exception',
      });
    }),
  );
  unsubscribers.push(await configurePage(client, fixture, options, apiCalls));

  try {
    const origin = new URL(options.baseUrl).origin;
    await client.send('Storage.clearDataForOrigin', {
      origin,
      storageTypes: 'local_storage,indexeddb,websql,service_workers,cache_storage',
    }).catch(() => {});
    const performanceMetricsBefore = await performanceMetrics(client);

    const targetUrl = new URL(scenario.path, options.baseUrl).toString();
    const started = performance.now();
    await client.send('Page.navigate', { url: targetUrl });
    try {
      await waitForExpression(client, scenario.readyExpression, options.timeoutMs);
    } catch (error) {
      error.message +=
        `\nNetwork failures:\n${JSON.stringify(networkFailures.slice(-20), null, 2)}` +
        `\nConsole:\n${JSON.stringify(consoleMessages.slice(-20), null, 2)}`;
      throw error;
    }
    const readyMs = performance.now() - started;
    const apiCallsAtReady = [...apiCalls];
    const bootstrapCallsAtReady = workspaceBootstrapCalls(apiCallsAtReady);
    const workspaceCallsAtReady = workspaceStartupCalls(apiCallsAtReady);
    const directStartupContract = scenario.directThread
      ? directThreadRequestContract(apiCallsAtReady, scenario, options.streamItems, options.streamContract)
      : null;

    if (scenario.modalId) {
      assertRequestContract(
        workspaceCallsAtReady.length === 0,
        scenario,
        'cold modal readiness must occur before any workspace startup API request',
        apiCallsAtReady,
      );
    } else if (!scenario.directThread) {
      assertRequestContract(
        bootstrapCallsAtReady.length >= 1,
        scenario,
        'workspace and direct-thread routes must request workspace bootstrap before readiness',
        apiCallsAtReady,
      );
    }

    await sleep(Math.max(80, options.sidecarLatencyMs ?? options.apiLatencyMs) + 80);
    const pageMetrics = await evaluate(client, `(() => {
      const paints = Object.fromEntries(performance.getEntriesByType('paint').map((entry) => [entry.name, entry.startTime]));
      const navigation = performance.getEntriesByType('navigation')[0];
      const bench = window.__illoBench || {};
      return {
        domNodes: document.getElementsByTagName('*').length,
        deepFieldFeatureNodes: document.querySelectorAll('.cortex-deep-field__star, .cortex-deep-field__star-glow, .cortex-deep-field__fleck').length,
        d3ShadowNodes: document.querySelectorAll('.cortex-svg-d3-layer svg *').length,
        d3ShadowBubbles: document.querySelectorAll('.cortex-svg-d3-layer .bubble-group').length,
        d3ShadowConnections: document.querySelectorAll('.cortex-svg-d3-layer .connection-path').length,
        primitiveBlobs: document.querySelectorAll('[data-constellation-signal-id]').length,
        fcpMs: paints['first-contentful-paint'] ?? null,
        lcpMs: bench.largestContentfulPaint?.startTime ?? null,
        longTaskObserverAvailable: bench.longTaskObserverAvailable === true,
        longTaskObserverError: bench.longTaskObserverError ?? null,
        longTaskCount: bench.longTaskObserverAvailable ? bench.longTasks.length : null,
        longTaskTotalMs: bench.longTaskObserverAvailable
          ? bench.longTasks.reduce((sum, task) => sum + task.duration, 0)
          : null,
        maxLongTaskMs: bench.longTaskObserverAvailable
          ? Math.max(0, ...bench.longTasks.map((task) => task.duration))
          : null,
        errors: bench.errors || [],
        wsMessages: bench.wsMessages || [],
        transferSize: navigation?.transferSize ?? 0,
        decodedBodySize: navigation?.decodedBodySize ?? 0,
      };
    })()`);
    const workspaceIdleContract = options.workspaceIdle
      ? await measureWorkspaceIdleContract(client, scenario, options)
      : null;
    const performanceMetricsAfter = await performanceMetrics(client);
    const taskDurationMs = performanceDurationMs(
      performanceMetricsBefore,
      performanceMetricsAfter,
      'TaskDuration',
    );
    const historyApiStartIndex = apiCalls.length;
    const historyPerformanceBefore = await performanceMetrics(client);
    const historyStartedAt = performance.now();
    const historyContract = await measureThreadHistoryContract(client, scenario, options);
    const historyPerformanceAfter = await performanceMetrics(client);
    historyContract.wallMs = performance.now() - historyStartedAt;
    historyContract.taskDurationMs = performanceDurationMs(
      historyPerformanceBefore,
      historyPerformanceAfter,
      'TaskDuration',
    );
    const olderPageCalls = apiCalls.slice(historyApiStartIndex).filter(
      (call) => call.threadPageKind === 'older',
    );
    historyContract.remotePageRequestCount = olderPageCalls.length;
    historyContract.remotePageBytes = olderPageCalls.map((call) => call.threadPayloadBytes);
    historyContract.remotePageGzipBytes = olderPageCalls.map((call) => call.threadPayloadGzipBytes);
    historyContract.remotePageFetchMs = olderPageCalls.map((call) => call.durationMs);
    historyContract.remotePageItems = olderPageCalls.map((call) => call.threadPageItems);
    historyContract.reveals.forEach((reveal, index) => {
      reveal.fetchMs = olderPageCalls[index]?.durationMs ?? null;
      reveal.postFetchMs = Number.isFinite(reveal.fetchMs) ? reveal.revealMs - reveal.fetchMs : null;
    });

    const measuredApiCalls = [...apiCalls];
    const bootstrapCallsBeforeClose = workspaceBootstrapCalls(measuredApiCalls);
    const workspaceCallsBeforeClose = workspaceStartupCalls(measuredApiCalls);
    const lazyAssetContract = await measureLazyAssetContract(
      client,
      scenarioKey,
      scenario,
      assetRequests,
      apiCalls,
      options,
      lazyAssetClosures,
    );
    let postCloseReadyMs = null;
    let postCloseApiCalls = [];

    if (scenario.modalId) {
      assertRequestContract(
        workspaceCallsBeforeClose.length === 0,
        scenario,
        'the modal measurement phase must not download workspace startup data',
        measuredApiCalls,
      );

      const closeClicked = await evaluate(client, `(() => {
        const close = document.querySelector('.workspace-page-modal__actions [aria-label="Close page"]');
        if (!(close instanceof HTMLElement)) return false;
        close.click();
        return true;
      })()`);
      assertRequestContract(
        closeClicked === true,
        scenario,
        'the deterministic close-modal control must be available',
        measuredApiCalls,
      );

      const closeStarted = performance.now();
      await waitForExpression(client, WORKSPACE_READY_EXPRESSION, options.timeoutMs);
      postCloseReadyMs = performance.now() - closeStarted;
      await sleep(Math.max(400, (options.sidecarLatencyMs ?? options.apiLatencyMs) + 160));

      postCloseApiCalls = apiCalls.slice(measuredApiCalls.length);
      const postCloseBootstrapCalls = workspaceBootstrapCalls(postCloseApiCalls);
      assertRequestContract(
        postCloseBootstrapCalls.length === 1,
        scenario,
        `closing the modal must trigger exactly one workspace bootstrap request; observed ${postCloseBootstrapCalls.length}`,
        postCloseApiCalls,
      );
    }

    const allApiCalls = [...apiCalls];
    const allErrors = await evaluate(client, 'window.__illoBench?.errors || []').catch(() => []);
    const requestContract = {
      ...(directStartupContract ?? {
        kind: scenario.modalId ? 'modal-defers-workspace' : 'workspace-starts-immediately',
        passed: true,
      }),
      workspaceBootstrapAtReady: bootstrapCallsAtReady.length,
      workspaceBootstrapBeforeClose: bootstrapCallsBeforeClose.length,
      workspaceBootstrapPostClose: workspaceBootstrapCalls(postCloseApiCalls).length,
      workspaceStartupAtReady: workspaceCallsAtReady.length,
      workspaceStartupBeforeClose: workspaceCallsBeforeClose.length,
      workspaceStartupPostClose: workspaceStartupCalls(postCloseApiCalls).length,
      requestsAtReady: apiCallsAtReady.length,
      requestsMeasured: measuredApiCalls.length,
      requestsPostClose: postCloseApiCalls.length,
    };

    return {
      scenario: scenario.name,
      measured,
      readyMs,
      postCloseReadyMs,
      requestContract,
      workspaceIdleContract,
      lazyAssetContract,
      historyContract,
      apiCallCount: measuredApiCalls.length,
      postCloseApiCallCount: postCloseApiCalls.length,
      totalApiCallCount: allApiCalls.length,
      uniqueApiRoutes: new Set(measuredApiCalls.map((call) => call.label)).size,
      sidecarApiCallCount: measuredApiCalls.filter((call) => call.sidecar).length,
      unknownApiCalls: allApiCalls.filter((call) => call.unknown),
      apiCalls: measuredApiCalls,
      postCloseApiCalls,
      allApiCalls,
      ...pageMetrics,
      taskDurationMs,
      errors: [...new Set([...(pageMetrics.errors || []), ...(allErrors || [])])],
    };
  } finally {
    try {
      await parkPageBetweenRuns(client, options.timeoutMs);
    } finally {
      for (const unsubscribe of unsubscribers) await unsubscribe();
      await client.send('Fetch.disable').catch(() => {});
    }
  }
}

function summarizeScenario(samples) {
  const measuredSamples = samples.filter((sample) => sample.measured);
  const apiLabels = new Map();
  const allApiLabels = new Map();
  const postCloseApiLabels = new Map();
  const unknownApiLabels = new Set();
  const callWindows = [];

  function recordCalls(target, calls) {
    for (const call of calls) {
      const entry = target.get(call.label) ?? { count: 0, bytes: 0 };
      entry.count += 1;
      entry.bytes += call.bytes;
      target.set(call.label, entry);
    }
  }

  for (const sample of measuredSamples) {
    const calls = sample.apiCalls;
    if (calls.length) {
      callWindows.push({
        first: Math.min(...calls.map((call) => call.startedAt ?? 0)),
        last: Math.max(...calls.map((call) => call.fulfilledAt ?? 0)),
      });
    }
    recordCalls(apiLabels, calls);
    recordCalls(allApiLabels, sample.allApiCalls ?? calls);
    recordCalls(postCloseApiLabels, sample.postCloseApiCalls ?? []);
    for (const call of sample.unknownApiCalls ?? []) {
      unknownApiLabels.add(call.label);
    }
  }

  function routeSummaries(labels) {
    return [...labels.entries()]
      .map(([label, entry]) => ({
        label,
        total_calls: entry.count,
        avg_calls_per_run: entry.count / Math.max(measuredSamples.length, 1),
        avg_kb_per_run: entry.bytes / Math.max(measuredSamples.length, 1) / 1024,
      }))
      .sort((a, b) => b.total_calls - a.total_calls || a.label.localeCompare(b.label));
  }

  const routes = routeSummaries(apiLabels);
  const allRoutes = routeSummaries(allApiLabels);
  const postCloseRoutes = routeSummaries(postCloseApiLabels);
  const threadStageFirstOpenMs = summarizeNumbers(
    measuredSamples
      .map((sample) => sample.lazyAssetContract?.threadStageFirstOpenMs ?? 0)
      .filter((value) => value > 0),
  );
  const rarePaneFirstOpenMs = summarizeNumbers(
    measuredSamples
      .map((sample) => sample.lazyAssetContract?.rarePaneFirstOpenMs ?? 0)
      .filter((value) => value > 0),
  );
  const rarePaneDataReadyMs = summarizeNumbers(
    measuredSamples
      .map((sample) => sample.lazyAssetContract?.rarePaneDataReadyMs ?? 0)
      .filter((value) => value > 0),
  );
  const selectionTriggeredAssets = [...new Set(
    measuredSamples.flatMap((sample) => sample.lazyAssetContract?.selectionTriggeredAssets ?? []),
  )].sort();
  const directThreadContractSamples = measuredSamples.filter(
    (sample) => sample.lazyAssetContract?.kind === 'rare-pane-loads-on-selection',
  );
  const directStartupSamples = measuredSamples.filter(
    (sample) => sample.requestContract?.kind === 'direct-thread-startup',
  );
  const historySamples = measuredSamples.filter(
    (sample) => sample.historyContract?.kind === 'thread-history-window',
  );
  const historyReveals = historySamples.flatMap(
    (sample) => sample.historyContract.reveals ?? [],
  );
  const historyRevealArraysAvailable = historySamples.length > 0 && historySamples.every(
    (sample) => Array.isArray(sample.historyContract.reveals),
  );
  const historyValues = (items, key) => items
    .map((item) => item[key])
    .filter(Number.isFinite);
  const summarizeHistory = (key) => {
    const values = historyValues(historySamples.map((sample) => sample.historyContract), key);
    return values.length > 0 && values.length === historySamples.length
      ? summarizeNumbers(values)
      : null;
  };
  const revealValues = (key) => historyValues(historyReveals, key);
  const remotePageValues = (key) => historySamples.flatMap(
    (sample) => sample.historyContract[key] ?? [],
  ).filter(Number.isFinite);
  const defaultPaneKinds = ['activity-initial', 'discussion', 'handoff-summary', 'activity'];
  const defaultPaneReadyMs = Object.fromEntries(defaultPaneKinds.map((kind) => [
    kind,
    summarizeNumbers(directThreadContractSamples.flatMap((sample) => (
      sample.lazyAssetContract.defaultPaneInteractions
        ?.filter((interaction) => interaction.pane === kind)
        .map((interaction) => interaction.readyMs) ?? []
    ))),
  ]));
  const finiteValues = (key) => measuredSamples
    .map((sample) => sample[key])
    .filter(Number.isFinite);
  const fcpValues = finiteValues('fcpMs').filter((value) => value > 0);
  const taskDurationValues = finiteValues('taskDurationMs');
  const longTaskCountValues = finiteValues('longTaskCount');
  const longTaskTotalValues = finiteValues('longTaskTotalMs');
  const maxLongTaskValues = finiteValues('maxLongTaskMs');
  const domNodeValues = finiteValues('domNodes');
  const workspaceIdleSamples = measuredSamples
    .map((sample) => sample.workspaceIdleContract)
    .filter(Boolean);
  const workspaceIdleValues = (path) => workspaceIdleSamples
    .map((sample) => path.reduce((value, key) => value?.[key], sample))
    .filter(Number.isFinite);
  const summarizeWorkspaceIdle = (path) => summarizeNumbers(workspaceIdleValues(path));
  const workspaceInteractionSamples = workspaceIdleSamples.filter((sample) => sample.mutation);
  const workspaceIdleTelemetryAvailable = workspaceIdleSamples.length === measuredSamples.length &&
    workspaceIdleSamples.every((sample) => sample.available === true);
  const workspaceIdleNoPeriodicWake = workspaceIdleSamples.length === measuredSamples.length &&
    workspaceIdleSamples.every((sample) => (
    sample.idle?.available === true &&
    sample.idle.rafCallbacks === 0 &&
    sample.idle.signalStyleWrites === 0 &&
    sample.hidden?.available === true &&
    sample.hidden.rafCallbacks === 0 &&
    sample.hidden.signalStyleWrites === 0 &&
    sample.hidden.resumeRafCallbacks === 0 &&
    sample.hidden.resumeSignalStyleWrites === 0
  ));
  const wakeSummary = (key) => summarizeNumbers(
    workspaceInteractionSamples.map((sample) => sample[key]?.resumeMs).filter(Number.isFinite),
  );
  const mutationWake = wakeSummary('mutation');
  const dragWake = wakeSummary('drag');
  const workspaceIdleInteractionContractPassed = workspaceInteractionSamples.length === 0 || (
    workspaceInteractionSamples.length === measuredSamples.length &&
    workspaceInteractionSamples.every((sample) => sample.mutation?.available && sample.drag?.available) &&
    mutationWake.p95 <= WORKSPACE_IDLE_WAKE_BUDGET_MS && dragWake.p95 <= WORKSPACE_IDLE_WAKE_BUDGET_MS
  );

  return {
    runs: measuredSamples.length,
    ready_ms: summarizeNumbers(measuredSamples.map((sample) => sample.readyMs)),
    post_close_ready_ms: summarizeNumbers(
      measuredSamples.map((sample) => sample.postCloseReadyMs ?? 0).filter((value) => value > 0),
    ),
    fcp_available: fcpValues.length === measuredSamples.length,
    fcp_ms: summarizeNumbers(fcpValues),
    lcp_ms: summarizeNumbers(measuredSamples.map((sample) => sample.lcpMs ?? 0).filter((value) => value > 0)),
    api_calls: summarizeNumbers(measuredSamples.map((sample) => sample.apiCalls.length)),
    sidecar_api_calls: summarizeNumbers(measuredSamples.map((sample) => sample.apiCalls.filter((call) => call.sidecar).length)),
    critical_api_calls: summarizeNumbers(measuredSamples.map((sample) => sample.apiCalls.filter((call) => call.critical).length)),
    api_kb: summarizeNumbers(measuredSamples.map((sample) => sample.apiCalls.reduce((sum, call) => sum + call.bytes, 0) / 1024)),
    api_window_ms: summarizeNumbers(callWindows.map((window) => window.last - window.first)),
    unique_api_routes: summarizeNumbers(measuredSamples.map((sample) => new Set(sample.apiCalls.map((call) => call.label)).size)),
    post_close_api_calls: summarizeNumbers(measuredSamples.map((sample) => sample.postCloseApiCalls?.length ?? 0)),
    total_api_calls: summarizeNumbers(measuredSamples.map((sample) => sample.allApiCalls?.length ?? sample.apiCalls.length)),
    workspace_bootstrap_at_ready: summarizeNumbers(
      measuredSamples.map((sample) => sample.requestContract?.workspaceBootstrapAtReady ?? 0),
    ),
    workspace_bootstrap_before_close: summarizeNumbers(
      measuredSamples.map((sample) => sample.requestContract?.workspaceBootstrapBeforeClose ?? 0),
    ),
    workspace_bootstrap_post_close: summarizeNumbers(
      measuredSamples.map((sample) => sample.requestContract?.workspaceBootstrapPostClose ?? 0),
    ),
    workspace_startup_at_ready: summarizeNumbers(
      measuredSamples.map((sample) => sample.requestContract?.workspaceStartupAtReady ?? 0),
    ),
    workspace_startup_before_close: summarizeNumbers(
      measuredSamples.map((sample) => sample.requestContract?.workspaceStartupBeforeClose ?? 0),
    ),
    workspace_startup_post_close: summarizeNumbers(
      measuredSamples.map((sample) => sample.requestContract?.workspaceStartupPostClose ?? 0),
    ),
    startup_api_calls: summarizeNumbers(directStartupSamples.map((sample) => sample.requestContract.startupCalls)),
    startup_api_kb: summarizeNumbers(directStartupSamples.map((sample) => sample.requestContract.startupPayloadKb)),
    initial_direct_bootstrap_bytes: summarizeNumbers(
      directStartupSamples.map((sample) => sample.requestContract.initialBootstrapBytes).filter(Number.isFinite),
    ),
    initial_direct_bootstrap_gzip_bytes: summarizeNumbers(
      directStartupSamples.map((sample) => sample.requestContract.initialBootstrapGzipBytes).filter(Number.isFinite),
    ),
    initial_thread_payload_bytes: summarizeNumbers(
      directStartupSamples.map((sample) => sample.requestContract.initialThreadPayloadBytes).filter(Number.isFinite),
    ),
    initial_thread_payload_gzip_bytes: summarizeNumbers(
      directStartupSamples.map((sample) => sample.requestContract.initialThreadPayloadGzipBytes).filter(Number.isFinite),
    ),
    initial_thread_items: summarizeNumbers(
      directStartupSamples.map((sample) => sample.requestContract.initialThreadItems).filter(Number.isFinite),
    ),
    direct_bootstrap_before_me_fulfilled: directStartupSamples.every(
      (sample) => sample.requestContract.bootstrapBeforeMeFulfilled,
    ),
    request_contract_passed: measuredSamples.every((sample) => sample.requestContract?.passed === true),
    lazy_asset_contract_passed: measuredSamples.every((sample) => sample.lazyAssetContract?.passed === true),
    lazy_asset_observation_ms: Math.max(
      0,
      ...measuredSamples.map((sample) => sample.lazyAssetContract?.observationMs ?? 0),
    ),
    assets_before_lazy_selection: summarizeNumbers(
      measuredSamples
        .map((sample) => sample.lazyAssetContract?.assetsBeforeSelection ?? 0)
        .filter((value) => value > 0),
    ),
    selection_triggered_asset_count: summarizeNumbers(
      measuredSamples
        .map((sample) => sample.lazyAssetContract?.selectionTriggeredAssetCount ?? 0)
        .filter((value) => value > 0),
    ),
    selection_triggered_assets: selectionTriggeredAssets,
    manifest_expected_asset_count: summarizeNumbers(
      measuredSamples
        .map((sample) => sample.lazyAssetContract?.manifestExpectedAssetCount ?? 0)
        .filter((value) => value > 0),
    ),
    manifest_expected_entry_assets: [...new Set(
      measuredSamples.flatMap(
        (sample) => sample.lazyAssetContract?.manifestExpectedEntryAssets ?? [],
      ),
    )].sort(),
    manifest_entry_assets_before_selection: [...new Set(
      measuredSamples.flatMap(
        (sample) => sample.lazyAssetContract?.manifestEntryAssetsBeforeSelection ?? [],
      ),
    )].sort(),
    manifest_shared_asset_count_before_selection: summarizeNumbers(
      measuredSamples
        .map((sample) => sample.lazyAssetContract?.manifestSharedAssetCountBeforeSelection ?? 0)
        .filter((value) => value > 0),
    ),
    manifest_deferred_assets_before_selection: [...new Set(
      measuredSamples.flatMap(
        (sample) => sample.lazyAssetContract?.manifestDeferredAssetsBeforeSelection ?? [],
      ),
    )].sort(),
    thread_stage_first_open_ms: threadStageFirstOpenMs,
    rare_pane_assets_before_selection: summarizeNumbers(
      measuredSamples
        .filter((sample) => sample.lazyAssetContract?.kind === 'rare-pane-loads-on-selection')
        .map((sample) => sample.lazyAssetContract?.rarePaneAssetsBeforeSelection ?? 0),
    ),
    rare_pane_first_open_ms: rarePaneFirstOpenMs,
    rare_pane_data_ready_ms: rarePaneDataReadyMs,
    rare_pane_first_open_budget_ms: RARE_PANE_OPEN_BUDGET_MS,
    rare_pane_first_open_budget_passed: rarePaneFirstOpenMs.p75 === 0 || rarePaneFirstOpenMs.p75 <= RARE_PANE_OPEN_BUDGET_MS,
    default_pane_contract_passed: directThreadContractSamples.every((sample) => (
      defaultPaneKinds.every((kind) => (
        sample.lazyAssetContract.defaultPaneInteractions?.some((interaction) => interaction.pane === kind)
      ))
    )),
    default_pane_ready_ms: defaultPaneReadyMs,
    composer_contract_passed: directThreadContractSamples.every(
      (sample) => sample.lazyAssetContract.composerContract?.passed === true,
    ),
    composer_send_ready_ms: summarizeNumbers(
      directThreadContractSamples
        .map((sample) => sample.lazyAssetContract.composerContract?.sendReadyMs ?? 0)
        .filter((value) => value > 0),
    ),
    thread_history_contract_passed: measuredSamples.every(
      (sample) => sample.historyContract?.passed === true,
    ),
    thread_history_initial_rendered_identity_count: summarizeHistory('initialRenderedIdentityCount'),
    thread_history_initial_rendered_item_count: summarizeHistory('initialRenderedItemCount'),
    thread_history_final_rendered_identity_count: summarizeHistory('finalRenderedIdentityCount'),
    thread_history_reveal_available: historyRevealArraysAvailable &&
      revealValues('revealMs').length === historyReveals.length,
    thread_history_reveal_ms: summarizeNumbers(revealValues('revealMs')),
    thread_history_viewport_drift_available: historyRevealArraysAvailable &&
      revealValues('viewportDriftPx').length === historyReveals.length,
    thread_history_viewport_drift_px: summarizeNumbers(revealValues('viewportDriftPx')),
    thread_history_task_duration_available: historySamples.every(
      (sample) => Number.isFinite(sample.historyContract.taskDurationMs),
    ),
    thread_history_task_duration_ms: summarizeNumbers(
      historySamples.map((sample) => sample.historyContract.taskDurationMs).filter(Number.isFinite),
    ),
    thread_history_long_task_observer_available: historySamples.every(
      (sample) => sample.historyContract.longTaskObserverAvailable === true,
    ),
    thread_history_max_long_task_ms: summarizeNumbers(revealValues('maxLongTaskMs')),
    thread_history_post_fetch_ms: summarizeNumbers(revealValues('postFetchMs')),
    thread_history_remote_page_requests: summarizeNumbers(
      historySamples.map((sample) => sample.historyContract.remotePageRequestCount).filter(Number.isFinite),
    ),
    thread_history_remote_page_bytes: summarizeNumbers(remotePageValues('remotePageBytes')),
    thread_history_remote_page_gzip_bytes: summarizeNumbers(remotePageValues('remotePageGzipBytes')),
    thread_history_remote_page_fetch_ms: summarizeNumbers(remotePageValues('remotePageFetchMs')),
    thread_history_remote_page_items: summarizeNumbers(remotePageValues('remotePageItems')),
    dom_nodes_available: domNodeValues.length === measuredSamples.length,
    dom_nodes: summarizeNumbers(domNodeValues),
    deep_field_feature_nodes: summarizeNumbers(measuredSamples.map((sample) => sample.deepFieldFeatureNodes ?? 0)),
    d3_shadow_nodes: summarizeNumbers(measuredSamples.map((sample) => sample.d3ShadowNodes ?? 0)),
    d3_shadow_bubbles: summarizeNumbers(measuredSamples.map((sample) => sample.d3ShadowBubbles ?? 0)),
    d3_shadow_connections: summarizeNumbers(measuredSamples.map((sample) => sample.d3ShadowConnections ?? 0)),
    primitive_blobs: summarizeNumbers(measuredSamples.map((sample) => sample.primitiveBlobs ?? 0)),
    task_duration_available: taskDurationValues.length === measuredSamples.length,
    task_duration_ms: summarizeNumbers(taskDurationValues),
    long_task_observer_available: measuredSamples.every(
      (sample) => sample.longTaskObserverAvailable === true,
    ),
    long_task_observer_errors: [...new Set(
      measuredSamples.map((sample) => sample.longTaskObserverError).filter(Boolean),
    )],
    long_task_count: summarizeNumbers(longTaskCountValues),
    long_task_total_ms: summarizeNumbers(longTaskTotalValues),
    max_long_task_ms: summarizeNumbers(maxLongTaskValues),
    workspace_idle_telemetry_available: workspaceIdleTelemetryAvailable,
    workspace_idle_no_periodic_wake: workspaceIdleNoPeriodicWake,
    workspace_idle_interaction_contract_passed: workspaceIdleInteractionContractPassed,
    workspace_idle_task_duration_ms: summarizeWorkspaceIdle(['idle', 'taskDurationMs']),
    workspace_idle_script_duration_ms: summarizeWorkspaceIdle(['idle', 'scriptDurationMs']),
    workspace_idle_raf_callbacks: summarizeWorkspaceIdle(['idle', 'rafCallbacks']),
    workspace_idle_signal_style_writes: summarizeWorkspaceIdle(['idle', 'signalStyleWrites']),
    workspace_idle_mutation_resume_ms: mutationWake,
    workspace_idle_drag_resume_ms: dragWake,
    routes,
    post_close_routes: postCloseRoutes,
    all_routes: allRoutes,
    unknown_routes: allRoutes.filter((route) => unknownApiLabels.has(route.label)),
    errors: [...new Set(measuredSamples.flatMap((sample) => sample.errors || []))],
  };
}

function printTextReport(result) {
  console.log(`Frontend benchmark ${result.config.baseUrl}`);
  console.log(`Fixture: ${result.config.ideas} ideas, ${result.config.connections} connections, ${result.config.streamItems} stream items`);
  console.log(`Latency: api=${result.config.apiLatencyMs}ms sidecar=${result.config.sidecarLatencyMs ?? result.config.apiLatencyMs}ms`);
  console.log('');

  const rows = result.scenarios.map((scenario) => ({
    name: scenario.name,
    runs: String(scenario.summary.runs),
    p50: scenario.summary.ready_ms.p50.toFixed(1),
    p95: scenario.summary.ready_ms.p95.toFixed(1),
    fcp: summaryMetricAvailable(scenario.summary, 'fcp_available', 'fcp_ms')
      ? scenario.summary.fcp_ms.p50.toFixed(1)
      : '-',
    api: scenario.summary.api_calls.p50.toFixed(0),
    routes: scenario.summary.unique_api_routes.p50.toFixed(0),
    bootstrap: `${scenario.summary.workspace_bootstrap_before_close.p50.toFixed(0)}/${scenario.summary.workspace_bootstrap_post_close.p50.toFixed(0)}`,
    close: scenario.summary.post_close_ready_ms.p50 ? scenario.summary.post_close_ready_ms.p50.toFixed(1) : '-',
    pane75: scenario.summary.rare_pane_first_open_ms.p75
      ? scenario.summary.rare_pane_first_open_ms.p75.toFixed(1)
      : '-',
    task: summaryMetricAvailable(scenario.summary, 'task_duration_available', 'task_duration_ms')
      ? scenario.summary.task_duration_ms.p50.toFixed(1)
      : '-',
    long: summaryMetricAvailable(scenario.summary, 'long_task_observer_available', 'long_task_count')
      ? scenario.summary.max_long_task_ms.p95.toFixed(1)
      : '-',
    dom: scenario.summary.dom_nodes.p50.toFixed(0),
    d3: scenario.summary.d3_shadow_nodes.p50.toFixed(0),
    links: scenario.summary.d3_shadow_connections.p50.toFixed(0),
    field: scenario.summary.deep_field_feature_nodes.p50.toFixed(0),
  }));
  const headers = ['name', 'runs', 'p50', 'p95', 'fcp', 'api', 'routes', 'bootstrap', 'close', 'pane75', 'task', 'long', 'dom', 'd3', 'links', 'field'];
  const widths = Object.fromEntries(headers.map((header) => [
    header,
    Math.max(header.length, ...rows.map((row) => row[header].length)),
  ]));
  console.log(headers.map((header) => header.padEnd(widths[header])).join('  '));
  console.log(headers.map((header) => '-'.repeat(widths[header])).join('  '));
  for (const row of rows) {
    console.log(headers.map((header) => row[header].padEnd(widths[header])).join('  '));
  }

  const legacy = scenarioByName(result, 'thread');
  const canonical = scenarioByName(result, 'threadCanonical');
  if (legacy && canonical) {
    const parityGap = Math.abs(canonical.summary.ready_ms.p50 - legacy.summary.ready_ms.p50) /
      legacy.summary.ready_ms.p50 * 100;
    console.log('');
    console.log(
      `Canonical/legacy direct-route p50 parity: ` +
      `${parityGap < DIRECT_ROUTE_P50_GAP_BUDGET_PCT ? 'PASS' : 'FAIL'} ` +
      `(${parityGap.toFixed(1)}% gap; budget <${DIRECT_ROUTE_P50_GAP_BUDGET_PCT}%)`,
    );
  }

  for (const scenario of result.scenarios) {
    console.log('');
    console.log(
      `${scenario.name} request contract: ${scenario.summary.request_contract_passed ? 'PASS' : 'FAIL'} ` +
      `(workspace bootstrap before-close/post-close ` +
      `${scenario.summary.workspace_bootstrap_before_close.p50.toFixed(0)}/` +
      `${scenario.summary.workspace_bootstrap_post_close.p50.toFixed(0)})`,
    );
    if (SCENARIOS[scenario.key]?.directThread) {
      const historyCounts = threadHistoryCounts(scenario.summary);
      console.log(
        `${scenario.name} startup: ${scenario.summary.startup_api_calls.p50.toFixed(0)} calls/` +
        `${scenario.summary.startup_api_kb.p50.toFixed(2)}KB ` +
        `(call budget <=${DIRECT_THREAD_STARTUP_CALL_BUDGET}` +
        `${result.config.streamItems <= 80 ? `; normal payload budget <=${DIRECT_THREAD_STARTUP_PAYLOAD_BUDGET_KB}KB` : ''}); ` +
        `bootstrap before /api/me ${scenario.summary.direct_bootstrap_before_me_fulfilled ? 'PASS' : 'FAIL'}`,
      );
      console.log(
        `${scenario.name} initial thread payload: ` +
        `${(scenario.summary.initial_thread_payload_bytes.p50 / 1024).toFixed(2)}KB raw/` +
        `${(scenario.summary.initial_thread_payload_gzip_bytes.p50 / 1024).toFixed(2)}KB gzip; ` +
        `${scenario.summary.initial_thread_items.p50.toFixed(0)} items; ` +
        `contract ${result.config.streamContract}`,
      );
      console.log(
        `${scenario.name} history markers: ${scenario.summary.thread_history_contract_passed ? 'PASS' : 'FAIL'} ` +
        `(initial identities/rendered ${Number.isFinite(historyCounts.initialIdentities?.p50) ? historyCounts.initialIdentities.p50.toFixed(0) : '-'}/` +
        `${Number.isFinite(historyCounts.initialItems?.p50) ? historyCounts.initialItems.p50.toFixed(0) : '-'}; ` +
        `reveal p95 ${scenario.summary.thread_history_reveal_ms.p95.toFixed(1)}ms; ` +
        `drift max ${scenario.summary.thread_history_viewport_drift_px.max.toFixed(1)}px; ` +
        `final identities ${Number.isFinite(historyCounts.finalIdentities?.p50) ? historyCounts.finalIdentities.p50.toFixed(0) : '-'})`,
      );
      console.log(
        `${scenario.name} remote history: ` +
        `${scenario.summary.thread_history_remote_page_requests.p50.toFixed(0)} pages; ` +
        `page max ${(scenario.summary.thread_history_remote_page_bytes.max / 1024).toFixed(2)}KB raw/` +
        `${(scenario.summary.thread_history_remote_page_gzip_bytes.max / 1024).toFixed(2)}KB gzip; ` +
        `fetch p95 ${scenario.summary.thread_history_remote_page_fetch_ms.p95.toFixed(1)}ms`,
      );
      console.log(
        `${scenario.name} history work: ` +
        `CDP task p50/p95 ${scenario.summary.thread_history_task_duration_ms.p50.toFixed(1)}/` +
        `${scenario.summary.thread_history_task_duration_ms.p95.toFixed(1)}ms; ` +
        `post-fetch p95 ${scenario.summary.thread_history_post_fetch_ms.p95.toFixed(1)}ms; ` +
        `long task max ${scenario.summary.thread_history_max_long_task_ms.max.toFixed(1)}ms`,
      );
    }
    if (scenario.summary.lazy_asset_observation_ms > 0) {
      console.log(
        `${scenario.name} lazy asset contract: ${scenario.summary.lazy_asset_contract_passed ? 'PASS' : 'FAIL'} ` +
        `(observed ${scenario.summary.lazy_asset_observation_ms.toFixed(0)}ms; ` +
        `manifest closure ${scenario.summary.manifest_expected_asset_count.p50.toFixed(0)}; ` +
        `entry assets before ${scenario.summary.manifest_entry_assets_before_selection.length}; ` +
        `shared before ${scenario.summary.manifest_shared_asset_count_before_selection.p50.toFixed(0)}; ` +
        `deferred and fetched ${scenario.summary.selection_triggered_asset_count.p50.toFixed(0)})`,
      );
      for (const asset of scenario.summary.selection_triggered_assets) {
        console.log(`  on selection: ${asset}`);
      }
    }
    if (scenario.summary.rare_pane_first_open_ms.p75 > 0) {
      console.log(
        `${scenario.name} Vault first-open p75: ${scenario.summary.rare_pane_first_open_ms.p75.toFixed(1)}ms ` +
        `(budget <=${scenario.summary.rare_pane_first_open_budget_ms}ms: ` +
        `${scenario.summary.rare_pane_first_open_budget_passed ? 'PASS' : 'FAIL'}; ` +
        `rare assets before selection ${scenario.summary.rare_pane_assets_before_selection.max.toFixed(0)}; ` +
        `full data p75 ${scenario.summary.rare_pane_data_ready_ms.p75.toFixed(1)}ms)`,
      );
      console.log(
        `${scenario.name} default pane contract: ${scenario.summary.default_pane_contract_passed ? 'PASS' : 'FAIL'} ` +
        `(Discussion p75 ${scenario.summary.default_pane_ready_ms.discussion.p75.toFixed(1)}ms; ` +
        `Handoff p75 ${scenario.summary.default_pane_ready_ms['handoff-summary'].p75.toFixed(1)}ms; ` +
        `Activity p75 ${scenario.summary.default_pane_ready_ms.activity.p75.toFixed(1)}ms)`,
      );
      console.log(
        `${scenario.name} composer contract: ${scenario.summary.composer_contract_passed ? 'PASS' : 'FAIL'} ` +
        `(type, submit, POST, refresh p75 ${scenario.summary.composer_send_ready_ms.p75.toFixed(1)}ms)`,
      );
    }
    console.log(`${scenario.name} API routes:`);
    for (const route of scenario.summary.routes.slice(0, 20)) {
      console.log(`  ${route.avg_calls_per_run.toFixed(1)}x/run  ${route.label}`);
    }
    if (scenario.summary.post_close_routes.length) {
      console.log('  Post-close API routes:');
      for (const route of scenario.summary.post_close_routes.slice(0, 20)) {
        console.log(`    ${route.avg_calls_per_run.toFixed(1)}x/run  ${route.label}`);
      }
    }
    if (scenario.summary.unknown_routes.length) {
      console.log('  Unknown mocked routes:');
      for (const route of scenario.summary.unknown_routes) {
        console.log(`    ${route.label}`);
      }
    }
    if (scenario.summary.errors.length) {
      console.log('  Browser errors:');
      for (const error of scenario.summary.errors) {
        console.log(`    ${error}`);
      }
    }
  }
}

function scenarioByName(result, name) {
  return result.scenarios.find((scenario) => scenario.name === name || scenario.key === name);
}

function summaryMetricAvailable(summary, availabilityKey, metricKey) {
  if (Object.hasOwn(summary, availabilityKey)) return summary[availabilityKey] === true;
  const metric = summary[metricKey];
  return Boolean(
    metric && Number.isFinite(metric.p50) && Number.isFinite(metric.p95) && metric.max > 0,
  );
}

function summaryMetricValue(summary, availabilityKey, metricKey, percentileName) {
  if (!summaryMetricAvailable(summary, availabilityKey, metricKey)) return null;
  const value = summary[metricKey]?.[percentileName];
  return Number.isFinite(value) ? value : null;
}

function threadHistoryCounts(summary) {
  const value = (currentKey, legacyKey) => Object.hasOwn(summary, currentKey)
    ? summary[currentKey]
    : summary[legacyKey] ?? null;
  return {
    initialIdentities: value(
      'thread_history_initial_rendered_identity_count',
      'thread_history_initial_raw_items',
    ),
    initialItems: value(
      'thread_history_initial_rendered_item_count',
      'thread_history_initial_rendered_items',
    ),
    finalIdentities: value(
      'thread_history_final_rendered_identity_count',
      'thread_history_final_raw_items',
    ),
  };
}

function directThreadHistoryFailures(key, summary) {
  const failures = [];
  if (summary.thread_history_contract_passed !== true) {
    failures.push(`${key}: thread history window behavior contract failed`);
  }

  const counts = threadHistoryCounts(summary);
  for (const [label, metric] of [
    ['initial rendered identity count', counts.initialIdentities],
    ['initial rendered item count', counts.initialItems],
    ['final rendered identity count', counts.finalIdentities],
  ]) {
    if (![metric?.p50, metric?.p95, metric?.max].every(Number.isFinite)) {
      failures.push(`${key}: ${label} unavailable`);
    }
  }

  for (const [label, availabilityKey, metricKey] of [
    ['history reveal', 'thread_history_reveal_available', 'thread_history_reveal_ms'],
    ['history viewport drift', 'thread_history_viewport_drift_available', 'thread_history_viewport_drift_px'],
    ['history CDP task duration', 'thread_history_task_duration_available', 'thread_history_task_duration_ms'],
    ['history long task', 'thread_history_long_task_observer_available', 'thread_history_max_long_task_ms'],
  ]) {
    const metric = summary[metricKey];
    const metricAvailable = metric && [metric.p50, metric.p95, metric.max].every(Number.isFinite);
    const observerAvailable = !Object.hasOwn(summary, availabilityKey) ||
      summary[availabilityKey] === true;
    if (!observerAvailable || !metricAvailable) failures.push(`${key}: ${label} unavailable`);
  }
  return failures;
}

function pctDelta(before, after) {
  if (!before) return 0;
  return ((after - before) / before) * 100;
}

const COMPARISON_CONFIG_KEYS = [
  'runs', 'warmups', 'ideas', 'connections', 'streamItems', 'apiLatencyMs', 'sidecarLatencyMs',
  'timeoutMs', 'allowUnknownApi', 'workspaceIdle', 'idleSettleMs', 'idleWindowMs',
];

function comparisonSignature(result, scenarioKeys) {
  return JSON.stringify({
    ...Object.fromEntries(COMPARISON_CONFIG_KEYS.map((key) => [
      key,
      key === 'allowUnknownApi' ? result.config[key] === true : result.config[key],
    ])),
    scenarios: result.config.scenarios.filter((key) => scenarioKeys.includes(key)),
  });
}

function withinRegressionBudget(before, after) {
  return before > 0 && pctDelta(before, after) <= MAX_REGRESSION_PCT;
}

function isThreadHistoryStressConfig(config) {
  return config.ideas === 1500 &&
    config.connections === 3000 &&
    config.streamItems === 1000 &&
    config.apiLatencyMs === 100;
}

function stressAbsoluteFailures(key, summary, revealBudgetMs = THREAD_HISTORY_REVEAL_BUDGET_MS) {
  const failures = [];
  const metrics = [
    ['CDP TaskDuration', summaryMetricAvailable(summary, 'task_duration_available', 'task_duration_ms'), summary.task_duration_ms?.p50, STRESS_TASK_P50_BUDGET_MS, 'ms'],
    ['LongTask p95', summaryMetricAvailable(summary, 'long_task_observer_available', 'long_task_count'), summary.max_long_task_ms?.p95, STRESS_LONG_TASK_P95_BUDGET_MS, 'ms'],
    ['DOM nodes max', summaryMetricAvailable(summary, 'dom_nodes_available', 'dom_nodes'), summary.dom_nodes?.max, STRESS_DOM_NODE_BUDGET, ''],
    ['history reveal p95', summaryMetricAvailable(summary, 'thread_history_reveal_available', 'thread_history_reveal_ms'), summary.thread_history_reveal_ms?.p95, revealBudgetMs, 'ms'],
    ['history viewport drift max', summaryMetricAvailable(summary, 'thread_history_viewport_drift_available', 'thread_history_viewport_drift_px'), summary.thread_history_viewport_drift_px?.max, THREAD_HISTORY_VIEWPORT_DRIFT_BUDGET_PX, 'px'],
  ];
  for (const [metric, available, value, budget, unit] of metrics) {
    if (!available || !Number.isFinite(value)) failures.push(`${key}: ${metric} unavailable`);
    else if (value > budget) failures.push(`${key}: ${metric} ${value.toFixed(1)}${unit} > ${budget}${unit}`);
  }

  const historyCounts = threadHistoryCounts(summary);
  for (const [label, count] of [
    ['initial rendered identity count', historyCounts.initialIdentities],
    ['initial rendered item count', historyCounts.initialItems],
  ]) {
    if (Number.isFinite(count?.max) && count.max > THREAD_HISTORY_WINDOW_SIZE) {
      failures.push(`${key}: ${label} exceeds ${THREAD_HISTORY_WINDOW_SIZE}`);
    }
  }
  return failures;
}

function isPaginationComparison(before, after) {
  return before.config.streamContract === 'legacy' && after.config.streamContract === 'paged';
}

function paginationAbsoluteFailures(key, summary, config) {
  const failures = [];
  const bootstrapBytes = summary.initial_direct_bootstrap_bytes;
  const threadPayloadBytes = summary.initial_thread_payload_bytes;
  const initialItems = summary.initial_thread_items;
  for (const [label, metric] of [
    ['initial direct bootstrap bytes', bootstrapBytes],
    ['initial thread payload bytes', threadPayloadBytes],
    ['initial thread items', initialItems],
  ]) {
    if (!metric || ![metric.p50, metric.p95, metric.max].every(Number.isFinite) || metric.max <= 0) {
      failures.push(`${key}: ${label} unavailable`);
    }
  }
  if (Number.isFinite(bootstrapBytes?.max) && bootstrapBytes.max > THREAD_PAGE_RAW_BUDGET_BYTES) {
    failures.push(`${key}: initial direct bootstrap exceeds ${THREAD_PAGE_RAW_BUDGET_BYTES} bytes`);
  }
  const expectedInitialItems = Math.min(config.streamItems, THREAD_HISTORY_WINDOW_SIZE);
  if (Number.isFinite(initialItems?.min) && (
    initialItems.min !== expectedInitialItems || initialItems.max !== expectedInitialItems
  )) {
    failures.push(`${key}: initial page must contain exactly ${expectedInitialItems} items`);
  }

  const expectedOlderPages = Math.max(
    0,
    Math.ceil(config.streamItems / THREAD_HISTORY_WINDOW_SIZE) - 1,
  );
  const pageRequests = summary.thread_history_remote_page_requests;
  if (!pageRequests || ![pageRequests.min, pageRequests.max].every(Number.isFinite)) {
    failures.push(`${key}: remote page request count unavailable`);
  } else if (pageRequests.min !== expectedOlderPages || pageRequests.max !== expectedOlderPages) {
    failures.push(`${key}: expected ${expectedOlderPages} remote pages`);
  }
  if (expectedOlderPages > 0) {
    const pageBytes = summary.thread_history_remote_page_bytes;
    const fetchMs = summary.thread_history_remote_page_fetch_ms;
    const postFetchMs = summary.thread_history_post_fetch_ms;
    const pageItems = summary.thread_history_remote_page_items;
    for (const [label, metric] of [
      ['remote page bytes', pageBytes],
      ['remote page fetch', fetchMs],
      ['remote page post-fetch work', postFetchMs],
      ['remote page items', pageItems],
    ]) {
      if (!metric || ![metric.p50, metric.p95, metric.max].every(Number.isFinite) || metric.max <= 0) {
        failures.push(`${key}: ${label} unavailable`);
      }
    }
    if (Number.isFinite(pageBytes?.max) && pageBytes.max > THREAD_PAGE_RAW_BUDGET_BYTES) {
      failures.push(`${key}: remote page exceeds ${THREAD_PAGE_RAW_BUDGET_BYTES} bytes`);
    }
    if (Number.isFinite(fetchMs?.p95) && fetchMs.p95 > THREAD_PAGE_FETCH_BUDGET_MS) {
      failures.push(`${key}: remote page fetch p95 exceeds ${THREAD_PAGE_FETCH_BUDGET_MS}ms`);
    }
    if (Number.isFinite(pageItems?.max) && pageItems.max > THREAD_HISTORY_WINDOW_SIZE) {
      failures.push(`${key}: remote page exceeds ${THREAD_HISTORY_WINDOW_SIZE} items`);
    }
  }
  return failures;
}

function comparisonFailures(before, after, scenarioKeys) {
  const failures = [];
  const paginationComparison = isPaginationComparison(before, after);
  const workspaceIdleComparison = before.config.workspaceIdle === true && after.config.workspaceIdle === true;
  const hasStreamContract = Boolean(before.config.streamContract || after.config.streamContract);
  if (comparisonSignature(before, scenarioKeys) !== comparisonSignature(after, scenarioKeys)) {
    failures.push('before/after benchmark config signatures differ');
  }
  if (hasStreamContract && !paginationComparison && !workspaceIdleComparison) {
    failures.push('pagination comparison requires legacy before and paged after contracts');
  }

  for (const key of scenarioKeys) {
    const beforeScenario = scenarioByName(before, key);
    const afterScenario = scenarioByName(after, key);
    if (!beforeScenario || !afterScenario) {
      failures.push(`${key}: missing before or after scenario`);
      continue;
    }

    const { summary } = afterScenario;
    if (!summary.request_contract_passed) failures.push(`${key}: request contract failed`);
    if (!summary.lazy_asset_contract_passed) failures.push(`${key}: lazy asset contract failed`);
    if (!summary.rare_pane_first_open_budget_passed) failures.push(`${key}: rare pane budget failed`);
    if (!summary.default_pane_contract_passed) failures.push(`${key}: default pane contract failed`);
    if (!summary.composer_contract_passed) failures.push(`${key}: composer contract failed`);
    if (summary.errors.length) failures.push(`${key}: browser errors observed`);

    const directThread = Boolean(SCENARIOS[key]?.directThread);
    const stress = directThread && isThreadHistoryStressConfig(after.config);
    if (directThread) failures.push(...directThreadHistoryFailures(key, summary));
    if (directThread && after.config.streamContract === 'paged') {
      failures.push(...paginationAbsoluteFailures(key, summary, after.config));
    }

    const beforeReady = beforeScenario.summary.ready_ms;
    const afterReady = summary.ready_ms;
    const readyMetrics = [['p50', beforeReady.p50, afterReady.p50], ['p95', beforeReady.p95, afterReady.p95]];
    const stableReady = readyMetrics.map(([, beforeValue, afterValue]) => withinRegressionBudget(beforeValue, afterValue));
    if ((!stress || paginationComparison) && !stableReady.every(Boolean)) {
      failures.push(`${key}: ready regression >${MAX_REGRESSION_PCT}%`);
    }
    const beforeFcp = summaryMetricValue(beforeScenario.summary, 'fcp_available', 'fcp_ms', 'p50');
    const afterFcp = summaryMetricValue(summary, 'fcp_available', 'fcp_ms', 'p50');
    if ((!stress || paginationComparison) && (beforeFcp === null || afterFcp === null)) {
      failures.push(`${key}: comparable FCP unavailable`);
    } else if ((!stress || paginationComparison) && !withinRegressionBudget(beforeFcp, afterFcp)) {
      failures.push(`${key}: FCP p50 regression >${MAX_REGRESSION_PCT}%`);
    }

    if (workspaceIdleComparison) {
      if (!beforeScenario.summary.workspace_idle_telemetry_available || !summary.workspace_idle_telemetry_available) {
        failures.push(`${key}: workspace idle telemetry unavailable`);
      }
      if (!directThread) {
        const reductions = [
          ['rAF callbacks', 'workspace_idle_raf_callbacks', 95],
          ['signal style writes', 'workspace_idle_signal_style_writes', 95],
          ['ScriptDuration', 'workspace_idle_script_duration_ms', 80],
          ['TaskDuration', 'workspace_idle_task_duration_ms', 40],
        ];
        for (const [label, metric, target] of reductions) {
          const beforeValue = beforeScenario.summary[metric]?.p50;
          const afterValue = summary[metric]?.p50;
          if (!Number.isFinite(beforeValue) || !Number.isFinite(afterValue) || beforeValue <= 0) {
            failures.push(`${key}: comparable idle ${label} unavailable`);
          } else if (-pctDelta(beforeValue, afterValue) < target) {
            failures.push(`${key}: idle ${label} reduction <${target}%`);
          }
        }
        if (!summary.workspace_idle_no_periodic_wake) failures.push(`${key}: settled workspace JS woke periodically`);
        if (!summary.workspace_idle_interaction_contract_passed) failures.push(`${key}: interaction wake p95 >${WORKSPACE_IDLE_WAKE_BUDGET_MS}ms`);
      }
    }

    if (stress && !paginationComparison) {
      for (const [percentileName, beforeValue, afterValue] of readyMetrics) {
        const improvementPct = -pctDelta(beforeValue, afterValue);
        if (improvementPct < STRESS_READY_IMPROVEMENT_TARGET_PCT) {
          failures.push(
            `${key}: ready ${percentileName} improved ${improvementPct.toFixed(1)}% < ` +
            `${STRESS_READY_IMPROVEMENT_TARGET_PCT}%`,
          );
        }
      }
    }
    if (stress) failures.push(...stressAbsoluteFailures(
      key,
      summary,
      paginationComparison ? THREAD_PAGE_FETCH_BUDGET_MS : THREAD_HISTORY_REVEAL_BUDGET_MS,
    ));

    if (directThread && paginationComparison) {
      if (before.config.streamItems > THREAD_HISTORY_WINDOW_SIZE) {
        const beforeBootstrap = beforeScenario.summary.initial_direct_bootstrap_bytes?.p50;
        const afterBootstrap = summary.initial_direct_bootstrap_bytes?.p50;
        if (!Number.isFinite(beforeBootstrap) || !Number.isFinite(afterBootstrap) || beforeBootstrap <= 0) {
          failures.push(`${key}: comparable initial direct bootstrap bytes unavailable`);
        } else if (-pctDelta(beforeBootstrap, afterBootstrap) < THREAD_STARTUP_PAYLOAD_REDUCTION_TARGET_PCT) {
          failures.push(`${key}: initial direct bootstrap reduction <${THREAD_STARTUP_PAYLOAD_REDUCTION_TARGET_PCT}%`);
        }
      }

      const regressionMetrics = [
        ['DOM p50', beforeScenario.summary.dom_nodes?.p50, summary.dom_nodes?.p50],
        ['CDP task p50', summaryMetricValue(beforeScenario.summary, 'task_duration_available', 'task_duration_ms', 'p50'), summaryMetricValue(summary, 'task_duration_available', 'task_duration_ms', 'p50')],
        ['Long task p95', summaryMetricValue(beforeScenario.summary, 'long_task_observer_available', 'max_long_task_ms', 'p95'), summaryMetricValue(summary, 'long_task_observer_available', 'max_long_task_ms', 'p95')],
      ];
      for (const [metric, beforeValue, afterValue] of regressionMetrics) {
        if (!Number.isFinite(beforeValue) || !Number.isFinite(afterValue)) {
          failures.push(`${key}: comparable ${metric} unavailable`);
        } else if (!withinRegressionBudget(beforeValue, afterValue)) {
          failures.push(`${key}: ${metric} regression >${MAX_REGRESSION_PCT}%`);
        }
      }
    }
  }
  return failures;
}

function printComparison(before, after, scenarioKeys) {
  console.log(`Frontend benchmark wins: ${before.config.phase || 'before'} -> ${after.config.phase || 'after'}`);
  console.log('');
  const rows = [];
  for (const afterScenario of after.scenarios) {
    if (!scenarioKeys.includes(afterScenario.key)) continue;
    const beforeScenario = scenarioByName(before, afterScenario.name);
    if (!beforeScenario) continue;
    const beforeSummary = beforeScenario.summary;
    const afterSummary = afterScenario.summary;
    const metrics = [
      ['ready p50', beforeSummary.ready_ms.p50, afterSummary.ready_ms.p50, 'ms'],
      ['ready p95', beforeSummary.ready_ms.p95, afterSummary.ready_ms.p95, 'ms'],
      ['FCP p50', summaryMetricValue(beforeSummary, 'fcp_available', 'fcp_ms', 'p50'), summaryMetricValue(afterSummary, 'fcp_available', 'fcp_ms', 'p50'), 'ms'],
      ['API calls p50', beforeSummary.api_calls.p50, afterSummary.api_calls.p50, ''],
      ['API KB p50', beforeSummary.api_kb.p50, afterSummary.api_kb.p50, 'KB'],
      ['initial bootstrap KB p50', beforeSummary.initial_direct_bootstrap_bytes?.p50 / 1024, afterSummary.initial_direct_bootstrap_bytes?.p50 / 1024, 'KB'],
      ['initial thread payload KB p50', beforeSummary.initial_thread_payload_bytes?.p50 / 1024, afterSummary.initial_thread_payload_bytes?.p50 / 1024, 'KB'],
      ['rare pane first-open p75', beforeSummary.rare_pane_first_open_ms?.p75 ?? 0, afterSummary.rare_pane_first_open_ms?.p75 ?? 0, 'ms'],
      ['DOM nodes p50', beforeSummary.dom_nodes.p50, afterSummary.dom_nodes.p50, ''],
      ['D3 shadow nodes p50', beforeSummary.d3_shadow_nodes?.p50 ?? 0, afterSummary.d3_shadow_nodes?.p50 ?? 0, ''],
      ['D3 shadow bubbles p50', beforeSummary.d3_shadow_bubbles?.p50 ?? 0, afterSummary.d3_shadow_bubbles?.p50 ?? 0, ''],
      ['D3 shadow links p50', beforeSummary.d3_shadow_connections?.p50 ?? 0, afterSummary.d3_shadow_connections?.p50 ?? 0, ''],
      ['field feature nodes p50', beforeSummary.deep_field_feature_nodes.p50, afterSummary.deep_field_feature_nodes.p50, ''],
      ['CDP task p50', summaryMetricValue(beforeSummary, 'task_duration_available', 'task_duration_ms', 'p50'), summaryMetricValue(afterSummary, 'task_duration_available', 'task_duration_ms', 'p50'), 'ms'],
      ['long task p95', summaryMetricValue(beforeSummary, 'long_task_observer_available', 'max_long_task_ms', 'p95'), summaryMetricValue(afterSummary, 'long_task_observer_available', 'max_long_task_ms', 'p95'), 'ms'],
      ['history initial rendered identities p50', threadHistoryCounts(beforeSummary).initialIdentities?.p50 ?? null, threadHistoryCounts(afterSummary).initialIdentities?.p50 ?? null, ''],
      ['history reveal p95', beforeSummary.thread_history_reveal_ms?.p95 ?? 0, afterSummary.thread_history_reveal_ms?.p95 ?? 0, 'ms'],
      ['history drift max', beforeSummary.thread_history_viewport_drift_px?.max ?? 0, afterSummary.thread_history_viewport_drift_px?.max ?? 0, 'px'],
      ['history CDP task p95', beforeSummary.thread_history_task_duration_ms?.p95, afterSummary.thread_history_task_duration_ms?.p95, 'ms'],
      ['history long task max', beforeSummary.thread_history_max_long_task_ms?.max, afterSummary.thread_history_max_long_task_ms?.max, 'ms'],
      ['history post-fetch p95', beforeSummary.thread_history_post_fetch_ms?.p95, afterSummary.thread_history_post_fetch_ms?.p95, 'ms'],
      ['remote pages p50', beforeSummary.thread_history_remote_page_requests?.p50, afterSummary.thread_history_remote_page_requests?.p50, ''],
      ['remote page KB max', beforeSummary.thread_history_remote_page_bytes?.max / 1024, afterSummary.thread_history_remote_page_bytes?.max / 1024, 'KB'],
      ['remote fetch p95', beforeSummary.thread_history_remote_page_fetch_ms?.p95, afterSummary.thread_history_remote_page_fetch_ms?.p95, 'ms'],
    ];
    for (const [metric, beforeValue, afterValue, unit] of metrics) {
      rows.push({
        scenario: afterScenario.name,
        metric,
        before: beforeValue,
        after: afterValue,
        delta: Number.isFinite(beforeValue) && Number.isFinite(afterValue)
          ? pctDelta(beforeValue, afterValue)
          : null,
        unit,
      });
    }
  }

  for (const row of rows) {
    if (row.delta === null) {
      const beforeValue = Number.isFinite(row.before) ? `${row.before.toFixed(1)}${row.unit}` : 'unavailable';
      const afterValue = Number.isFinite(row.after) ? `${row.after.toFixed(1)}${row.unit}` : 'unavailable';
      console.log(`${row.scenario} ${row.metric}: ${beforeValue} -> ${afterValue}`);
      continue;
    }
    const arrow = row.delta <= 0 ? 'win' : 'reg';
    console.log(`${row.scenario} ${row.metric}: ${row.before.toFixed(1)}${row.unit} -> ${row.after.toFixed(1)}${row.unit} (${arrow} ${Math.abs(row.delta).toFixed(1)}%)`);
  }

  const failures = comparisonFailures(before, after, scenarioKeys);
  console.log('');
  console.log(`Acceptance contract: ${failures.length ? 'FAIL' : 'PASS'}`);
  for (const failure of failures) console.log(`  FAIL ${failure}`);
  return failures;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.compare) {
    const [beforePath, afterPath] = options.compare.split(',').map((item) => item.trim());
    if (!beforePath || !afterPath) throw new Error('--compare expects BEFORE,AFTER');
    const before = JSON.parse(await readFile(beforePath, 'utf8'));
    const after = JSON.parse(await readFile(afterPath, 'utf8'));
    const failures = printComparison(before, after, options.scenarios);
    if (failures.length) {
      throw new Error('Frontend benchmark acceptance contract failed');
    }
    return;
  }

  const fixture = buildFixture(options);
  const lazyAssetClosures = await loadExpectedLazyAssetClosures(options.manifest ?? VITE_CLIENT_MANIFEST_URL);
  const chrome = await launchChrome(options);
  const client = new CdpClient(chrome.pageWsUrl);
  await client.connect();

  try {
    const samplesByScenario = new Map();
    for (const scenarioKey of options.scenarios) {
      const samples = [];
      const totalRuns = options.warmups + options.runs;
      for (let index = 0; index < totalRuns; index += 1) {
        const measured = index >= options.warmups;
        const sample = await runScenario(
          client,
          scenarioKey,
          fixture,
          options,
          measured,
          lazyAssetClosures,
        );
        samples.push(sample);
      }
      samplesByScenario.set(scenarioKey, samples);
    }

    const result = {
      config: {
        phase: options.phase,
        baseUrl: options.baseUrl,
        runs: options.runs,
        warmups: options.warmups,
        scenarios: options.scenarios,
        ideas: options.ideas,
        connections: options.connections,
        streamItems: options.streamItems,
        streamContract: options.streamContract,
        apiLatencyMs: options.apiLatencyMs,
        sidecarLatencyMs: options.sidecarLatencyMs,
        timeoutMs: options.timeoutMs,
        allowUnknownApi: options.allowUnknownApi,
        workspaceIdle: options.workspaceIdle, idleSettleMs: options.idleSettleMs, idleWindowMs: options.idleWindowMs,
        lazyAssetManifest: {
          threadStageModuleId: lazyAssetClosures.threadStage.moduleId,
          threadStageAssetCount: lazyAssetClosures.threadStage.assets.length,
          threadStageEntryAssetCount: lazyAssetClosures.threadStage.entryAssets.length,
          vaultModuleId: lazyAssetClosures.vault.moduleId,
          vaultAssetCount: lazyAssetClosures.vault.assets.length,
          vaultEntryAssetCount: lazyAssetClosures.vault.entryAssets.length,
        },
      },
      scenarios: options.scenarios.map((scenarioKey) => {
        const samples = samplesByScenario.get(scenarioKey) ?? [];
        return {
          key: scenarioKey,
          name: SCENARIOS[scenarioKey].name,
          summary: summarizeScenario(samples),
          samples,
        };
      }),
    };
    const unknownRoutes = result.scenarios.flatMap((scenario) => scenario.summary.unknown_routes);
    if (unknownRoutes.length && !options.allowUnknownApi) {
      throw new Error(`Unknown mocked API routes detected:\n${unknownRoutes.map((route) => `- ${route.label}`).join('\n')}`);
    }
    const failedPerformanceContracts = result.scenarios.filter((scenario) => (
      scenario.summary.rare_pane_first_open_ms.p75 > 0 &&
      scenario.summary.rare_pane_first_open_budget_passed === false
    ));
    const idleTelemetryFailures = options.workspaceIdle
      ? result.scenarios
          .filter((scenario) => scenario.summary.workspace_idle_telemetry_available !== true)
          .map((scenario) => `${scenario.key}: telemetry unavailable`)
      : [];
    const threadFailures = result.scenarios.flatMap((scenario) => {
      if (!SCENARIOS[scenario.key]?.directThread) return [];
      const failures = directThreadHistoryFailures(scenario.key, scenario.summary);
      if (result.config.streamContract === 'paged') {
        failures.push(...paginationAbsoluteFailures(scenario.key, scenario.summary, result.config));
      }
      if (isThreadHistoryStressConfig(result.config)) {
        failures.push(...stressAbsoluteFailures(
          scenario.key,
          scenario.summary,
          result.config.streamContract === 'paged'
            ? THREAD_PAGE_FETCH_BUDGET_MS
            : THREAD_HISTORY_REVEAL_BUDGET_MS,
        ));
      }
      return failures;
    });

    if (options.out) {
      await mkdir(path.dirname(options.out), { recursive: true });
      await writeFile(options.out, `${JSON.stringify(result, null, 2)}\n`);
    }

    if (options.json) {
      console.log(JSON.stringify(result, null, 2));
    } else {
      printTextReport(result);
    }

    if (failedPerformanceContracts.length) {
      throw new Error(
        `Rare pane first-open budget failed:\n${failedPerformanceContracts.map((scenario) => (
          `- ${scenario.name}: p75 ${scenario.summary.rare_pane_first_open_ms.p75.toFixed(1)}ms > ` +
          `${scenario.summary.rare_pane_first_open_budget_ms}ms`
        )).join('\n')}`,
      );
    }
    if (threadFailures.length) {
      throw new Error(`Thread benchmark contract failed:\n${threadFailures.map((failure) => `- ${failure}`).join('\n')}`);
    }
    if (idleTelemetryFailures.length) {
      throw new Error(`Workspace idle telemetry failed:\n${idleTelemetryFailures.map((failure) => `- ${failure}`).join('\n')}`);
    }
  } finally {
    client.close();
    await chrome.close();
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
