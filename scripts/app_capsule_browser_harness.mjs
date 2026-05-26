#!/usr/bin/env node
/**
 * Headless Chrome runtime harness for App Capsule Lab.
 *
 * The outer page acts like the Illo host. It mounts the app in a sandboxed
 * iframe, injects the same public bridge shape, serves deterministic binding
 * data, and receives metrics from an eval-only probe running inside the app.
 */

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { performance } from 'node:perf_hooks';

const DEFAULT_CHROME_PATHS = [
  process.env.CHROME_PATH,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter((candidate) => candidate && existsSync(candidate));

function parseArgs(argv) {
  const options = { input: null };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--input') {
      index += 1;
      options.input = argv[index];
    } else if (arg === '--help' || arg === '-h') {
      console.log('Usage: node scripts/app_capsule_browser_harness.mjs --input payload.json');
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!options.input) throw new Error('--input is required');
  return options;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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
    const message = JSON.parse(await normalizeWsData(data));
    if (message.id) {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(`${message.error.message}: ${JSON.stringify(message.error.data ?? '')}`));
      else pending.resolve(message.result ?? {});
      return;
    }
    if (!message.method) return;
    const callbacks = this.handlers.get(message.method);
    if (!callbacks) return;
    for (const callback of callbacks) callback(message.params ?? {});
  }

  send(method, params = {}) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error(`CDP is not connected for ${method}`));
    }
    const id = this.nextId;
    this.nextId += 1;
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
    this.ws.send(JSON.stringify({ id, method, params }));
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

async function launchChrome(config) {
  const chromePath = config.chrome_path || DEFAULT_CHROME_PATHS.find(Boolean);
  if (!chromePath) throw new Error('Could not find Chrome. Pass --chrome-path or set CHROME_PATH.');
  const userDataDir = await mkdtemp(path.join(os.tmpdir(), 'illo-app-capsule-eval-'));
  const width = Number(config.viewport?.width || 1440);
  const height = Number(config.viewport?.height || 900);
  const proc = spawn(
    chromePath,
    [
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
      `--window-size=${width},${height}`,
      'about:blank',
    ],
    { stdio: ['ignore', 'ignore', 'pipe'] },
  );

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
  const targets = await fetchJson(`http://${endpoint.host}/json/list`);
  const pageTarget = targets.find((target) => target.type === 'page');
  return {
    proc,
    userDataDir,
    pageWsUrl: pageTarget.webSocketDebuggerUrl,
    async close() {
      proc.kill('SIGTERM');
      await Promise.race([new Promise((resolve) => proc.once('exit', resolve)), sleep(1500)]).catch(() => {});
      await rm(userDataDir, { recursive: true, force: true });
    },
  };
}

async function fetchJson(url, init) {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(`${response.status} ${response.statusText} for ${url}`);
  return response.json();
}

async function evaluate(client, expression) {
  const result = await client.send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || 'Runtime.evaluate failed');
  return result.result?.value;
}

async function waitForResult(client, timeoutMs) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    const result = await evaluate(client, 'window.__APP_CAPSULE_EVAL_RESULT__ || null');
    if (result) return result;
    await sleep(25);
  }
  const diagnostics = await evaluate(client, `(() => ({
    readyState: document.readyState,
    title: document.title,
    text: document.body?.textContent?.slice(0, 1000) || ''
  }))()`);
  throw new Error(`Timed out waiting for app capsule eval result: ${JSON.stringify(diagnostics)}`);
}

function buildOuterHtml(config) {
  const srcdoc = buildSrcdoc(config);
  return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>App Capsule Eval Harness</title>
    <style>
      html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; background: #10141f; }
      iframe { display: block; width: 100vw; height: 100vh; border: 0; background: transparent; }
    </style>
  </head>
  <body>
    <iframe id="app-frame" sandbox="allow-scripts allow-forms allow-popups" srcdoc="${escapeAttribute(srcdoc)}"></iframe>
    <script>window.__APP_CAPSULE_CONFIG__ = ${safeScriptJson(config)};<\/script>
    <script>${outerHostScript()}<\/script>
  </body>
</html>`;
}

function buildSrcdoc(config) {
  const injections = `
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${escapeHtml(config.app.name)}</title>
    <script>window.__ILLO_APP_MANIFEST__ = ${safeScriptJson(config.manifest || {})};<\/script>
    ${runtimeStyle()}
    ${bridgeScript(config.app)}
    ${evalProbeScript()}
  `;
  return `<!doctype html>
<html>
  <head>${injections}</head>
  <body><main class="illo-generated-app-root">${config.source_code || ''}</main></body>
</html>`;
}

function outerHostScript() {
  return `
const config = window.__APP_CAPSULE_CONFIG__;
const frame = document.getElementById('app-frame');
const startedAt = performance.now();
const records = (config.records || []).map((record) => structuredClone(record));
const dataCalls = [];
let bridgeReadyAt = null;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function binding(alias) {
  return config.manifest?.data_plan?.bindings?.[alias] || null;
}

function serialize(record) {
  return structuredClone(record);
}

async function runBinding(alias, operation, payload) {
  dataCalls.push({ alias, operation, payload });
  if (config.api_latency_ms) await sleep(Number(config.api_latency_ms));
  const grant = binding(alias);
  if (!grant) throw new Error("Binding '" + alias + "' is not declared");
  if (!Array.isArray(grant.operations) || !grant.operations.includes(operation)) {
    throw new Error("Binding '" + alias + "' does not allow operation '" + operation + "'");
  }
  if (operation === 'schema') return { alias, object_key: grant.object_key, fields: ['name', 'company', 'job_title', 'role', 'linkedin_status', 'notes'] };
  if (operation === 'list' || operation === 'query') {
    const limit = Math.max(1, Math.min(Number(payload?.limit || 500), 500));
    return records.slice(0, limit).map(serialize);
  }
  if (operation === 'get') {
    const recordId = numericRecordId(payload);
    const record = records.find((item) => item.id === recordId);
    if (!record) throw new Error('Record not found: ' + recordId);
    return serialize(record);
  }
  if (operation === 'update') {
    const recordId = numericRecordId(payload);
    const record = records.find((item) => item.id === recordId);
    if (!record) throw new Error('Record not found: ' + recordId);
    const patch = payload?.dataPatch || payload?.data_patch || payload?.patch || payload?.data || {};
    record.data = { ...(record.data || {}), ...patch };
    record.version = Number(record.version || 1) + 1;
    return serialize(record);
  }
  if (operation === 'aggregate') return { total: records.length, groups: [] };
  throw new Error('Operation not implemented in eval harness: ' + operation);
}

function numericRecordId(payload) {
  const value = payload?.recordId ?? payload?.record_id ?? payload?.id;
  const number = Number(value);
  if (!Number.isInteger(number) || number <= 0) throw new Error('recordId must be numeric');
  return number;
}

function respond(requestId, data, error) {
  if (!requestId) return;
  frame.contentWindow.postMessage({ source: 'illo-host', type: 'illo:response', requestId, data, error }, '*');
}

window.addEventListener('message', async (event) => {
  const message = event.data || {};
  if (message.source === 'illo-app') {
    if (message.type === 'illo:ready') {
      bridgeReadyAt = performance.now();
      frame.contentWindow.postMessage({
        source: 'illo-host',
        type: 'illo:init',
        app: { ...config.app, manifest: config.manifest, visualSpec: {} },
        state: {},
        theme: { id: 'eval', mode: 'dark', colorScheme: 'dark', accent: '#4BACB8', kit: 'constellation-app-kit', surface: 'workspace' }
      }, '*');
      return;
    }
    if (message.type === 'illo:state:get') return respond(message.requestId, {});
    if (message.type === 'illo:state:set' || message.type === 'illo:state:update') return respond(message.requestId, {});
    if (message.type === 'illo:binding') {
      try {
        respond(message.requestId, await runBinding(message.alias, message.operation, message.payload || {}));
      } catch (error) {
        respond(message.requestId, null, error && error.message ? error.message : String(error));
      }
      return;
    }
    if (message.type === 'illo:action:run') return respond(message.requestId, { ok: true });
  }
  if (message.source === 'illo-eval' && message.type === 'done') {
    const metrics = message.metrics || {};
    window.__APP_CAPSULE_EVAL_RESULT__ = {
      browser_pass: metrics.ok ? 1 : 0,
      bridge_ready_ms: bridgeReadyAt ? Math.round(bridgeReadyAt - startedAt) : null,
      mount_ms: Math.round(performance.now() - startedAt),
      data_call_count: dataCalls.length,
      data_operations: dataCalls.map((call) => call.operation),
      ...metrics
    };
  }
});
`;
}

function bridgeScript(app) {
  return `<script>
(function () {
  const pending = new Map();
  let sequence = 0;
  let currentState = {};
  function nextId() {
    sequence += 1;
    return 'illo-' + Date.now().toString(36) + '-' + sequence.toString(36);
  }
  function request(type, payload) {
    const requestId = nextId();
    parent.postMessage({ source: 'illo-app', type, requestId, ...(payload || {}) }, '*');
    return new Promise((resolve, reject) => {
      pending.set(requestId, { resolve, reject });
      setTimeout(() => {
        if (!pending.has(requestId)) return;
        pending.delete(requestId);
        reject(new Error('Illo app bridge timed out'));
      }, 8000);
    });
  }
  function binding(alias) {
    const normalizedAlias = String(alias || '').trim();
    if (!normalizedAlias) throw new Error('window.illo.data(alias) requires an alias');
    function run(operation, payload) {
      return request('illo:binding', { alias: normalizedAlias, operation, payload: payload || {} });
    }
    return {
      schema: (options) => run('schema', options),
      list: (options) => run('list', options),
      query: (options) => run('query', options),
      get: (recordId, options) => run('get', { recordId, ...(options || {}) }),
      create: (data, options) => run('create', { data: data || {}, ...(options || {}) }),
      update: (recordId, dataPatch, options) => run('update', { recordId, dataPatch: dataPatch || {}, ...(options || {}) }),
      archive: (recordId, options) => run('archive', { recordId, ...(options || {}) }),
      aggregate: (options) => run('aggregate', options)
    };
  }
  const stateApi = {
    get: () => request('illo:state:get'),
    set: (data) => request('illo:state:set', { data: data || {} }),
    update: (patch) => request('illo:state:update', { patch: patch || {} }),
    value: () => currentState
  };
  window.illo = {
    app: ${safeScriptJson(app)},
    theme: {},
    data: binding,
    state: stateApi,
    actions: { run: (actionKey, payload) => request('illo:action:run', { actionKey: String(actionKey || ''), payload: payload || {} }) },
    toast: (message) => parent.postMessage({ source: 'illo-app', type: 'illo:toast', message: String(message || '') }, '*'),
    getState: stateApi.get,
    setState: stateApi.set,
    updateState: stateApi.update
  };
  window.addEventListener('message', (event) => {
    const message = event.data || {};
    if (message.source !== 'illo-host') return;
    if (message.type === 'illo:init' || message.type === 'illo:state') {
      currentState = message.state || {};
      window.illo.app = message.app || window.illo.app;
      window.illo.theme = message.theme || window.illo.theme || {};
      window.dispatchEvent(new CustomEvent('illo:state', { detail: currentState }));
      return;
    }
    if (message.type === 'illo:response' && pending.has(message.requestId)) {
      const handlers = pending.get(message.requestId);
      pending.delete(message.requestId);
      if (message.error) handlers.reject(new Error(String(message.error)));
      else handlers.resolve(message.data);
    }
  });
  function ready() {
    parent.postMessage({ source: 'illo-app', type: 'illo:ready' }, '*');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, { once: true });
  else setTimeout(ready, 0);
})();
<\/script>`;
}

function evalProbeScript() {
  return `<script>
(function () {
  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
  async function waitFor(check, timeoutMs) {
    const deadline = performance.now() + timeoutMs;
    while (performance.now() < deadline) {
      if (check()) return true;
      await sleep(25);
    }
    return false;
  }
  function text(selector) {
    return document.querySelector(selector)?.textContent || '';
  }
  function report(metrics) {
    parent.postMessage({ source: 'illo-eval', type: 'done', metrics }, '*');
  }
  async function run() {
    try {
      const rowsReady = await waitFor(() => document.querySelectorAll('[data-record-row]').length > 0, 8000);
      const firstInput = document.querySelector('[data-note-input]');
      const firstButton = document.querySelector('[data-note-save]');
      if (!rowsReady || !firstInput || !firstButton) throw new Error('CRM rows or note controls did not render');
      firstInput.value = 'Eval note saved';
      firstInput.dispatchEvent(new Event('input', { bubbles: true }));
      firstButton.click();
      const noteSaved = await waitFor(() => {
        const input = document.querySelector('[data-note-input]');
        return input && input.value === 'Eval note saved';
      }, 5000);
      const wrap = document.querySelector('.illo-table-wrap');
      const horizontalOverflow =
        document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 ||
        document.body.scrollWidth > document.body.clientWidth + 1;
      report({
        ok: rowsReady && noteSaved && !horizontalOverflow,
        row_count_rendered: document.querySelectorAll('[data-record-row]').length,
        note_update_passed: noteSaved,
        horizontal_overflow: horizontalOverflow,
        internal_table_scroll: wrap ? wrap.scrollWidth > wrap.clientWidth + 1 : false,
        dom_nodes: document.getElementsByTagName('*').length,
        status_text: text('#status')
      });
    } catch (error) {
      report({ ok: false, probe_error: error && error.message ? error.message : String(error) });
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(run, 0), { once: true });
  else setTimeout(run, 0);
})();
<\/script>`;
}

function runtimeStyle() {
  return `<style>
:root {
  color-scheme: dark;
  --illo-font: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  --illo-accent: #4BACB8;
  --illo-bg: rgba(6, 10, 18, 0.96);
  --illo-panel: rgba(13, 19, 31, 0.94);
  --illo-panel-strong: rgba(20, 28, 44, 0.98);
  --illo-border: rgba(255, 255, 255, 0.12);
  --illo-text: rgba(244, 247, 251, 0.96);
  --illo-muted: rgba(226, 232, 240, 0.66);
  --illo-soft: rgba(255, 255, 255, 0.07);
  --illo-radius-md: 8px;
  --illo-control-height: 36px;
  font-family: var(--illo-font);
}
* { box-sizing: border-box; }
html, body { width: 100%; min-width: 0; min-height: 100%; margin: 0; overflow: hidden; background: transparent; color: var(--illo-text); }
button, input, textarea, select { font: inherit; color: inherit; }
button { cursor: pointer; }
.illo-generated-app-root { width: 100%; min-height: 100%; }
.illo-app { width: 100%; min-height: 100vh; display: grid; gap: 16px; padding: clamp(16px, 3vw, 32px); color: var(--illo-text); font-family: var(--illo-font); background: var(--illo-bg); }
.illo-panel { min-width: 0; border: 1px solid var(--illo-border); border-radius: var(--illo-radius-md); background: var(--illo-panel); overflow: hidden; }
.illo-toolbar, .illo-row { display: flex; min-width: 0; align-items: center; gap: 10px; }
.illo-toolbar { flex-wrap: wrap; justify-content: space-between; }
.illo-stack { display: grid; gap: 12px; }
.illo-input, .illo-app input:not([type='checkbox']):not([type='radio']):not([type='hidden']) { min-width: 0; width: 100%; border: 1px solid var(--illo-border); border-radius: var(--illo-radius-md); background: var(--illo-panel-strong); color: var(--illo-text); padding: 10px 12px; outline: none; }
.illo-button, .illo-app button { min-height: var(--illo-control-height); border: 1px solid var(--illo-border); border-radius: var(--illo-radius-md); background: var(--illo-panel-strong); color: var(--illo-text); padding: 9px 13px; font-weight: 700; line-height: 1; }
.illo-title { margin: 0; color: var(--illo-text); font-size: clamp(22px, 4vw, 34px); line-height: 1.08; letter-spacing: 0; }
.illo-muted { color: var(--illo-muted); }
.illo-table-wrap { min-width: 0; overflow: auto; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--illo-border); text-align: left; vertical-align: top; }
th { color: var(--illo-muted); font-size: 12px; font-weight: 700; }
</style>`;
}

function safeScriptJson(value) {
  return JSON.stringify(value).replace(/</g, '\\u003c');
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/'/g, '&#39;');
}

async function run() {
  const options = parseArgs(process.argv.slice(2));
  const config = JSON.parse(await readFile(options.input, 'utf8'));
  const consoleMessages = [];
  const chrome = await launchChrome(config);
  const client = new CdpClient(chrome.pageWsUrl);
  try {
    await client.connect();
    client.on('Runtime.consoleAPICalled', (params) => {
      consoleMessages.push({
        type: params.type,
        text: (params.args || []).map((arg) => arg.value || arg.description || '').join(' '),
      });
    });
    client.on('Runtime.exceptionThrown', (params) => {
      consoleMessages.push({
        type: 'exception',
        text: params.exceptionDetails?.text || params.exceptionDetails?.exception?.description || 'exception',
      });
    });
    await client.send('Runtime.enable');
    await client.send('Page.enable');
    const html = buildOuterHtml(config);
    await client.send('Page.navigate', { url: `data:text/html;charset=utf-8,${encodeURIComponent(html)}` });
    const result = await waitForResult(client, Number(config.timeout_ms || 10000));
    if (config.screenshot_path) {
      const screenshot = await client.send('Page.captureScreenshot', {
        format: 'png',
        captureBeyondViewport: false,
        fromSurface: true,
      });
      await mkdir(path.dirname(config.screenshot_path), { recursive: true });
      await writeFile(config.screenshot_path, Buffer.from(screenshot.data, 'base64'));
      result.screenshot_path = config.screenshot_path;
    }
    const errors = consoleMessages.filter((message) => message.type === 'error' || message.type === 'exception');
    result.console_errors = errors.length;
    result.console_messages = consoleMessages;
    console.log(JSON.stringify(result));
  } finally {
    client.close();
    await chrome.close();
  }
}

run().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
