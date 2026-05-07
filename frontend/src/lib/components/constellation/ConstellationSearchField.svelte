<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  import ConstellationTextInput from './ConstellationTextInput.svelte';

  type Props = Omit<HTMLInputAttributes, 'size' | 'class'> & {
    className?: string;
    placeholder?: string;
    value?: string;
  };

  let {
    className = '',
    placeholder = 'Search ideas...',
    value = $bindable(''),
    ...rest
  }: Props = $props();
</script>

<ConstellationTextInput bind:value mono {placeholder} className={`constellation-search-field ${className}`} {...rest} />

<style>
  :global(.constellation-search-field) {
    min-width: 136px;
  }
</style>
