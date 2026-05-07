<script lang="ts">
  import ConstellationIcon, { type ConstellationIconName } from './ConstellationIcon.svelte';

  export type ConstellationGlyphLabel =
    | 'overview'
    | 'cortex'
    | 'cycles'
    | 'memory'
    | 'skills'
    | 'team'
    | 'vault'
    | 'costs'
    | 'system'
    | 'runtime'
    | 'settings'
    | 'menu'
    | string;

  let {
    label,
    className = '',
  }: {
    label: ConstellationGlyphLabel;
    className?: string;
  } = $props();

  const key = $derived(label.toLowerCase());
  const resolvedIcon = $derived.by(() => {
    const mapping: Record<string, ConstellationIconName> = {
      overview: 'overview',
      cortex: 'cortex',
      cycles: 'cycles',
      memory: 'memory',
      skills: 'skills',
      team: 'team',
      vault: 'vault',
      costs: 'costs',
      system: 'system',
      runtime: 'runtime',
      settings: 'settings',
      menu: 'menu',
    };

    return mapping[key] ?? null;
  });
</script>

<span class={['constellation-glyph-icon', className].filter(Boolean).join(' ')}>
  {#if resolvedIcon}
    <ConstellationIcon name={resolvedIcon} size={16} stroke={1.8} />
  {:else}
    {label}
  {/if}
</span>

<style>
  .constellation-glyph-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 100%;
  }

  .constellation-glyph-icon :global(svg) {
    width: 16px;
    height: 16px;
  }
</style>
