<script lang="ts">
  import type { Snippet } from 'svelte';

  type Props = {
    className?: string;
    children?: Snippet;
  };

  let { className = '', children }: Props = $props();

  const rootClass = $derived(
    ['constellation-split-layout', className].filter(Boolean).join(' '),
  );
</script>

<div class={rootClass}>
  {@render children?.()}
</div>

<style>
  .constellation-split-layout {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 22px;
    align-items: start;
    align-content: start;
  }

  @media (max-width: 1180px) {
    .constellation-split-layout {
      grid-template-columns: 1fr;
    }
  }
</style>
