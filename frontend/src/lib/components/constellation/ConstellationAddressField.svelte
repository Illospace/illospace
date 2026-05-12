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
    background: #57cfa0;
    box-shadow: 0 0 10px rgba(87, 207, 160, 0.48);
  }

  .constellation-address-dot.loading {
    background: #57CFA0;
    box-shadow: 0 0 10px color-mix(in srgb, var(--constellation-color-amber, #57CFA0) 44%, transparent);
  }

  .constellation-address-dot.error {
    background: #d17878;
    box-shadow: 0 0 10px rgba(209, 120, 120, 0.46);
  }
</style>
