<script lang="ts">
  import type { Snippet } from 'svelte';

  type Props = {
    className?: string;
    primary?: Snippet;
    secondary?: Snippet;
  };

  let { className = '', primary, secondary }: Props = $props();

  const rootClass = $derived(
    ['constellation-utility-bar', className].filter(Boolean).join(' '),
  );
</script>

<div class={rootClass}>
  <div class="constellation-utility-bar-primary">
    {@render primary?.()}
  </div>

  {#if secondary}
    <div class="constellation-utility-bar-secondary">
      {@render secondary()}
    </div>
  {/if}
</div>

<style>
  .constellation-utility-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
  }

  .constellation-utility-bar-primary {
    min-width: min(100%, 260px);
    flex: 1 1 260px;
  }

  .constellation-utility-bar-secondary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
  }

  @media (max-width: 980px) {
    .constellation-utility-bar {
      flex-direction: column;
      align-items: stretch;
    }
  }
</style>
