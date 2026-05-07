<script lang="ts">
  import type { Snippet } from 'svelte';

  import {
    ConstellationPanel,
    ConstellationPill,
    ConstellationSectionHeader,
  } from '$lib/components/constellation';

  type PillTone = 'muted' | 'warning' | 'success' | 'danger' | 'info';

  let {
    eyebrow,
    title,
    description,
    status = '',
    statusTone = 'muted',
    actions,
    children,
  }: {
    eyebrow: string;
    title: string;
    description: string;
    status?: string;
    statusTone?: PillTone;
    actions?: Snippet;
    children?: Snippet;
  } = $props();
</script>

{#snippet statusMeta()}
  <ConstellationPill variant={statusTone} leadingDot>{status}</ConstellationPill>
{/snippet}

<div class="setup-card-shell">
  <ConstellationPanel className="setup-card" ariaLabel={title}>
    <ConstellationSectionHeader {title} {description} {eyebrow} size="sm" meta={status ? statusMeta : undefined} {actions} />

    {@render children?.()}
  </ConstellationPanel>
</div>

<style>
  .setup-card-shell {
    display: contents;
  }

  :global(.setup-card .constellation-panel-content) {
    display: grid;
    gap: 22px;
  }
</style>
