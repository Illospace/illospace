<script lang="ts">
  import {
    ConstellationButton,
    ConstellationNotice,
    ConstellationSegmentedToggle,
    ConstellationTextInput,
  } from '$lib/components/constellation';
  import type { ToolPolicy } from '$lib/types/toolPolicy';
  import './runtimeCards.css';

  let {
    policy,
    disabledTools,
    canManageSettings,
    loading,
    saving,
    error,
    onRetry,
    onChange,
    onSave,
  }: {
    policy: ToolPolicy | null;
    disabledTools: string[];
    canManageSettings: boolean;
    loading: boolean;
    saving: boolean;
    error: string;
    onRetry: () => void;
    onChange: (names: string[]) => void;
    onSave: () => void;
  } = $props();

  let toolName = $state('');
  const options = [{ key: 'off', label: 'Off' }, { key: 'on', label: 'On' }];
  const tools = $derived.by(() => {
    const known = policy?.tools ?? [];
    const names = new Set(known.map((tool) => tool.name));
    return [...known, ...disabledTools.filter((name) => !names.has(name)).map((name) => ({ name, description: '' }))]
      .sort((a, b) => a.name.localeCompare(b.name));
  });
  const dirty = $derived(JSON.stringify([...disabledTools].sort()) !== JSON.stringify(policy?.disabled_tools ?? []));

  function setAvailable(name: string, available: boolean) {
    onChange(available ? disabledTools.filter((tool) => tool !== name) : [...new Set([...disabledTools, name])].sort());
  }

  function blockByName() {
    const name = toolName.trim();
    if (!name || name.length > 200 || disabledTools.length >= 512) return;
    setAvailable(name, false);
    toolName = '';
  }
</script>

<section class="runtime-section runtime-card" aria-labelledby="tool-policy-heading" aria-busy={loading || saving}>
  <header class="runtime-section-heading">
    <div>
      <h2 id="tool-policy-heading">Tool policy</h2>
      <p>Turn off tools for every run in this workspace. This includes schedules, Cycles, inbound messages, and workers. A run cannot turn a tool back on.</p>
    </div>
  </header>

  {#if loading}
    <p>Loading tool policy...</p>
  {/if}
  {#if error}
    <ConstellationNotice title="Tool policy failed to load or save." description={error} tone="danger" compact>
      {#snippet actions()}
        <ConstellationButton variant="secondary" size="sm" onclick={onRetry}>Retry</ConstellationButton>
      {/snippet}
    </ConstellationNotice>
  {/if}
  {#if policy}
    {#if !canManageSettings}
      <p>Only an admin can change this policy.</p>
    {/if}
    {#each tools as tool (tool.name)}
      <div class="model-row">
        <div class="model-copy">
          <h3 class="runtime-card-identifier">{tool.name}</h3>
          {#if tool.description}<p>{tool.description}</p>{/if}
          {#if !policy.tools.some((known) => known.name === tool.name)}
            <p>Unknown tool. Illo saves this name and blocks the tool if it connects later.</p>
          {/if}
        </div>
        <ConstellationSegmentedToggle
          {options}
          activeKey={disabledTools.includes(tool.name) ? 'off' : 'on'}
          ariaLabel={`${tool.name} available`}
          disabled={!canManageSettings || saving || loading}
          onActiveKeyChange={(key) => setAvailable(tool.name, key === 'on')}
        />
      </div>
    {/each}
    <div class="model-row">
      <div class="model-copy">
        <label for="blocked-tool-name">Block a tool by name</label>
        <p id="blocked-tool-help">Use this for a tool that is not connected yet.</p>
      </div>
      <div class="model-copy">
        <ConstellationTextInput id="blocked-tool-name" bind:value={toolName} maxlength={200} aria-describedby="blocked-tool-help" disabled={!canManageSettings || saving || loading} />
        <ConstellationButton variant="secondary" size="sm" onclick={blockByName} disabled={!canManageSettings || saving || loading || !toolName.trim() || disabledTools.length >= 512}>Block</ConstellationButton>
      </div>
    </div>
    <ConstellationButton onclick={onSave} loading={saving} loadingLabel="Saving" disabled={!canManageSettings || loading || !dirty}>Save changes</ConstellationButton>
  {/if}
</section>
