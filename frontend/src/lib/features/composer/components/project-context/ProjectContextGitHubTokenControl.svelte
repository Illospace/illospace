<script lang="ts">
  import { ConstellationIcon } from '$lib/components/constellation';
  import type { VaultSecret } from '$lib/utils/projectContextGithub';

  let {
    vaultSecrets = [],
    vaultLocked = false,
    vaultLoading = false,
    vaultError = '',
    vaultPin = $bindable(''),
    vaultUnlocking = false,
    vaultTokenKey = $bindable(''),
    githubLoading = false,
    githubConnected = false,
    githubConnectedLogin = '',
    githubRepoCount = 0,
    authOpen = false,
    newTokenKey = $bindable('GITHUB_TOKEN'),
    newTokenValue = $bindable(''),
    tokenSaving = false,
    onOpenAuth,
    onCloseAuth,
    onRefresh,
    onConnect,
    onSelectVaultKey,
    onUnlockVault,
    onSaveToken,
    onClearVaultError,
  }: {
    vaultSecrets?: VaultSecret[];
    vaultLocked?: boolean;
    vaultLoading?: boolean;
    vaultError?: string;
    vaultPin?: string;
    vaultUnlocking?: boolean;
    vaultTokenKey?: string;
    githubLoading?: boolean;
    githubConnected?: boolean;
    githubConnectedLogin?: string;
    githubRepoCount?: number;
    authOpen?: boolean;
    newTokenKey?: string;
    newTokenValue?: string;
    tokenSaving?: boolean;
    onOpenAuth?: () => void;
    onCloseAuth?: () => void;
    onRefresh?: () => void;
    onConnect?: () => void;
    onSelectVaultKey?: (keyName: string) => void;
    onUnlockVault?: () => void;
    onSaveToken?: () => void;
    onClearVaultError?: () => void;
  } = $props();

  const ADD_TOKEN_SELECT_VALUE = '__add_github_token__';

  function handleVaultTokenChange(event: Event) {
    const select = event.currentTarget as HTMLSelectElement;
    const nextValue = select.value;
    if (nextValue === ADD_TOKEN_SELECT_VALUE) {
      onOpenAuth?.();
      select.value = vaultTokenKey;
      return;
    }
    vaultTokenKey = nextValue;
    onSelectVaultKey?.(vaultTokenKey);
  }
</script>

<div class="github-connect-row">
  {#if vaultLocked}
    <button type="button" onclick={onOpenAuth}>Unlock Vault</button>
  {:else if vaultLoading}
    <div class="project-context-muted compact">Checking Vault...</div>
  {:else}
    <select
      aria-label="GitHub Vault token"
      value={vaultTokenKey}
      disabled={githubLoading}
      onchange={handleVaultTokenChange}
    >
      {#if vaultSecrets.length === 0}
        <option value="">No GitHub token in Vault</option>
      {:else}
        <option value="">Choose Vault token</option>
        <optgroup label="Saved tokens">
          {#each vaultSecrets as secret}
            <option value={secret.key_name}>{secret.key_name}{secret.is_shared ? ' - shared' : ''}</option>
          {/each}
        </optgroup>
      {/if}
      <optgroup label="Actions">
        <option value={ADD_TOKEN_SELECT_VALUE}>Add GitHub token...</option>
      </optgroup>
    </select>
    <button type="button" onclick={onConnect} disabled={githubLoading || !vaultTokenKey}>
      {githubLoading && vaultTokenKey ? 'Connecting...' : 'Connect'}
    </button>
    <button
      class="project-context-icon-command compact"
      type="button"
      onclick={onRefresh}
      disabled={vaultLoading || githubLoading}
      aria-label="Refresh GitHub tokens"
      title="Refresh GitHub tokens"
    >
      <ConstellationIcon name="refresh" size={13} stroke={2} />
    </button>
  {/if}
</div>

{#if githubConnected}
  <p class="project-context-muted compact">
    Connected{githubConnectedLogin ? ` as @${githubConnectedLogin}` : ''}. {githubRepoCount} repos loaded.
  </p>
{/if}

{#if authOpen}
  <div class="vault-token-panel">
    {#if vaultLocked}
      <div class="github-token-row">
        <input
          aria-label="Vault PIN"
          type="password"
          placeholder="Vault PIN"
          bind:value={vaultPin}
          oninput={onClearVaultError}
          onkeydown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              onUnlockVault?.();
            }
          }}
        />
        <button type="button" onclick={onUnlockVault} disabled={vaultUnlocking || !vaultPin.trim()}>
          {vaultUnlocking ? 'Unlocking...' : 'Unlock'}
        </button>
        <button type="button" onclick={onCloseAuth}>Cancel</button>
      </div>
    {:else}
      <div class="github-save-row">
        <input
          aria-label="Vault key name"
          placeholder="GITHUB_TOKEN"
          bind:value={newTokenKey}
          oninput={onClearVaultError}
        />
        <input
          aria-label="New GitHub token"
          type="password"
          placeholder="Token value"
          bind:value={newTokenValue}
          oninput={onClearVaultError}
        />
        <button type="button" onclick={onSaveToken} disabled={tokenSaving || !newTokenValue.trim()}>
          {tokenSaving ? 'Saving...' : 'Save'}
        </button>
        <button type="button" onclick={onCloseAuth}>Cancel</button>
      </div>
    {/if}
    {#if vaultError}
      <p class="project-context-error">{vaultError}</p>
    {/if}
  </div>
{:else if vaultError}
  <p class="project-context-error">{vaultError}</p>
{/if}
