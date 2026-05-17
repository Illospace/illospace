<script lang="ts">
  import type { HTMLInputAttributes } from 'svelte/elements';

  import ConstellationTextInput from './ConstellationTextInput.svelte';

  export type ConstellationAddressFieldStatus = 'connected' | 'loading' | 'error';

  type Props = Omit<HTMLInputAttributes, 'size' | 'class'> & {
    className?: string;
    status?: ConstellationAddressFieldStatus;
    value?: string;
  };

  let {
    className = '',
    status = 'connected',
    value = $bindable(''),
    ...rest
  }: Props = $props();
</script>

<ConstellationTextInput bind:value mono className={className} {...rest}>
  {#snippet leadingVisual()}
    <span class={`constellation-address-dot ${status}`}></span>
  {/snippet}
</ConstellationTextInput>

<style>
  .constellation-address-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
  }

  .constellation-address-dot.connected {
    background: var(--constellation-color-success);
    box-shadow: 0 0 10px color-mix(in srgb, var(--constellation-color-success) 48%, transparent);
  }

  .constellation-address-dot.loading {
    background: var(--constellation-color-amber);
    box-shadow: 0 0 10px color-mix(in srgb, var(--constellation-color-amber) 44%, transparent);
  }

  .constellation-address-dot.error {
    background: var(--constellation-color-danger);
    box-shadow: 0 0 10px color-mix(in srgb, var(--constellation-color-danger) 46%, transparent);
  }
</style>
