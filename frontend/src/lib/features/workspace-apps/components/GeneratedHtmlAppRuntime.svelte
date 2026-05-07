<script lang="ts">
  import { browser } from '$app/environment';
  import { onMount } from 'svelte';

  import {
    createDomainRecord,
    getDomain,
    getDomainRecord,
    listDomainRecords,
    removeDomainRecord,
    updateDomainRecord,
    type WorkspaceAppRead,
  } from '$lib/features/workspace-apps/api/workspaceAppsApi';
  import { ConstellationIcon, ConstellationIconButton, ConstellationPill } from '$lib/components/constellation';
  import { theme } from '$lib/stores/theme.svelte';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { normalizeDomainRequest, withDomainRecordAliases } from '$lib/utils/generatedAppBridge';

  type RuntimeMessage = {
    source?: string;
    type?: string;
    requestId?: string;
    data?: Record<string, any>;
    patch?: Record<string, any>;
    domain?: Record<string, any>;
    alias?: string;
    message?: string;
  };

  type DomainRequest = ReturnType<typeof normalizeDomainRequest>;
  type DomainInflightRequest = {
    startedAt: number;
    promise: Promise<unknown>;
  };

  let {
    app,
    surface = 'workspace',
    onclose,
  }: {
    app: WorkspaceAppRead;
    surface?: 'workspace' | 'dock';
    onclose?: () => void;
  } = $props();

  let iframeEl = $state<HTMLIFrameElement | null>(null);
  let stateData = $state<Record<string, any>>({});
  let loading = $state(true);
  let saving = $state(false);
  let loadKey = $state('');
  let frameReady = $state(false);
  let lastInitSignature = '';
  const domainCreateInflight = new Map<string, DomainInflightRequest>();
  const DOMAIN_CREATE_DEDUPE_MS = 2500;

  const activeVersion = $derived(app.active_version);
  const manifest = $derived(activeVersion?.manifest ?? {});
  const stateKey = $derived(String(manifest.state_key || 'default'));
  const sourceCode = $derived(activeVersion?.source_code || fallbackSource());
  const appAccent = $derived(String(app.visual_spec?.accent || '#57CFA0'));
  const srcdoc = $derived(buildSrcdoc(sourceCode, app.name, manifest, theme.mode, appAccent));
  const versionLabel = $derived(activeVersion ? `v${activeVersion.version}` : 'draft');

  function fallbackSource() {
    return `
      <section class="empty-app">
        <h1>${escapeHtml(app.name)}</h1>
        <p>This workspace app has no generated source yet.</p>
      </section>
    `;
  }

  function jsonForScript(value: unknown) {
    return JSON.stringify(value).replace(/</g, '\\u003c');
  }

  function escapeHtml(value: string) {
    return value
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function stableJsonValue(value: unknown): unknown {
    if (Array.isArray(value)) return value.map((item) => stableJsonValue(item));
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => [key, stableJsonValue(item)]),
    );
  }

  function stableSignature(value: unknown) {
    return JSON.stringify(stableJsonValue(value));
  }

  function bridgeScript() {
    return `<script>
      (function () {
        const pending = new Map();
        let sequence = 0;
        let lastStateSignature = '';
        let lastThemeSignature = '';
        let lastAppSignature = '';

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
              reject(new Error('Illo host bridge timed out'));
            }, 8000);
          });
        }

        function applyTheme(nextTheme) {
          const mode = nextTheme && nextTheme.mode === 'light' ? 'light' : 'dark';
          document.documentElement.setAttribute('data-illo-theme', mode);
          document.documentElement.style.colorScheme = mode;
        }

        function stableSignature(value) {
          try {
            return JSON.stringify(value || {});
          } catch (error) {
            return String(Date.now());
          }
        }

        function logBridgeWarnings(warnings) {
          if (!Array.isArray(warnings) || !warnings.length) return;
          warnings.forEach((warning) => console.warn('[Illo app bridge]', warning));
        }

        function domain(alias) {
          const normalizedAlias = String(alias || '').trim();
          if (!normalizedAlias) throw new Error('window.illo.domain(alias) requires an alias');
          return {
            schema: () => request('illo:domain:schema', { alias: normalizedAlias, domain: { alias: normalizedAlias } }),
            list: (options) => request('illo:domain:list', { alias: normalizedAlias, domain: { alias: normalizedAlias, ...(options || {}) } }),
            query: (options) => request('illo:domain:query', { alias: normalizedAlias, domain: { alias: normalizedAlias, ...(options || {}) } }),
            get: (recordId) => request('illo:domain:get', { alias: normalizedAlias, domain: { alias: normalizedAlias, recordId } }),
            create: (data, options) => request('illo:domain:create', {
              alias: normalizedAlias,
              domain: { alias: normalizedAlias, data: data || {}, ...(options || {}) }
            }),
            update: (recordId, dataPatch, options) => request('illo:domain:update', {
              alias: normalizedAlias,
              domain: { alias: normalizedAlias, recordId, dataPatch: dataPatch || {}, ...(options || {}) }
            }),
            archive: (recordId) => request('illo:domain:archive', { alias: normalizedAlias, domain: { alias: normalizedAlias, recordId } })
          };
        }

        function compatibilityRequest(type, payload) {
          return request(type, { domain: payload || {} });
        }

        window.illo = {
          app: ${jsonForScript({ id: app.id, key: app.key, name: app.name })},
          state: {},
          theme: {},
          domain,
          getState: () => request('illo:state:get'),
          setState: (data) => request('illo:state:set', { data }),
          updateState: (patch) => request('illo:state:update', { patch }),
          domains: {
            schema: (payload) => compatibilityRequest('illo:domain:schema', payload),
            list: (payload) => compatibilityRequest('illo:domain:list', payload),
            query: (payload) => compatibilityRequest('illo:domain:query', payload),
            get: (payload) => compatibilityRequest('illo:domain:get', payload),
            create: (payload) => compatibilityRequest('illo:domain:create', payload),
            update: (payload) => compatibilityRequest('illo:domain:update', payload),
            archive: (payload) => compatibilityRequest('illo:domain:archive', payload)
          },
          toast: (message) => parent.postMessage({ source: 'illo-app', type: 'illo:toast', message: String(message || '') }, '*')
        };

        window.addEventListener('message', (event) => {
          const message = event.data || {};
          if (message.source !== 'illo-host') return;
          if (message.type === 'illo:init' || message.type === 'illo:state') {
            const nextApp = message.app || window.illo.app;
            const nextState = message.state || {};
            const nextTheme = message.theme || window.illo.theme || {};
            const appSignature = stableSignature(nextApp);
            const stateSignature = stableSignature(nextState);
            const themeSignature = stableSignature(nextTheme);
            const changed =
              appSignature !== lastAppSignature ||
              stateSignature !== lastStateSignature ||
              themeSignature !== lastThemeSignature;
            window.illo.app = nextApp;
            window.illo.state = nextState;
            window.illo.theme = nextTheme;
            if (themeSignature !== lastThemeSignature) applyTheme(window.illo.theme);
            lastAppSignature = appSignature;
            lastStateSignature = stateSignature;
            lastThemeSignature = themeSignature;
            if (changed) {
              window.runEvent(new CustomEvent('illo:state', { detail: window.illo.state }));
            }
            return;
          }
          if (message.type === 'illo:response' && pending.has(message.requestId)) {
            const handlers = pending.get(message.requestId);
            pending.delete(message.requestId);
            logBridgeWarnings(message.warnings);
            if (message.error) handlers.reject(new Error(String(message.error)));
            else handlers.resolve(message.data);
          }
        });

        function ready() {
          parent.postMessage({ source: 'illo-app', type: 'illo:ready' }, '*');
        }

        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', ready, { once: true });
        } else {
          setTimeout(ready, 0);
        }
      })();
    <\/script>`;
  }

  function runtimeStyle(themeMode: 'dark' | 'light', accent: string) {
    const dark = themeMode !== 'light';
    return `<style>
      :root {
        color-scheme: ${dark ? 'dark' : 'light'};
        --illo-font: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        font-family: var(--illo-font);
        background: transparent;
        --illo-accent: ${escapeHtml(accent)};
        --illo-bg: ${dark ? 'rgba(5, 9, 18, 0.88)' : 'rgba(252, 248, 238, 0.92)'};
        --illo-panel: ${dark ? 'rgba(11, 16, 28, 0.82)' : 'rgba(255, 252, 244, 0.88)'};
        --illo-panel-strong: ${dark ? 'rgba(18, 25, 42, 0.92)' : 'rgba(255, 255, 252, 0.96)'};
        --illo-border: ${dark ? 'rgba(255, 255, 255, 0.10)' : 'rgba(66, 52, 28, 0.14)'};
        --illo-text: ${dark ? 'rgba(244, 246, 250, 0.94)' : 'rgba(28, 24, 18, 0.92)'};
        --illo-muted: ${dark ? 'rgba(244, 246, 250, 0.58)' : 'rgba(61, 51, 38, 0.64)'};
        --illo-soft: ${dark ? 'rgba(255, 255, 255, 0.07)' : 'rgba(75, 61, 38, 0.08)'};
        --illo-focus: color-mix(in srgb, var(--illo-accent) 72%, white);
        --illo-danger: ${dark ? '#EF6F7B' : '#C94457'};
        --illo-radius-xs: 4px;
        --illo-radius-sm: 6px;
        --illo-radius-md: 8px;
        --illo-shadow: 0 14px 34px rgba(0, 0, 0, ${dark ? '0.24' : '0.12'});
        --illo-control-height: 36px;
      }

      * { box-sizing: border-box; }

      html,
      body {
        width: 100%;
        min-width: 0;
        min-height: 100%;
        margin: 0;
        overflow-x: hidden;
        background: transparent;
        color: var(--illo-text);
      }

      body {
        padding: 0;
      }

      button,
      input,
      textarea,
      select {
        font: inherit;
      }

      button,
      input,
      textarea,
      select {
        color: inherit;
      }

      button {
        cursor: pointer;
      }

      button:disabled,
      input:disabled,
      textarea:disabled,
      select:disabled {
        cursor: not-allowed;
        opacity: 0.55;
      }

      a {
        color: var(--illo-accent);
      }

      :focus-visible {
        outline: 2px solid var(--illo-focus);
        outline-offset: 2px;
      }

      img,
      canvas,
      svg,
      video {
        max-width: 100%;
      }

      .illo-generated-app-root {
        width: 100%;
        min-height: 100%;
      }

      .illo-app {
        width: 100%;
        min-height: 100%;
        display: grid;
        gap: 16px;
        padding: clamp(16px, 4vw, 28px);
        color: var(--illo-text);
        font-family: var(--illo-font);
        background:
          radial-gradient(circle at 18% 0%, color-mix(in srgb, var(--illo-accent) 12%, transparent), transparent 38%),
          var(--illo-bg);
      }

      .illo-panel {
        min-width: 0;
        border: 1px solid var(--illo-border);
        border-radius: 16px;
        background:
          linear-gradient(180deg, color-mix(in srgb, white 7%, transparent), transparent),
          var(--illo-panel);
        box-shadow: 0 16px 42px rgba(0, 0, 0, ${dark ? '0.22' : '0.10'});
      }

      .illo-toolbar,
      .illo-row {
        display: flex;
        min-width: 0;
        align-items: center;
        gap: 10px;
      }

      .illo-toolbar {
        flex-wrap: wrap;
        justify-content: space-between;
      }

      .illo-stack {
        display: grid;
        gap: 12px;
      }

      .illo-list {
        display: grid;
        gap: 8px;
        margin: 0;
        padding: 0;
        list-style: none;
      }

      .illo-app ul,
      .illo-app ol {
        display: grid;
        gap: 8px;
        margin: 0;
        padding: 0;
        list-style: none;
      }

      .illo-row {
        padding: 10px 12px;
        border: 1px solid var(--illo-border);
        border-radius: 12px;
        background: var(--illo-soft);
      }

      .illo-input,
      .illo-select,
      .illo-textarea,
      .illo-app input:not([type='checkbox']):not([type='radio']):not([type='hidden']),
      .illo-app textarea,
      .illo-app select {
        min-width: 0;
        width: 100%;
        border: 1px solid var(--illo-border);
        border-radius: var(--illo-radius-md);
        background: var(--illo-panel-strong);
        color: var(--illo-text);
        padding: 10px 12px;
        outline: none;
      }

      .illo-input:focus,
      .illo-select:focus,
      .illo-textarea:focus,
      .illo-app input:not([type='checkbox']):not([type='radio']):not([type='hidden']):focus,
      .illo-app textarea:focus,
      .illo-app select:focus,
      .illo-button:focus-visible,
      .illo-app button:focus-visible {
        border-color: color-mix(in srgb, var(--illo-accent) 60%, var(--illo-border));
        box-shadow: 0 0 0 3px color-mix(in srgb, var(--illo-accent) 22%, transparent);
      }

      .illo-button,
      .illo-app button {
        border: 1px solid var(--illo-border);
        border-radius: var(--illo-radius-md);
        background: var(--illo-panel-strong);
        color: var(--illo-text);
        padding: 9px 13px;
        font-weight: 700;
        line-height: 1;
        text-decoration: none;
        cursor: pointer;
        transition:
          background 120ms ease,
          border-color 120ms ease,
          transform 120ms ease;
      }

      .illo-button:hover,
      .illo-app button:hover {
        border-color: color-mix(in srgb, var(--illo-accent) 52%, var(--illo-border));
        transform: translateY(-1px);
      }

      .illo-button[data-variant='primary'],
      .illo-button.is-primary,
      .illo-button-primary {
        border-color: color-mix(in srgb, var(--illo-accent) 42%, var(--illo-border));
        background: color-mix(in srgb, var(--illo-accent) 18%, var(--illo-panel-strong));
      }

      .illo-button-danger {
        border-color: color-mix(in srgb, var(--illo-danger) 74%, transparent);
        background: var(--illo-danger);
        color: #FFFFFF;
      }

      .illo-tabs {
        display: inline-flex;
        gap: 4px;
        padding: 4px;
        border: 1px solid var(--illo-border);
        border-radius: 999px;
        background: var(--illo-soft);
      }

      .illo-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--illo-accent) 13%, var(--illo-soft));
        color: var(--illo-text);
        padding: 4px 8px;
        font-size: 12px;
        font-weight: 700;
      }

      .illo-empty {
        display: grid;
        min-height: 160px;
        place-content: center;
        color: var(--illo-muted);
        text-align: center;
      }

      .illo-title {
        margin: 0;
        color: var(--illo-text);
        font-size: clamp(22px, 5vw, 34px);
        line-height: 1.08;
        letter-spacing: 0;
      }

      .illo-copy,
      .illo-muted {
        color: var(--illo-muted);
      }

      .empty-app {
        display: grid;
        min-height: 280px;
        place-content: center;
        gap: 8px;
        padding: 28px;
        text-align: center;
      }

      .empty-app h1 {
        margin: 0;
        font-size: 20px;
        letter-spacing: 0;
      }

      .empty-app p {
        margin: 0;
        color: var(--illo-muted);
        font-size: 13px;
      }
    </style>`;
  }

  function injectIntoFullDocument(source: string, injections: string) {
    if (source.match(/<head[^>]*>/i)) {
      return source.replace(/<head([^>]*)>/i, `<head$1>${injections}`);
    }
    if (source.match(/<\/head>/i)) {
      return source.replace(/<\/head>/i, `${injections}</head>`);
    }
    if (source.match(/<body[^>]*>/i)) {
      return source.replace(/<body([^>]*)>/i, `<body$1>${injections}`);
    }
    return `${injections}${source}`;
  }

  function buildSrcdoc(
    source: string,
    title: string,
    runtimeManifest: Record<string, any>,
    themeMode: 'dark' | 'light',
    accent: string,
  ) {
    const injections = `
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>${escapeHtml(title)}</title>
      <script>window.__ILLO_APP_MANIFEST__ = ${jsonForScript(runtimeManifest || {})};<\/script>
      ${runtimeStyle(themeMode, accent)}
      ${bridgeScript()}
    `;

    if (/<html[\s>]/i.test(source)) {
      return injectIntoFullDocument(source, injections);
    }

    return `<!doctype html>
      <html>
        <head>${injections}</head>
        <body>
          <main class="illo-generated-app-root">${source}</main>
        </body>
      </html>`;
  }

  function frameWindow() {
    return iframeEl?.contentWindow ?? null;
  }

  function postToFrame(type: string, payload: Record<string, any> = {}) {
    const target = frameWindow();
    if (!target) return;
    target.postMessage({ source: 'illo-host', type, ...payload }, '*');
  }

  function buildInitPayload() {
    return {
      app: {
        id: app.id,
        key: app.key,
        name: app.name,
        description: app.description,
        manifest,
        visualSpec: app.visual_spec || {},
      },
      state: stateData,
      theme: {
        id: theme.id,
        mode: theme.mode,
        colorScheme: theme.mode,
        accent: appAccent,
        kit: 'constellation-app-kit',
        surface,
      },
    };
  }

  function sendInit(options: { force?: boolean } = {}) {
    if (!frameReady || loading) return;
    const payload = buildInitPayload();
    const signature = stableSignature({
      app: payload.app,
      state: payload.state,
      theme: payload.theme,
      versionId: activeVersion?.id ?? null,
    });
    if (!options.force && signature === lastInitSignature) return;
    lastInitSignature = signature;
    postToFrame('illo:init', payload);
  }

  function respond(requestId: string | undefined, data: unknown, error?: string, warnings: string[] = []) {
    if (!requestId) return;
    postToFrame('illo:response', {
      requestId,
      data,
      error,
      warnings: warnings.length ? warnings : undefined,
    });
  }

  async function loadState() {
    loading = true;
    try {
      stateData = (await workspaceApps.loadState(app.id, stateKey, { silent: true })) ?? {};
    } finally {
      loading = false;
      sendInit({ force: true });
    }
  }

  async function persistState(requestId: string | undefined, nextState: Record<string, any>) {
    stateData = nextState;
    workspaceApps.rememberState(app.id, stateKey, nextState);
    saving = true;
    try {
      stateData = await workspaceApps.updateState(app.id, stateKey, nextState);
      respond(requestId, stateData);
      postToFrame('illo:state', { state: stateData });
    } catch (err: any) {
      respond(requestId, null, err?.detail || err?.message || 'Failed to save state');
    } finally {
      saving = false;
    }
  }

  async function executeDomainRequest(request: DomainRequest): Promise<unknown> {
    switch (request.operation) {
      case 'schema':
        return getDomain(request.domainId);
      case 'list':
      case 'query':
        return withDomainRecordAliases(
          await listDomainRecords(request.domainId, {
            objectKey: request.objectKey,
            search: request.search,
            includeArchived: request.includeArchived,
            limit: request.limit,
          }),
        );
      case 'get':
        return withDomainRecordAliases(await getDomainRecord(request.domainId, request.recordId!));
      case 'create':
        return withDomainRecordAliases(
          await createDomainRecord(request.domainId, request.objectKey!, {
            data: request.data || {},
            title: request.title,
          }),
        );
      case 'update':
        return withDomainRecordAliases(
          await updateDomainRecord(request.domainId, request.recordId!, {
            data_patch: request.dataPatch || {},
            title: request.title,
            expected_version: request.expectedVersion,
          }),
        );
      case 'archive':
        return removeDomainRecord(request.domainId, request.recordId!, 'archive');
      default:
        throw new Error(`Unsupported Domain operation '${request.operation}'`);
    }
  }

  function domainCreateSignature(request: DomainRequest) {
    return stableSignature({
      operation: request.operation,
      domainId: request.domainId,
      objectKey: request.objectKey,
      title: request.title,
      data: request.data || {},
    });
  }

  function rememberDomainCreate(signature: string, promise: Promise<unknown>) {
    const entry = { startedAt: Date.now(), promise };
    domainCreateInflight.set(signature, entry);
    const scheduleCleanup = () => {
      setTimeout(() => {
        if (domainCreateInflight.get(signature) === entry) {
          domainCreateInflight.delete(signature);
        }
      }, DOMAIN_CREATE_DEDUPE_MS);
    };
    promise.then(scheduleCleanup, scheduleCleanup);
    return entry;
  }

  async function runDomainRequestWithDedupe(request: DomainRequest) {
    if (request.operation !== 'create') {
      return executeDomainRequest(request);
    }

    const signature = domainCreateSignature(request);
    const existing = domainCreateInflight.get(signature);
    if (existing && Date.now() - existing.startedAt <= DOMAIN_CREATE_DEDUPE_MS) {
      return existing.promise;
    }

    return rememberDomainCreate(signature, executeDomainRequest(request)).promise;
  }

  async function handleDomainRequest(message: RuntimeMessage) {
    const operation = message.type?.replace('illo:domain:', '') || '';
    try {
      const request = normalizeDomainRequest(manifest, operation, message.domain || {}, message.alias || null);
      const result = await runDomainRequestWithDedupe(request);
      respond(message.requestId, result, undefined, request.warnings);
    } catch (err: any) {
      respond(message.requestId, null, err?.detail || err?.message || 'Domain request failed');
    }
  }

  function handleMessage(event: MessageEvent<RuntimeMessage>) {
    if (!browser || event.source !== frameWindow()) return;
    const message = event.data || {};
    if (message.source !== 'illo-app') return;

    if (message.type === 'illo:ready') {
      frameReady = true;
      sendInit();
      return;
    }

    if (message.type === 'illo:state:get') {
      respond(message.requestId, stateData);
      return;
    }

    if (message.type === 'illo:state:set') {
      void persistState(message.requestId, message.data || {});
      return;
    }

    if (message.type === 'illo:state:update') {
      void persistState(message.requestId, { ...stateData, ...(message.patch || {}) });
      return;
    }

    if (message.type?.startsWith('illo:domain:')) {
      void handleDomainRequest(message);
      return;
    }

    if (message.type === 'illo:toast' && message.message) {
      ui.toast(message.message, 'info');
    }
  }

  function handleFrameLoad() {
    frameReady = true;
    lastInitSignature = '';
    sendInit({ force: true });
  }

  function handleCloseClick(event: MouseEvent) {
    event.preventDefault();
    event.stopPropagation();
    onclose?.();
  }

  onMount(() => {
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  });

  $effect(() => {
    const nextLoadKey = `${app.id}:${stateKey}:${activeVersion?.id ?? 'draft'}`;
    if (nextLoadKey === loadKey) return;
    loadKey = nextLoadKey;
    frameReady = false;
    lastInitSignature = '';
    void loadState();
  });

  $effect(() => {
    theme.mode;
    sendInit();
  });
</script>

<section class="generated-html-app generated-app-shell" class:is-dock={surface === 'dock'} aria-label={app.name}>
  <header class="generated-html-app__header generated-app-shell__header">
    <div class="generated-html-app__identity">
      <span class="generated-html-app__dot" style={`--app-accent:${appAccent}`} aria-hidden="true"></span>
      <div>
        <span class="generated-html-app__eyebrow">Generated app</span>
        <h2>{app.name}</h2>
      </div>
    </div>

    <div class="generated-html-app__actions">
      <ConstellationPill variant={saving ? 'warning' : 'info'} leadingDot>{saving ? 'saving' : loading ? 'loading' : versionLabel}</ConstellationPill>
      {#if onclose}
        <ConstellationIconButton label="Close generated app" variant="quiet" size="md" onclick={handleCloseClick}>
          <ConstellationIcon name="close" size={15} stroke={2} />
        </ConstellationIconButton>
      {/if}
    </div>
  </header>

  <iframe
    bind:this={iframeEl}
    class="generated-html-app__frame"
    title={app.name}
    sandbox="allow-scripts allow-forms allow-popups"
    {srcdoc}
    onload={handleFrameLoad}
  ></iframe>
</section>

<style>
.generated-html-app {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  width: min(760px, calc(100vw - 28px));
  height: min(720px, calc(100vh - 92px));
  min-width: 0;
  min-height: 360px;
  overflow: hidden;
  border-radius: 22px;
}

.generated-html-app.is-dock {
  width: 100%;
  height: 100%;
  min-height: 0;
  border-radius: 0;
}

.generated-html-app__header {
  position: relative;
  z-index: 2;
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 13px 14px;
}

.generated-html-app.is-dock .generated-html-app__header {
  padding-inline: 0;
}

.generated-html-app__identity,
.generated-html-app__actions {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 10px;
}

.generated-html-app__dot {
  --app-accent: var(--positive);
  width: 12px;
  height: 12px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: var(--app-accent);
  box-shadow: 0 0 18px color-mix(in srgb, var(--app-accent) 42%, transparent);
}

.generated-html-app__eyebrow {
  display: block;
  color: var(--constellation-label-meta);
  font-family: var(--constellation-font-mono);
  font-size: 9px;
  font-weight: 680;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}

.generated-html-app h2 {
  overflow: hidden;
  margin: 2px 0 0;
  color: var(--constellation-section-title);
  font-size: 14px;
  font-weight: 720;
  letter-spacing: 0;
  line-height: 1.2;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.generated-html-app__frame {
  width: 100%;
  min-width: 0;
  height: 100%;
  min-height: 0;
  border: 0;
  background: transparent;
}

@media (max-width: 720px) {
  .generated-html-app {
    width: calc(100vw - 20px);
    height: min(720px, calc(100vh - 74px));
    min-height: 320px;
    border-radius: 18px;
  }
}

</style>
