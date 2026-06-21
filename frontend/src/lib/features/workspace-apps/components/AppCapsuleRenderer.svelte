<script lang="ts">
  import { browser } from '$app/environment';
  import { onMount } from 'svelte';

  import { ConstellationIcon, ConstellationPill } from '$lib/components/constellation';
  import type { GeneratedAppSurface } from '$lib/features/workspace-apps/domain/generatedAppSurface';
  import {
    appendWorkspaceAppEvent,
    getWorkspaceAppCollaboration,
    listWorkspaceAppEvents,
    runWorkspaceAppAction,
    runWorkspaceAppBinding,
    type WorkspaceAppCollaborationRead,
    type WorkspaceAppRead,
  } from '$lib/features/workspace-apps/api/workspaceAppsApi';
  import {
    buildAppCapsuleSrcdoc,
    fallbackAppCapsuleSource,
    stableSignature,
  } from '$lib/features/workspace-apps/runtime/appCapsuleRuntime';
  import { theme } from '$lib/stores/theme.svelte';
  import { ui } from '$lib/stores/ui.svelte';
  import { workspaceApps } from '$lib/stores/workspaceApps.svelte';

  import GeneratedAppChrome from './GeneratedAppChrome.svelte';

  type RuntimeMessage = {
    source?: string;
    type?: string;
    requestId?: string;
    alias?: string;
    operation?: string;
    payload?: Record<string, any>;
    data?: Record<string, any>;
    patch?: Record<string, any>;
    statePatch?: Record<string, any>;
    actionKey?: string;
    eventType?: string;
    stateKey?: string;
    idempotencyKey?: string;
    expectedStateVersion?: number;
    afterEventId?: number;
    limit?: number;
    metadata?: Record<string, any>;
    message?: string;
  };

  let {
    app,
    surface = 'workspace',
    onclose,
  }: {
    app: WorkspaceAppRead;
    surface?: GeneratedAppSurface;
    onclose?: () => void;
  } = $props();

  let iframeEl = $state<HTMLIFrameElement | null>(null);
  let stateData = $state<Record<string, any>>({});
  let loading = $state(true);
  let saving = $state(false);
  let loadKey = $state('');
  let frameReady = $state(false);
  let smokeError = $state<string | null>(null);
  let smokeTimer: ReturnType<typeof setTimeout> | null = null;
  let lastInitSignature = '';
  let frameGeneration = 0;
  let readyGeneration = -1;
  let srcdocGenerationSignature = '';

  const activeVersion = $derived(app.active_version);
  const manifest = $derived(activeVersion?.manifest ?? {});
  const stateKey = $derived(String(manifest.state_key || 'default'));
  const sourceCode = $derived(activeVersion?.source_code || fallbackAppCapsuleSource(app.name));
  const appAccent = $derived(String(app.visual_spec?.accent || app.visual_spec?.thumbnail?.accent || '#4BACB8'));
  const srcdoc = $derived(
    buildAppCapsuleSrcdoc({
      source: sourceCode,
      title: app.name,
      manifest,
      themeMode: theme.mode,
      accent: appAccent,
      app: { id: app.id, key: app.key, name: app.name },
    }),
  );
  const versionLabel = $derived(activeVersion ? `v${activeVersion.version}` : 'draft');

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

  function currentFrameReady() {
    return frameReady && readyGeneration === frameGeneration;
  }

  function sendInit(options: { force?: boolean } = {}) {
    if (!currentFrameReady() || loading) return;
    const payload = buildInitPayload();
    const signature = stableSignature({
      app: payload.app,
      state: payload.state,
      theme: payload.theme,
      versionId: activeVersion?.id ?? null,
      frameGeneration,
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

  function rememberCollaboration(result: WorkspaceAppCollaborationRead) {
    const resultState = result?.state;
    if (resultState?.key === stateKey) {
      stateData = resultState.data || {};
      workspaceApps.rememberState(app.id, stateKey, stateData);
      postToFrame('illo:state', { state: stateData });
    }
    postToFrame('illo:collab', { collaboration: result });
  }

  async function handleCollaborationGet(message: RuntimeMessage) {
    try {
      const result = await getWorkspaceAppCollaboration(app.id, {
        state_key: message.stateKey || undefined,
        after_event_id: message.afterEventId ?? undefined,
        limit: message.limit ?? undefined,
      });
      rememberCollaboration(result);
      respond(message.requestId, result);
    } catch (err: any) {
      respond(message.requestId, null, err?.detail || err?.message || 'Collaboration request failed');
    }
  }

  async function handleCollaborationEvents(message: RuntimeMessage) {
    try {
      const result = await listWorkspaceAppEvents(app.id, {
        after_event_id: message.afterEventId ?? undefined,
        event_type: message.eventType || undefined,
        limit: message.limit ?? undefined,
      });
      respond(message.requestId, result);
    } catch (err: any) {
      respond(message.requestId, null, err?.detail || err?.message || 'Collaboration events request failed');
    }
  }

  async function handleCollaborationEvent(message: RuntimeMessage) {
    try {
      const eventType = String(message.eventType || '').trim();
      if (!eventType) throw new Error('collab.event(eventType, payload) requires an event type');
      const result = await appendWorkspaceAppEvent(app.id, {
        event_type: eventType,
        payload: message.payload || {},
        state_patch: message.statePatch || message.patch || null,
        state_key: message.stateKey || undefined,
        idempotency_key: message.idempotencyKey || undefined,
        expected_state_version: message.expectedStateVersion ?? undefined,
        metadata: message.metadata || {},
      });
      rememberCollaboration(result);
      respond(message.requestId, result);
    } catch (err: any) {
      respond(message.requestId, null, err?.detail || err?.message || 'Collaboration event failed');
    }
  }

  async function handleBindingRequest(message: RuntimeMessage) {
    try {
      const alias = String(message.alias || '').trim();
      const operation = String(message.operation || '').trim();
      if (!alias) throw new Error('window.illo.data(alias) requires an alias');
      if (!operation) throw new Error('Data operation is required');
      const result = await runWorkspaceAppBinding(app.id, alias, operation, {
        payload: message.payload || {},
      });
      respond(message.requestId, result.data, undefined, result.warnings || []);
    } catch (err: any) {
      respond(message.requestId, null, err?.detail || err?.message || 'Data request failed');
    }
  }

  async function handleActionRequest(message: RuntimeMessage) {
    try {
      const actionKey = String(message.actionKey || '').trim();
      if (!actionKey) throw new Error('actions.run(actionKey) requires an action key');
      const result = await runWorkspaceAppAction(app.id, {
        action_key: actionKey,
        payload: message.payload || {},
      });
      respond(message.requestId, result);
    } catch (err: any) {
      respond(message.requestId, null, err?.detail || err?.message || 'Workspace action failed');
    }
  }

  function clearSmokeTimer() {
    if (!smokeTimer) return;
    clearTimeout(smokeTimer);
    smokeTimer = null;
  }

  function markFramePending() {
    frameGeneration += 1;
    readyGeneration = -1;
    frameReady = false;
    smokeError = null;
    lastInitSignature = '';
  }

  function scheduleSmokeGate(generation: number) {
    clearSmokeTimer();
    smokeTimer = setTimeout(() => {
      if (generation !== frameGeneration || frameReady) return;
      smokeError = 'This app capsule did not finish connecting to the Illo runtime bridge.';
    }, 3000);
  }

  function handleMessage(event: MessageEvent<RuntimeMessage>) {
    if (!browser || event.source !== frameWindow()) return;
    const message = event.data || {};
    if (message.source !== 'illo-app') return;

    if (message.type === 'illo:ready') {
      readyGeneration = frameGeneration;
      frameReady = true;
      smokeError = null;
      clearSmokeTimer();
      sendInit({ force: true });
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

    if (message.type === 'illo:binding') {
      void handleBindingRequest(message);
      return;
    }

    if (message.type === 'illo:action:run') {
      void handleActionRequest(message);
      return;
    }

    if (message.type === 'illo:collab:get') {
      void handleCollaborationGet(message);
      return;
    }

    if (message.type === 'illo:collab:events') {
      void handleCollaborationEvents(message);
      return;
    }

    if (message.type === 'illo:collab:event') {
      void handleCollaborationEvent(message);
      return;
    }

    if (message.type === 'illo:toast' && message.message) {
      ui.toast(message.message, 'info');
    }
  }

  function handleFrameLoad() {
    lastInitSignature = '';
    scheduleSmokeGate(frameGeneration);
  }

  onMount(() => {
    window.addEventListener('message', handleMessage);
    return () => {
      clearSmokeTimer();
      window.removeEventListener('message', handleMessage);
    };
  });

  $effect(() => {
    const nextSignature = stableSignature({ srcdoc });
    if (!srcdocGenerationSignature) {
      srcdocGenerationSignature = nextSignature;
      return;
    }
    if (nextSignature === srcdocGenerationSignature) return;
    srcdocGenerationSignature = nextSignature;
    markFramePending();
  });

  $effect(() => {
    const nextLoadKey = `${app.id}:${stateKey}:${activeVersion?.id ?? 'draft'}`;
    if (nextLoadKey === loadKey) return;
    loadKey = nextLoadKey;
    void loadState();
  });

  $effect(() => {
    theme.mode;
    sendInit();
  });
</script>

<GeneratedAppChrome
  className="generated-app-shell app-capsule"
  title={app.name}
  accent={appAccent}
  {surface}
  {onclose}
  closeLabel="Close workspace app"
>
  {#snippet actions()}
    <ConstellationPill variant={smokeError ? 'danger' : saving ? 'warning' : 'info'} leadingDot>
      {smokeError ? 'error' : saving ? 'saving' : loading ? 'loading' : versionLabel}
    </ConstellationPill>
  {/snippet}

  <div class="app-capsule__body">
    <iframe
      bind:this={iframeEl}
      class="app-capsule__frame"
      title={app.name}
      sandbox="allow-scripts allow-forms allow-popups"
      {srcdoc}
      onload={handleFrameLoad}
    ></iframe>

    {#if smokeError}
      <div class="app-capsule__error" role="alert">
        <ConstellationIcon name="runtime" size={18} stroke={1.9} />
        <div>
          <strong>App runtime failed</strong>
          <p>{smokeError}</p>
        </div>
      </div>
    {/if}
  </div>
</GeneratedAppChrome>

<style>
:global(.generated-app-chrome.app-capsule) {
  width: min(1400px, calc(100vw - 32px));
  height: min(900px, calc(100vh - 56px));
  min-width: min(360px, calc(100vw - 32px));
  min-height: min(520px, calc(100vh - 56px));
  overflow: hidden;
  border-radius: var(--radius-lg);
}

:global(.generated-app-chrome.app-capsule.is-dock) {
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 100%;
  border-radius: 0;
}

.app-capsule__body {
  position: relative;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}

.app-capsule__frame {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
  background: transparent;
}

.app-capsule__error {
  position: absolute;
  inset: 12px;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  max-width: 480px;
  margin: auto;
  padding: 14px;
  border: 1px solid var(--constellation-control-pill-danger-border);
  border-radius: var(--radius-md);
  background: var(--constellation-surface-panel-background);
  color: var(--constellation-section-title);
  box-shadow: var(--constellation-surface-panel-shadow);
}

.app-capsule__error strong {
  display: block;
  margin: 0 0 4px;
  font-size: 13px;
  letter-spacing: 0;
}

.app-capsule__error p {
  margin: 0;
  color: var(--constellation-section-description);
  font-size: 12px;
  line-height: 1.4;
}
</style>
