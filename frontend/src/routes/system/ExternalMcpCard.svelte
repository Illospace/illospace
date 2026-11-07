<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import {
    ConstellationButton, ConstellationEmptyState, ConstellationNotice,
    ConstellationPill, ConstellationTextarea, ConstellationTextInput,
  } from '$lib/components/constellation';
  import RuntimeSelect from './RuntimeSelect.svelte';
  import type { ExternalMcpConfig, ExternalMcpServer, ExternalMcpTestResult } from './types';
  import './runtime-card.css';

  let { canManageSettings }: { canManageSettings: boolean } = $props();
  type Row = { id?: number; config: ExternalMcpConfig; allowedText: string; missingKeys: string[] };
  let rows = $state<Row[]>([]);
  let draft = $state<Row | null>(null);
  let loaded = $state(false);
  let busy = $state(false);
  let error = $state('');
  let tests = $state<Record<number, ExternalMcpTestResult>>({});
  const authOptions = [
    { key: 'none', label: 'None' },
    { key: 'bearer', label: 'Bearer token' },
    { key: 'client_credentials', label: 'OAuth client credentials' },
  ];
  type TextKey = 'name' | 'url' | 'bearer_token_key' | 'token_url' | 'client_id_key' | 'client_secret_key' | 'scope' | 'resource';
  const fields: { key: TextKey; label: string; mode?: string }[] = [
    { key: 'name', label: 'Name' }, { key: 'url', label: 'URL' },
    { key: 'bearer_token_key', label: 'Token vault key', mode: 'bearer' },
    { key: 'token_url', label: 'Token URL', mode: 'client_credentials' },
    { key: 'client_id_key', label: 'Client ID vault key', mode: 'client_credentials' },
    { key: 'client_secret_key', label: 'Client secret vault key', mode: 'client_credentials' },
    { key: 'scope', label: 'Scope (optional)', mode: 'client_credentials' },
    { key: 'resource', label: 'Resource (optional)', mode: 'client_credentials' },
  ];

  function row(server: ExternalMcpServer): Row {
    const { id, created_at, updated_at, missing_secret_keys, ...config } = server;
    return { id, config, allowedText: config.allowed_tools.join('\n'), missingKeys: missing_secret_keys };
  }
  function addServer() {
    draft = { config: { name: '', url: '', transport: 'streamable_http', auth_mode: 'none',
      bearer_token_key: null, token_url: null, client_id_key: null, client_secret_key: null,
      scope: null, resource: null, allowed_tools: [], enabled: true, timeout_seconds: 30 },
      allowedText: '', missingKeys: [] };
  }
  function errorMessage(value: unknown): string {
    const detail = (value as { detail?: unknown })?.detail;
    return typeof detail === 'string' ? detail : 'The request failed.';
  }
  async function load() {
    try {
      rows = (await api.externalMcpServers()).map(row);
      loaded = true;
    } catch (value) { error = errorMessage(value); }
  }
  onMount(() => { void load(); });

  async function save(value: Row) {
    if (!canManageSettings || busy) return;
    busy = true;
    error = '';
    const config = { ...value.config, allowed_tools: value.allowedText.split('\n').map(name => name.trim()).filter(Boolean) };
    try {
      if (value.id !== undefined) await api.updateExternalMcpServer(value.id, config);
      else await api.createExternalMcpServer(config);
      draft = null;
      tests = {};
      await load();
    } catch (value) { error = errorMessage(value); }
    finally { busy = false; }
  }
  async function remove(value: Row) {
    if (!canManageSettings || busy || value.id === undefined) return;
    if (!window.confirm(`Delete ${value.config.name}? Runs stop using its tools.`)) return;
    busy = true;
    error = '';
    try { await api.deleteExternalMcpServer(value.id); await load(); }
    catch (value) { error = errorMessage(value); }
    finally { busy = false; }
  }
  async function test(value: Row) {
    if (!canManageSettings || busy || value.id === undefined) return;
    busy = true;
    error = '';
    try { tests[value.id] = await api.testExternalMcpServer(value.id); }
    catch (failure) { tests[value.id] = { ok: false, tools: [], error: errorMessage(failure) }; }
    finally { busy = false; }
  }
</script>

{#snippet editor(value: Row)}
  {@const result = value.id === undefined ? undefined : tests[value.id]}
  <div class="runtime-stack">
    <div class="runtime-flow-grid">
      {#each fields as field (field.key)}
        {#if !field.mode || field.mode === value.config.auth_mode}
          <div class="runtime-static-field">
            <span id={`mcp-${value.id ?? 'new'}-${field.key}-label`}>{field.label}</span>
            <ConstellationTextInput
              id={`mcp-${value.id ?? 'new'}-${field.key}`}
              aria-labelledby={`mcp-${value.id ?? 'new'}-${field.key}-label`}
              value={value.config[field.key] ?? ''}
              disabled={!canManageSettings || busy}
              oninput={(event) => { value.config[field.key] = event.currentTarget.value; }}
            />
            {#if field.key === 'name'}
              <p>Lowercase letters, numbers, and _. Tools appear as mcp__&lt;name&gt;__&lt;tool&gt;.</p>
            {/if}
          </div>
        {/if}
      {/each}
      <div class="runtime-static-field">
        <span id={`mcp-${value.id ?? 'new'}-timeout-label`}>Timeout (seconds)</span>
        <ConstellationTextInput
          type="number" min="1" max="120"
          aria-labelledby={`mcp-${value.id ?? 'new'}-timeout-label`}
          value={String(value.config.timeout_seconds)} disabled={!canManageSettings || busy}
          oninput={(event) => { value.config.timeout_seconds = Number(event.currentTarget.value); }}
        />
      </div>
      <RuntimeSelect
        id={`mcp-${value.id ?? 'new'}-auth`} label="Authentication"
        value={value.config.auth_mode} options={authOptions} disabled={!canManageSettings || busy}
        onValueChange={(mode) => { value.config.auth_mode = mode as ExternalMcpConfig['auth_mode']; }}
      />
      <div class="runtime-static-field">
        <span id={`mcp-${value.id ?? 'new'}-allowed-label`}>Allowed tools</span>
        <ConstellationTextarea aria-labelledby={`mcp-${value.id ?? 'new'}-allowed-label`}
          bind:value={value.allowedText} disabled={!canManageSettings || busy} mono />
        <p>One tool name per line. An empty list allows no tools.</p>
      </div>
      <RuntimeSelect id={`mcp-${value.id ?? 'new'}-enabled`} label="Enabled"
        value={String(value.config.enabled)} options={[{ key: 'true', label: 'Enabled' }, { key: 'false', label: 'Disabled' }]}
        disabled={!canManageSettings || busy} onValueChange={(enabled) => { value.config.enabled = enabled === 'true'; }} />
    </div>
    {#each value.missingKeys as key (key)}
      <ConstellationNotice title={`Missing vault key: ${key}`} tone="warning" compact />
    {/each}
    {#if canManageSettings}
      <div class="runtime-section-heading">
        <ConstellationButton size="sm" disabled={busy} onclick={() => save(value)}>Save</ConstellationButton>
        <ConstellationButton variant="quiet" size="sm" disabled={busy} onclick={() => { draft = null; void load(); }}>Cancel</ConstellationButton>
        {#if value.id !== undefined}
          <ConstellationButton variant="destructive" size="sm" disabled={busy} onclick={() => remove(value)}>Delete</ConstellationButton>
          <ConstellationButton variant="secondary" size="sm" disabled={busy} onclick={() => test(value)}>Test connection</ConstellationButton>
        {/if}
      </div>
    {/if}
    {#if result}
      {#if result.ok}
        <ConstellationNotice title={`Connected. ${result.tools.length} tools found, ${result.tools.filter(tool => tool.allowed).length} allowed.`} tone="success" compact />
        <ul>
          {#each result.tools as tool}
            <li>{tool.name} <ConstellationPill variant={tool.allowed ? 'success' : 'muted'}>{tool.allowed ? 'Allowed' : 'Not allowed'}</ConstellationPill>
              <p>{tool.description}</p>
            </li>
          {/each}
        </ul>
      {:else}
        <ConstellationNotice title="Connection failed." description={result.error ?? ''} tone="danger" compact />
      {/if}
    {/if}
  </div>
{/snippet}

<section class="runtime-section" aria-labelledby="external-mcp-heading">
  <header class="runtime-section-heading">
    <div>
      <h2 id="external-mcp-heading">MCP servers</h2>
      <p>Connect your organization's MCP servers. Illo can use only the tools that you allow.</p>
    </div>
    {#if canManageSettings}
      <ConstellationButton variant="secondary" size="sm" onclick={addServer} disabled={busy || draft !== null}>Add server</ConstellationButton>
    {/if}
  </header>
  {#if !canManageSettings}<ConstellationNotice title="Only an admin can change MCP servers." compact />{/if}
  {#if error}<ConstellationNotice title={error} tone="danger" compact />{/if}
  {#if loaded && rows.length === 0 && !draft}
    <ConstellationEmptyState title="No MCP servers yet." size="sm" surface="plain" />
  {/if}
  {#each rows as value (value.id)}
    <div class="runtime-stack">
      <header class="runtime-section-heading">
        <div><h3>{value.config.name}</h3></div>
        <ConstellationPill variant={value.config.enabled ? 'success' : 'muted'}>{value.config.enabled ? 'Enabled' : 'Disabled'}</ConstellationPill>
      </header>
      {@render editor(value)}
    </div>
  {/each}
  {#if draft}{@render editor(draft)}{/if}
</section>
