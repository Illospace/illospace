<script lang="ts">
  import { ConstellationButton } from '$lib/components/constellation';

  import { MODEL_FIELDS } from './constants';
  import RuntimeSelect from './RuntimeSelect.svelte';
  import SetupCard from './SetupCard.svelte';
  import type { ModelTier, RuntimeOption } from './types';

  let {
    description,
    connectionStatus,
    modelDraft,
    modelOptions,
    oauthUrl,
    oauthCallback,
    oauthPending,
    oauthCallbackMode,
    canManageSettings,
    savingConnection,
    savingModels,
    onUpdateModel,
    onSaveModels,
    onCallbackChange,
    onStartCodexSignIn,
    onStartLocalCodexSignIn,
    onFinishCodexSignIn,
  }: {
    description: string;
    connectionStatus: string;
    modelDraft: Record<ModelTier, string>;
    modelOptions: RuntimeOption[];
    oauthUrl: string;
    oauthCallback: string;
    oauthPending: boolean;
    oauthCallbackAvailable: boolean;
    oauthCallbackMode: 'server' | 'local_bridge' | 'manual';
    canManageSettings: boolean;
    savingConnection: boolean;
    savingModels: boolean;
    onUpdateModel: (tier: ModelTier, value: string) => void;
    onSaveModels: () => void;
    onCallbackChange: (value: string) => void;
    onStartCodexSignIn: () => void;
    onStartLocalCodexSignIn: () => void;
    onFinishCodexSignIn: () => void;
  } = $props();

  const hasModelAccess = $derived(connectionStatus === 'connected');
</script>

<SetupCard
  eyebrow="Models"
  title="Models"
  {description}
>
  <div class="provider-list" aria-label="Model providers">
    <div class="provider-row">
      <div class="provider-mark" aria-hidden="true">
        <span>OpenAI</span>
      </div>
      <div class="provider-copy">
        <p>OpenAI</p>
        <strong>ChatGPT subscription</strong>
        <span>{hasModelAccess ? 'Connected for Composer model requests.' : 'Sign in to use your ChatGPT subscription for model requests.'}</span>
      </div>
      {#if !hasModelAccess}
        <ConstellationButton onclick={onStartCodexSignIn} loading={savingConnection}>
          {oauthPending ? 'Open again' : 'Sign in'}
        </ConstellationButton>
      {/if}
    </div>
  </div>

  {#if oauthPending && !hasModelAccess}
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
      <ConstellationButton variant="quiet" onclick={onFinishCodexSignIn} loading={savingConnection}>
        Finish
      </ConstellationButton>
      {#if oauthCallbackMode === 'server'}
        <ConstellationButton variant="quiet" onclick={onStartLocalCodexSignIn} loading={savingConnection}>
          Use localhost fallback
        </ConstellationButton>
      {/if}
    </div>
  {/if}

  <div class="model-routing-meta">
    <span>Composer Intelligence</span>
  </div>

  <div class="model-grid">
    {#each MODEL_FIELDS as field}
      <div class="tier-field">
        <RuntimeSelect
          id={`model-${field.key}`}
          label={field.label}
          value={modelDraft[field.key]}
          options={modelOptions}
          disabled={!canManageSettings}
          onValueChange={(value) => onUpdateModel(field.key, value)}
        />
        <p>{field.help}</p>
      </div>
    {/each}
  </div>

  <div class="panel-actions">
    <ConstellationButton onclick={onSaveModels} loading={savingModels} disabled={!canManageSettings}>
      Save models
    </ConstellationButton>
  </div>
</SetupCard>

<style>
  .provider-list {
    display: grid;
    gap: 12px;
  }

  .provider-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 14px;
    min-width: 0;
    padding: 14px;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 14px;
    background:
      linear-gradient(135deg, color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 8%, transparent), transparent 44%),
      color-mix(in srgb, var(--constellation-control-input-background) 88%, transparent);
  }

  .provider-mark {
    display: inline-flex;
    width: 56px;
    height: 56px;
    align-items: center;
    justify-content: center;
    border: 1px solid color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 28%, var(--constellation-control-input-border));
    border-radius: 14px;
    background:
      radial-gradient(circle at 28% 22%, color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 18%, transparent), transparent 48%),
      var(--constellation-surface-panel-background);
    color: var(--constellation-color-text-primary);
    box-shadow: 0 14px 34px color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 10%, transparent);
  }

  .provider-mark span {
    font-family: var(--constellation-font-mono);
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .provider-copy {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .provider-copy p,
  .model-routing-meta span,
  .oauth-callback-row label {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .provider-copy strong {
    min-width: 0;
    color: var(--constellation-color-text-primary);
    font-size: 17px;
    font-weight: 560;
    letter-spacing: 0;
    line-height: 1.15;
  }

  .provider-copy span,
  .tier-field p {
    margin: 0;
    color: var(--constellation-text-muted);
    font-size: var(--constellation-type-body-sm);
    line-height: 1.35;
  }

  .oauth-callback-row {
    display: grid;
    grid-template-columns: auto minmax(86px, 118px) minmax(0, 1fr) auto;
    gap: 12px;
    align-items: center;
  }

  .oauth-sign-in-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 36px;
    padding: 0 12px;
    border: 1px solid var(--constellation-control-button-secondary-border);
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-button-secondary-background);
    color: var(--constellation-control-button-secondary-text);
    font-size: var(--constellation-type-body-sm);
    font-weight: 650;
    text-decoration: none;
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

  input:focus,
  .oauth-sign-in-link:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .model-routing-meta {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
  }

  .model-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 18px;
  }

  .tier-field {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .panel-actions {
    display: flex;
    justify-content: flex-end;
  }

  @media (max-width: 980px) {
    .model-grid {
      grid-template-columns: 1fr;
    }

    .oauth-callback-row {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 640px) {
    .provider-row,
    .model-routing-meta {
      align-items: flex-start;
      grid-template-columns: 1fr;
    }

    .provider-mark {
      width: 48px;
      height: 48px;
    }
  }
</style>
