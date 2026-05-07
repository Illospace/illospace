<script lang="ts">
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';

  type CallbackStatus = 'working' | 'success' | 'error';

  let status = $state<CallbackStatus>('working');
  let message = $state('Finishing ChatGPT / Codex connection...');

  onMount(async () => {
    const state = callbackState();
    try {
      await api.exchangeRuntimeOpenAIOAuth({ callback: window.location.href });
      const introUrl = await cortexIntroUrl();
      status = 'success';
      message = 'ChatGPT / Codex connected. Opening Cortex...';
      notifySystem({ status: 'success', state });
      scheduleCompletion(introUrl);
    } catch (err: any) {
      status = 'error';
      message = err?.detail || err?.message || 'Failed to finish ChatGPT / Codex login.';
      notifySystem({ status: 'error', state, detail: message });
    }
  });

  function callbackState() {
    try {
      return new URL(window.location.href).searchParams.get('state') || '';
    } catch {
      return '';
    }
  }

  function notifySystem(payload: { status: 'success' | 'error'; state: string; detail?: string }) {
    const messagePayload = {
      type: 'illo:openai-oauth',
      ...payload,
    };
    try {
      window.opener?.postMessage(messagePayload, window.location.origin);
    } catch {
      // The return link remains available when a browser blocks opener messaging.
    }
    try {
      const channel = new BroadcastChannel('illo:openai-oauth');
      channel.postMessage(messagePayload);
      channel.close();
    } catch {
      // BroadcastChannel is a convenience fallback, not a requirement.
    }
  }

  async function cortexIntroUrl() {
    try {
      const intro = await api.startRuntimeReadyIntro();
      if (intro?.idea_id) {
        const params = new URLSearchParams({
          idea: intro.idea_id,
          onboarding: 'runtime-ready',
        });
        return `/cortex?${params.toString()}`;
      }
    } catch {
      // The System page listener also starts the intro when this is a popup.
    }
    return '/cortex';
  }

  function scheduleCompletion(returnUrl: string) {
    window.setTimeout(() => {
      window.close();
      window.setTimeout(() => {
        window.location.assign(returnUrl);
      }, 250);
    }, 900);
  }
</script>

<svelte:head>
  <title>OpenAI Callback</title>
</svelte:head>

<div class="callback-page">
  <div class="callback-card">
    <div class="callback-badge callback-badge--{status}">
      {status === 'working' ? 'Connecting' : status === 'success' ? 'Connected' : 'Failed'}
    </div>
    <h1>OpenAI / Codex</h1>
    <p>{message}</p>
    <a class="callback-link" href="/cortex">Open Cortex</a>
  </div>
</div>

<style>
  .callback-page {
    min-height: 100vh;
    display: grid;
    place-items: center;
    padding: 32px 20px;
    background: linear-gradient(180deg, var(--bg-1), var(--bg-2));
  }

  .callback-card {
    width: min(520px, 100%);
    padding: 28px;
    border-radius: 12px;
    border: 1px solid var(--border-2);
    background: color-mix(in srgb, var(--bg-1) 92%, white 8%);
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.16);
  }

  .callback-badge {
    display: inline-flex;
    align-items: center;
    border-radius: 999px;
    padding: 6px 12px;
    font-size: var(--text-xs);
    font-weight: var(--weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 14px;
  }

  .callback-badge--working {
    color: #0369a1;
    background: color-mix(in srgb, #0ea5e9 14%, var(--bg-1) 86%);
  }

  .callback-badge--success {
    color: #059669;
    background: color-mix(in srgb, #10b981 14%, var(--bg-1) 86%);
  }

  .callback-badge--error {
    color: #dc2626;
    background: color-mix(in srgb, #ef4444 14%, var(--bg-1) 86%);
  }

  h1 {
    margin: 0 0 12px;
    font-size: clamp(1.5rem, 3vw, 2rem);
  }

  p {
    margin: 0 0 18px;
    color: var(--text-2);
    line-height: 1.5;
  }

  .callback-link {
    color: var(--accent);
    font-weight: var(--weight-medium);
    text-decoration: none;
  }

  .callback-link:hover {
    text-decoration: underline;
  }
</style>
