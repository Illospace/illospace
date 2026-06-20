<script lang="ts">
  import { ConstellationButton, ConstellationIcon, ConstellationTextInput } from '$lib/components/constellation';

  let {
    oauthUrl,
    oauthCallback,
    oauthPending,
    oauthCallbackMode,
    hasPersonalConnection,
    hasOrgConnection,
    canManageSettings,
    personalApiKey,
    orgApiKey,
    savingConnection,
    savingPersonalApiKey,
    savingOrgApiKey,
    onCallbackChange,
    onPersonalApiKeyChange,
    onOrgApiKeyChange,
    onStartCodexSignIn,
    onStartLocalCodexSignIn,
    onFinishCodexSignIn,
    onSavePersonalApiKey,
    onSaveOrgApiKey,
  }: {
    oauthUrl: string;
    oauthCallback: string;
    oauthPending: boolean;
    oauthCallbackMode: 'server' | 'local_bridge' | 'manual';
    hasPersonalConnection: boolean;
    hasOrgConnection: boolean;
    canManageSettings: boolean;
    personalApiKey: string;
    orgApiKey: string;
    savingConnection: boolean;
    savingPersonalApiKey: boolean;
    savingOrgApiKey: boolean;
    onCallbackChange: (value: string) => void;
    onPersonalApiKeyChange: (value: string) => void;
    onOrgApiKeyChange: (value: string) => void;
    onStartCodexSignIn: () => void;
    onStartLocalCodexSignIn: () => void;
    onFinishCodexSignIn: () => void;
    onSavePersonalApiKey: () => void;
    onSaveOrgApiKey: () => void;
  } = $props();

  const openAiActionLabel = $derived(openAiConnectionActionLabel());

  function openAiConnectionActionLabel() {
    if (hasPersonalConnection) return 'Manage';
    return oauthPending ? 'Open again' : 'Connect';
  }
</script>

<section class="runtime-section provider-connections" aria-labelledby="provider-connections-heading">
  <header class="runtime-section-heading">
    <div>
      <h2 id="provider-connections-heading">Provider connections</h2>
      <p>Connect the AI accounts and keys Illo can use.</p>
    </div>
  </header>

  <div class="provider-stack">
    <article class="provider-row">
      <span class="provider-mark" aria-hidden="true">
        <ConstellationIcon name="openai" size={24} stroke={1.55} />
      </span>

      <div class="provider-copy">
        <h3>OpenAI</h3>
        <p>ChatGPT subscription</p>
      </div>

      <ConstellationButton
        variant="secondary"
        size="sm"
        onclick={onStartCodexSignIn}
        loading={savingConnection}
        disabled={savingConnection}
      >
        {openAiActionLabel}
      </ConstellationButton>
    </article>

    <button
      type="button"
      class="connect-provider-row"
      disabled={savingConnection}
      onclick={onStartCodexSignIn}
    >
      <span class="connect-provider-icon" aria-hidden="true">
        <ConstellationIcon name="plus" size={15} />
      </span>
      <span class="connect-provider-copy">
        <strong>Connect personal account</strong>
        <span>ChatGPT / Codex sign-in</span>
      </span>
    </button>

    <article class="provider-token-row">
      <div class="provider-copy">
        <h3>Personal API key</h3>
        <p>Your model runs</p>
      </div>

      <div class="provider-token-controls">
        <span class:connected={hasPersonalConnection} class="provider-token-status">
          {hasPersonalConnection ? 'Connected' : 'Missing'}
        </span>
        <ConstellationTextInput
          id="personal-openai-api-key"
          type="password"
          placeholder="sk-..."
          value={personalApiKey}
          autocomplete="off"
          mono
          disabled={savingPersonalApiKey}
          oninput={(event) => onPersonalApiKeyChange((event.currentTarget as HTMLInputElement).value)}
        />
        <ConstellationButton
          variant="secondary"
          size="sm"
          onclick={onSavePersonalApiKey}
          loading={savingPersonalApiKey}
          disabled={!personalApiKey.trim() || savingConnection}
        >
          Save
        </ConstellationButton>
      </div>
    </article>

    {#if canManageSettings}
      <article class="provider-token-row">
        <div class="provider-copy">
          <h3>Workspace API key</h3>
          <p>Org default</p>
        </div>

        <div class="provider-token-controls">
          <span class:connected={hasOrgConnection} class="provider-token-status">
            {hasOrgConnection ? 'Connected' : 'Missing'}
          </span>
          <ConstellationTextInput
            id="workspace-openai-api-key"
            type="password"
            placeholder="sk-..."
            value={orgApiKey}
            autocomplete="off"
            mono
            disabled={savingOrgApiKey}
            oninput={(event) => onOrgApiKeyChange((event.currentTarget as HTMLInputElement).value)}
          />
          <ConstellationButton
            variant="secondary"
            size="sm"
            onclick={onSaveOrgApiKey}
            loading={savingOrgApiKey}
            disabled={!orgApiKey.trim() || savingConnection}
          >
            Rotate
          </ConstellationButton>
        </div>
      </article>
    {/if}
  </div>

  {#if oauthPending && !hasPersonalConnection}
    <div class="oauth-callback-row">
      {#if oauthUrl}
        <a class="oauth-sign-in-link" href={oauthUrl} target="_blank" rel="noreferrer">
          Open sign-in
        </a>
      {/if}
      <label for="openai-callback">Callback URL</label>
      <input
        id="openai-callback"
        value={oauthCallback}
        autocomplete="off"
        placeholder="http://localhost:1455/auth/callback?code=..."
        oninput={(event) => onCallbackChange((event.currentTarget as HTMLInputElement).value)}
      />
      <ConstellationButton variant="quiet" size="sm" onclick={onFinishCodexSignIn} loading={savingConnection}>
        Finish
      </ConstellationButton>
      {#if oauthCallbackMode === 'server'}
        <ConstellationButton variant="quiet" size="sm" onclick={onStartLocalCodexSignIn} loading={savingConnection}>
          Use localhost fallback
        </ConstellationButton>
      {/if}
    </div>
  {/if}
</section>

<style>
  .runtime-section {
    display: grid;
    gap: 18px;
    min-width: 0;
    padding: 22px 0;
    border-bottom: 1px solid var(--constellation-surface-panel-separator);
  }

  .runtime-section-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    min-width: 0;
  }

  .runtime-section-heading div {
    display: grid;
    gap: 7px;
    min-width: 0;
  }

  .runtime-section-heading h2,
  .provider-copy h3 {
    margin: 0;
    color: var(--constellation-color-text-primary);
    font-family: var(--constellation-font-sans);
    font-weight: 560;
    letter-spacing: 0;
  }

  .runtime-section-heading h2 {
    font-size: 18px;
    line-height: 1.2;
  }

  .runtime-section-heading p,
  .provider-copy p,
  .connect-provider-copy span {
    margin: 0;
    color: var(--constellation-color-text-secondary);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.45;
  }

  .provider-stack {
    display: grid;
    gap: 14px;
    min-width: 0;
  }

  .provider-row,
  .connect-provider-row,
  .provider-token-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 14px;
    min-width: 0;
    box-sizing: border-box;
  }

  .provider-row {
    padding: 2px 0;
  }

  .provider-token-row {
    grid-template-columns: minmax(140px, 0.42fr) minmax(0, 1fr);
    padding: 10px 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
  }

  .provider-mark {
    display: inline-flex;
    width: 44px;
    height: 44px;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 13px;
    background: var(--constellation-control-input-background);
    color: var(--constellation-color-text-primary);
  }

  .provider-copy {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .provider-copy h3 {
    font-size: 15px;
    line-height: 1.2;
  }

  .connect-provider-row {
    width: 100%;
    padding: 6px 0;
    border: 0;
    background: transparent;
    color: var(--constellation-color-text-primary);
    text-align: left;
    cursor: pointer;
    transition:
      background-color var(--constellation-motion-hover-duration) ease,
      opacity var(--constellation-motion-hover-duration) ease,
      transform var(--constellation-motion-hover-duration) ease;
  }

  .connect-provider-row:hover:not(:disabled) {
    opacity: 0.82;
    transform: translateY(-1px);
  }

  .connect-provider-row:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .connect-provider-row:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .connect-provider-icon {
    display: inline-flex;
    width: 34px;
    height: 34px;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 12px;
    background: var(--constellation-control-input-background);
    color: var(--constellation-color-text-secondary);
  }

  .connect-provider-copy {
    display: grid;
    gap: 3px;
    min-width: 0;
  }

  .connect-provider-copy strong {
    color: var(--constellation-color-text-primary);
    font-size: 14px;
    font-weight: 560;
    line-height: 1.2;
  }

  .provider-token-controls {
    display: grid;
    grid-template-columns: auto minmax(180px, 1fr) auto;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  .provider-token-status {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 24px;
    padding: 0 8px;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 999px;
    color: var(--constellation-color-text-secondary);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .provider-token-status.connected {
    color: var(--constellation-color-success, var(--constellation-color-text-primary));
  }

  .oauth-callback-row {
    display: grid;
    grid-template-columns: auto minmax(86px, 118px) minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
    padding-top: 2px;
  }

  .oauth-callback-row label {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .oauth-sign-in-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 32px;
    padding: 0 12px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-button-secondary-background);
    color: var(--constellation-control-button-secondary-text);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 650;
    letter-spacing: 0.1em;
    text-decoration: none;
    text-transform: uppercase;
  }

  input {
    width: 100%;
    min-width: 0;
    height: 42px;
    box-sizing: border-box;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 10px;
    background: var(--constellation-control-input-background);
    color: var(--constellation-text-primary);
    font: inherit;
    padding: 0 12px;
  }

  input:focus,
  .oauth-sign-in-link:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  @media (max-width: 760px) {
    .provider-row,
    .connect-provider-row,
    .oauth-callback-row {
      grid-template-columns: 1fr;
    }

    .provider-mark {
      width: 44px;
      height: 44px;
    }
  }

  @media (max-width: 760px) {
    .provider-token-row,
    .provider-token-controls {
      grid-template-columns: 1fr;
    }

    .provider-token-status {
      justify-self: start;
    }
  }
</style>
