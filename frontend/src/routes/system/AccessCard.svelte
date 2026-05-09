<script lang="ts">
  import { ConstellationButton } from '$lib/components/constellation';

  import SetupCard from './SetupCard.svelte';
  import type { PillTone } from './types';

  let {
    description,
    status,
    statusTone,
    apiKey,
    openaiEmbedderApiKey,
    geminiApiKey,
    oauthUrl,
    oauthCallback,
    oauthPending,
    oauthCallbackAvailable,
    oauthCallbackMode,
    showEmbedderKeyPrompt,
    canManageSettings,
    savingConnection,
    savingOpenAIEmbedderKey,
    savingGeminiKey,
    onApiKeyChange,
    onOpenAIEmbedderApiKeyChange,
    onGeminiApiKeyChange,
    onCallbackChange,
    onConnectWithApiKey,
    onConnectOpenAIEmbedderKey,
    onConnectWithGeminiKey,
    onStartCodexSignIn,
    onStartLocalCodexSignIn,
    onFinishCodexSignIn,
    onSkipEmbedderPrompt,
  }: {
    description: string;
    status: string;
    statusTone: PillTone;
    apiKey: string;
    openaiEmbedderApiKey: string;
    geminiApiKey: string;
    oauthUrl: string;
    oauthCallback: string;
    oauthPending: boolean;
    oauthCallbackAvailable: boolean;
    oauthCallbackMode: 'server' | 'local_bridge' | 'manual';
    showEmbedderKeyPrompt: boolean;
    canManageSettings: boolean;
    savingConnection: boolean;
    savingOpenAIEmbedderKey: boolean;
    savingGeminiKey: boolean;
    onApiKeyChange: (value: string) => void;
    onOpenAIEmbedderApiKeyChange: (value: string) => void;
    onGeminiApiKeyChange: (value: string) => void;
    onCallbackChange: (value: string) => void;
    onConnectWithApiKey: () => void;
    onConnectOpenAIEmbedderKey: () => void;
    onConnectWithGeminiKey: () => void;
    onStartCodexSignIn: () => void;
    onStartLocalCodexSignIn: () => void;
    onFinishCodexSignIn: () => void;
    onSkipEmbedderPrompt: () => void;
  } = $props();

  const showManualCallback = $derived(Boolean(oauthPending));
</script>

<SetupCard
  eyebrow="Access"
  title="Connect Access"
  {description}
  {status}
  {statusTone}
>
  <div class="access-stack">
    {#if showEmbedderKeyPrompt}
      <div class="embedder-prompt">
        <div class="field-copy">
          <p>Memory & retrieval</p>
          <span>Highly recommended. Save one workspace OpenAI API key for embeddings, or skip and connect it later.</span>
        </div>
        <div class="embedder-key-row">
          <input
            id="openai-embedder-api-key"
            type="password"
            value={openaiEmbedderApiKey}
            autocomplete="off"
            placeholder="sk-..."
            oninput={(event) => onOpenAIEmbedderApiKeyChange((event.currentTarget as HTMLInputElement).value)}
          />
          <ConstellationButton
            onclick={onConnectOpenAIEmbedderKey}
            loading={savingOpenAIEmbedderKey}
            loadingLabel="Saving"
          >
            Save workspace key
          </ConstellationButton>
          <ConstellationButton variant="quiet" onclick={onSkipEmbedderPrompt}>
            Skip for now
          </ConstellationButton>
        </div>
      </div>
    {/if}

    <div class="oauth-row">
      <div>
        <p>Codex</p>
        <span>
          {#if oauthPending && oauthCallbackMode === 'server'}
            Waiting for OpenAI to return to this Illo server.
          {:else if oauthPending && oauthCallbackAvailable}
            Waiting for OpenAI to return here.
          {:else if oauthPending}
            Automatic return is unavailable. Use the fallback if needed.
          {:else}
            Use your OpenAI account session for model access.
          {/if}
        </span>
      </div>
      <ConstellationButton variant="secondary" onclick={onStartCodexSignIn} loading={savingConnection}>
        {oauthPending ? 'Open again' : 'Sign in'}
      </ConstellationButton>
    </div>

    <div class="credential-row">
      <div class="field-copy">
        <label for="openai-api-key">{canManageSettings ? 'Workspace OpenAI key' : 'OpenAI runtime key'}</label>
        <span>
          {#if canManageSettings}
            Shared workspace fallback for models and OpenAI memory.
          {:else}
            Alternative to Codex sign-in for your own model access.
          {/if}
        </span>
      </div>
      <input
        id="openai-api-key"
        type="password"
        value={apiKey}
        autocomplete="off"
        placeholder="sk-..."
        oninput={(event) => onApiKeyChange((event.currentTarget as HTMLInputElement).value)}
      />
      <ConstellationButton onclick={onConnectWithApiKey} loading={savingConnection} loadingLabel="Connecting">
        Connect key
      </ConstellationButton>
    </div>

    <div class="credential-row">
      <div class="field-copy">
        <label for="gemini-api-key">Gemini key</label>
        <span>Required only when Gemini workspace memory is selected.</span>
      </div>
      <input
        id="gemini-api-key"
        type="password"
        value={geminiApiKey}
        disabled={!canManageSettings}
        autocomplete="off"
        placeholder="Google AI Studio key"
        oninput={(event) => onGeminiApiKeyChange((event.currentTarget as HTMLInputElement).value)}
      />
      <ConstellationButton
        onclick={onConnectWithGeminiKey}
        loading={savingGeminiKey}
        loadingLabel="Connecting"
        disabled={!canManageSettings}
      >
        Connect key
      </ConstellationButton>
    </div>

    {#if showManualCallback}
      {#if oauthUrl}
        <a class="oauth-sign-in-link" href={oauthUrl} target="_blank" rel="noreferrer">
          Open OpenAI sign-in
        </a>
      {/if}
      <div class="callback-row">
        <label for="openai-callback">Callback URL</label>
        <input
          id="openai-callback"
          value={oauthCallback}
          autocomplete="off"
          placeholder="http://localhost:1455/auth/callback?code=..."
          oninput={(event) => onCallbackChange((event.currentTarget as HTMLInputElement).value)}
        />
        <ConstellationButton variant="quiet" onclick={onFinishCodexSignIn} loading={savingConnection}>
          Finish
        </ConstellationButton>
      </div>
      {#if oauthPending && oauthCallbackMode === 'server'}
        <ConstellationButton variant="quiet" onclick={onStartLocalCodexSignIn} loading={savingConnection}>
          Use localhost fallback
        </ConstellationButton>
      {/if}
    {/if}
  </div>
</SetupCard>

<style>
  .access-stack {
    display: grid;
    gap: 18px;
  }

  .embedder-prompt {
    display: grid;
    gap: 12px;
    padding: 14px;
    border: 1px solid var(--constellation-control-pill-info-border);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-control-pill-info-background) 72%, transparent);
  }

  .embedder-key-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    gap: 12px;
    align-items: center;
  }

  .credential-row,
  .callback-row {
    display: grid;
    grid-template-columns: minmax(92px, 128px) minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
  }

  .oauth-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    min-height: 60px;
    padding: 12px;
    border: 1px solid var(--constellation-surface-panel-separator);
    border-radius: 8px;
    background: color-mix(in srgb, var(--constellation-surface-panel-background) 74%, transparent);
  }

  .oauth-row div {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .field-copy {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .oauth-row p,
  .field-copy p,
  .field-copy label,
  .callback-row label {
    margin: 0;
    min-width: 0;
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .oauth-row span {
    margin: 0;
    color: var(--constellation-text-muted);
    font-size: var(--constellation-type-body-sm);
  }

  .field-copy span {
    color: var(--constellation-text-muted);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.35;
  }

  .oauth-sign-in-link {
    display: inline-flex;
    justify-self: start;
    align-items: center;
    justify-content: center;
    min-height: 36px;
    padding: 0 12px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    border-radius: 8px;
    background: var(--constellation-button-secondary-background);
    color: var(--constellation-control-button-secondary-text);
    font-size: var(--constellation-type-body-sm);
    font-weight: 650;
    text-decoration: none;
  }

  .oauth-sign-in-link:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  input {
    width: 100%;
    min-width: 0;
    height: 42px;
    box-sizing: border-box;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 8px;
    background: var(--constellation-control-input-background);
    color: var(--constellation-text-primary);
    font: inherit;
    padding: 0 12px;
  }

  input:focus {
    outline: 2px solid rgba(141, 183, 255, 0.48);
    outline-offset: 2px;
  }

  input:disabled {
    cursor: not-allowed;
    opacity: 0.56;
  }

  input::placeholder {
    color: color-mix(in srgb, var(--constellation-text-muted) 68%, transparent);
  }

  @media (max-width: 980px) {
    .embedder-key-row,
    .credential-row,
    .callback-row {
      grid-template-columns: 1fr;
    }
  }
</style>
