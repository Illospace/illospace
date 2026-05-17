<script lang="ts">
  import type { Snippet } from 'svelte';

  import {
    ConstellationPanel,
    ConstellationSectionHeader,
  } from '$lib/components/constellation';

  let {
    eyebrow,
    title,
    description,
    actions,
    children,
  }: {
    eyebrow: string;
    title: string;
    description: string;
    actions?: Snippet;
    children?: Snippet;
  } = $props();
</script>

<div class="setup-card-shell">
  <ConstellationPanel className="setup-card" ariaLabel={title}>
    <ConstellationSectionHeader {title} {description} {eyebrow} size="sm" {actions} />

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
