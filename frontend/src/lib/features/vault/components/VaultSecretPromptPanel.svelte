<script lang="ts">
  import { onMount } from 'svelte';
  import {
    ConstellationButton,
    ConstellationIcon,
    ConstellationPill,
  } from '$lib/components/constellation';
  import { createSecret, pinStatus, vaultUnlock } from '$lib/features/vault/api/vaultApi';
  import { ui } from '$lib/stores/ui.svelte';
  import type { VaultSecretPrompt } from '$lib/types/cortex';

  const CATEGORIES = [
    'general',
    'api',
    'aws',
    'auth',
    'analytics',
    'database',
    'messaging',
    'monitoring',
    'payments',
    'service',
  ];

  let {
    prompt,
    onSaved,
    onDismiss,
  }: {
    prompt: VaultSecretPrompt | null;
    onSaved?: (promptId: string) => void;
    onDismiss?: (promptId: string) => void;
  } = $props();

  let activePromptId = $state<string | null>(null);
  let keyName = $state('');
  let secretValue = $state('');
  let description = $state('');
  let category = $state('api');
  let hasPin = $state(false);
  let vaultLocked = $state(false);
  let vaultToken = $state<string | null>(null);
  let pinInput = $state('');
  let checkingPin = $state(false);
  let unlocking = $state(false);
  let saving = $state(false);
  let showSecret = $state(false);

  const canSave = $derived(
    Boolean(keyName.trim() && secretValue.trim() && !saving && !vaultLocked),
  );

  $effect(() => {
    if (prompt?.id === activePromptId) return;
    activePromptId = prompt?.id ?? null;
    resetForm();
    void checkPin();
  });

  onMount(() => {
    void checkPin();
  });

  function normalizeCategory(value: string | null | undefined): string {
    const normalized = String(value || 'api').trim().toLowerCase();
    return CATEGORIES.includes(normalized) ? normalized : 'general';
  }

  function resetForm() {
    keyName = prompt?.key_name ?? '';
    secretValue = '';
    description = prompt?.description ?? '';
    category = normalizeCategory(prompt?.category);
    pinInput = '';
    showSecret = false;
  }

  async function checkPin() {
    checkingPin = true;
    try {
      const status = await pinStatus();
      hasPin = Boolean(status.has_pin);
      vaultLocked = hasPin && !vaultToken;
    } catch {
      hasPin = false;
      vaultLocked = false;
    } finally {
      checkingPin = false;
    }
  }

  async function unlockVault() {
    if (!pinInput.trim() || unlocking) return;
    unlocking = true;
    try {
      const unlocked = await vaultUnlock(pinInput);
      vaultToken = unlocked.token;
      vaultLocked = false;
      pinInput = '';
    } catch (err: any) {
      ui.toast(err?.detail || 'Incorrect PIN', 'error');
    } finally {
      unlocking = false;
    }
  }

  function handleVaultError(err: any, fallback: string) {
    if (err?.status === 423) {
      vaultToken = null;
      vaultLocked = true;
      ui.toast('Vault locked. Unlock to continue.', 'error');
      return;
    }
    ui.toast(err?.detail || fallback, 'error');
  }

  async function saveSecret() {
    if (!prompt || !canSave) return;
    saving = true;
    try {
      await createSecret({
        key_name: keyName.trim(),
        value: secretValue,
        description: description.trim(),
        category,
      }, vaultToken);
      secretValue = '';
      ui.toast('Secret saved to Vault', 'success');
      onSaved?.(prompt.id);
    } catch (err: any) {
      handleVaultError(err, 'Secret save failed');
    } finally {
      saving = false;
    }
  }

  function dismissPrompt() {
    if (!prompt) return;
    onDismiss?.(prompt.id);
  }
</script>

<div class="vault-prompt-panel">
  {#if prompt}
    <header class="vault-prompt-header">
      <span class="vault-prompt-icon" aria-hidden="true">
        <ConstellationIcon name="vault" size={18} stroke={1.8} />
      </span>
      <div class="vault-prompt-title">
        <p>Vault Request</p>
        <h2>{prompt.key_name}</h2>
      </div>
      <ConstellationPill variant={vaultLocked ? 'warning' : 'info'} leadingDot>
        {vaultLocked ? 'Locked' : 'Ready'}
      </ConstellationPill>
    </header>

    {#if prompt.reason}
      <section class="vault-prompt-reason">
        <span>Reason</span>
        <p>{prompt.reason}</p>
      </section>
    {/if}

    {#if vaultLocked}
      <form class="vault-prompt-unlock" onsubmit={(event) => { event.preventDefault(); void unlockVault(); }}>
        <label for="vault-prompt-pin">Vault PIN</label>
        <div class="vault-prompt-inline">
          <input
            id="vault-prompt-pin"
            type="password"
            bind:value={pinInput}
            autocomplete="current-password"
            placeholder="PIN"
            disabled={checkingPin || unlocking}
          />
          <ConstellationButton type="submit" size="sm" variant="secondary" loading={unlocking}>
            Unlock
          </ConstellationButton>
        </div>
      </form>
    {/if}

    <form class="vault-prompt-form" onsubmit={(event) => { event.preventDefault(); void saveSecret(); }}>
      <label for="vault-prompt-key">Key name</label>
      <input
        id="vault-prompt-key"
        bind:value={keyName}
        autocomplete="off"
        spellcheck="false"
        disabled={saving}
      />

      <label for="vault-prompt-value">Value</label>
      <div class="vault-prompt-secret-row">
        <input
          id="vault-prompt-value"
          type={showSecret ? 'text' : 'password'}
          bind:value={secretValue}
          autocomplete="off"
          placeholder="Secret value"
          disabled={saving || vaultLocked}
        />
        <button
          type="button"
          class="vault-prompt-show"
          aria-label={showSecret ? 'Hide secret value' : 'Show secret value'}
          title={showSecret ? 'Hide secret value' : 'Show secret value'}
          onclick={() => (showSecret = !showSecret)}
        >
          <ConstellationIcon name="preview" size={14} stroke={1.8} />
        </button>
      </div>

      <label for="vault-prompt-description">Description</label>
      <textarea
        id="vault-prompt-description"
        bind:value={description}
        rows="3"
        disabled={saving}
      ></textarea>

      <label for="vault-prompt-category">Category</label>
      <select id="vault-prompt-category" bind:value={category} disabled={saving}>
        {#each CATEGORIES as option}
          <option value={option}>{option}</option>
        {/each}
      </select>

      <div class="vault-prompt-actions">
        <ConstellationButton type="button" variant="quiet" onclick={dismissPrompt} disabled={saving}>
          Dismiss
        </ConstellationButton>
        <ConstellationButton type="submit" loading={saving} disabled={!canSave}>
          Save
        </ConstellationButton>
      </div>
    </form>
  {:else}
    <div class="vault-prompt-empty">
      <ConstellationIcon name="vault" size={22} stroke={1.7} />
      <p>No active Vault request.</p>
    </div>
  {/if}
</div>

<style>
  .vault-prompt-panel {
    width: 100%;
    min-width: 0;
    display: grid;
    gap: 16px;
    padding: 4px 2px 10px;
    color: var(--constellation-color-text-primary);
  }

  .vault-prompt-header {
    display: grid;
    grid-template-columns: 36px minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    padding: 4px 2px 14px;
    border-bottom: 1px solid var(--constellation-utility-panel-header-border);
  }

  .vault-prompt-icon {
    display: inline-flex;
    width: 34px;
    height: 34px;
    align-items: center;
    justify-content: center;
    border-radius: 10px;
    border: 1px solid color-mix(in srgb, var(--constellation-surface-panel-border) 78%, transparent);
    background: color-mix(in srgb, var(--constellation-control-pill-background) 72%, transparent);
    color: var(--constellation-control-pill-active-text);
  }

  .vault-prompt-title {
    min-width: 0;
    display: grid;
    gap: 4px;
  }

  .vault-prompt-title p,
  .vault-prompt-reason span,
  .vault-prompt-form label,
  .vault-prompt-unlock label {
    margin: 0;
    color: var(--constellation-color-text-muted);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 680;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .vault-prompt-title h2 {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--constellation-color-text-primary);
    font-size: 17px;
    font-weight: 680;
    letter-spacing: 0;
    line-height: 1.2;
  }

  .vault-prompt-reason {
    display: grid;
    gap: 7px;
    padding: 11px 12px;
    border-radius: 10px;
    border: 1px solid var(--constellation-surface-panel-border);
    background: color-mix(in srgb, var(--constellation-surface-panel-background) 74%, transparent);
  }

  .vault-prompt-reason p {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-size: 13px;
    line-height: 1.45;
  }

  .vault-prompt-unlock,
  .vault-prompt-form {
    display: grid;
    gap: 9px;
  }

  .vault-prompt-form {
    gap: 10px;
  }

  .vault-prompt-inline,
  .vault-prompt-secret-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 8px;
    align-items: center;
  }

  .vault-prompt-panel input,
  .vault-prompt-panel textarea,
  .vault-prompt-panel select {
    width: 100%;
    min-width: 0;
    border: 1px solid var(--constellation-control-field-border);
    border-radius: 10px;
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    font: inherit;
    font-size: 13px;
    letter-spacing: 0;
  }

  .vault-prompt-panel input,
  .vault-prompt-panel select {
    height: 38px;
    padding: 0 10px;
  }

  .vault-prompt-panel textarea {
    min-height: 78px;
    resize: vertical;
    padding: 9px 10px;
    line-height: 1.45;
  }

  .vault-prompt-panel input:focus,
  .vault-prompt-panel textarea:focus,
  .vault-prompt-panel select:focus {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .vault-prompt-panel input:disabled,
  .vault-prompt-panel textarea:disabled,
  .vault-prompt-panel select:disabled {
    opacity: 0.64;
  }

  .vault-prompt-show {
    display: inline-flex;
    width: 38px;
    height: 38px;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--constellation-control-field-border);
    border-radius: 10px;
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-muted);
    cursor: pointer;
  }

  .vault-prompt-show:hover {
    color: var(--constellation-color-text-primary);
  }

  .vault-prompt-show:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .vault-prompt-actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    padding-top: 6px;
  }

  .vault-prompt-empty {
    min-height: 180px;
    display: grid;
    place-items: center;
    gap: 10px;
    color: var(--constellation-color-text-muted);
    text-align: center;
  }

  .vault-prompt-empty p {
    margin: 0;
    font-size: 13px;
  }
</style>
