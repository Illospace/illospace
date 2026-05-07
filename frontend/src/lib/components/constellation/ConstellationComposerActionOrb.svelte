<script lang="ts">
  import ConstellationIcon from './ConstellationIcon.svelte';

  export type ConstellationComposerActionState = 'idle' | 'working';

  let {
    actionState = 'idle',
    label,
    className = '',
    title,
    disabled = false,
    type = 'button',
    onclick,
  }: {
    actionState?: ConstellationComposerActionState;
    label: string;
    className?: string;
    title?: string;
    disabled?: boolean;
    type?: 'button' | 'submit' | 'reset';
    onclick?: (event: MouseEvent) => void;
  } = $props();

  const isWorking = $derived(actionState === 'working');
  const rootClass = $derived(
    ['constellation-composer-action-orb', isWorking ? 'is-working' : 'is-idle', className]
      .filter(Boolean)
      .join(' '),
  );
  const resolvedTitle = $derived(title ?? label);
</script>

<button
  {type}
  class={rootClass}
  aria-label={label}
  title={resolvedTitle}
  {disabled}
  {onclick}
>
  <span class="constellation-composer-action-orb-glyph" aria-hidden="true">
    {#if isWorking}
      <ConstellationIcon name="stop" size={16} />
    {:else}
      <ConstellationIcon name="send" size={16} stroke={2} />
    {/if}
  </span>
</button>

<style>
  .constellation-composer-action-orb {
    --composer-action-size: var(
      --constellation-composer-action-button-size,
      var(--constellation-composer-orb-size, 32px)
    );

    appearance: none;
    display: inline-grid;
    place-items: center;
    flex: 0 0 auto;
    position: relative;
    width: var(--composer-action-size);
    height: var(--composer-action-size);
    min-width: var(--composer-action-size);
    min-height: var(--composer-action-size);
    max-width: var(--composer-action-size);
    max-height: var(--composer-action-size);
    aspect-ratio: 1 / 1;
    padding: 0;
    overflow: hidden;
    box-sizing: border-box;
    border-radius: var(--constellation-radius-pill);
    border: 1px solid var(--constellation-composer-action-button-border);
    background: var(--constellation-composer-action-button-background);
    color: var(--constellation-composer-action-button-text);
    box-shadow: var(--constellation-composer-action-button-shadow);
    line-height: 0;
    cursor: pointer;
    transition:
      transform var(--constellation-motion-hover-duration) ease,
      background var(--constellation-motion-hover-duration) ease,
      border-color var(--constellation-motion-hover-duration) ease,
      color var(--constellation-motion-hover-duration) ease,
      opacity var(--constellation-motion-hover-duration) ease,
      box-shadow var(--constellation-motion-hover-duration) ease;
  }

  .constellation-composer-action-orb:hover:not(:disabled) {
    transform: translateY(-1px);
    border-color: var(--constellation-composer-action-button-border-hover);
    background: var(--constellation-composer-action-button-background-hover);
    color: var(--constellation-composer-action-button-text-hover);
    box-shadow: var(--constellation-composer-action-button-shadow-hover);
  }

  .constellation-composer-action-orb:focus-visible {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-composer-action-orb:active:not(:disabled) {
    transform: translateY(1px) scale(0.98);
  }

  .constellation-composer-action-orb:disabled {
    opacity: var(--constellation-composer-orb-disabled-opacity);
    cursor: not-allowed;
    box-shadow: none;
  }

  .constellation-composer-action-orb.is-working:disabled {
    opacity: 1;
    cursor: default;
  }

  .constellation-composer-action-orb.is-working {
    border-color: var(--constellation-composer-action-button-working-border);
    background: var(--constellation-composer-action-button-working-background);
    color: var(--constellation-composer-action-button-working-text);
    box-shadow: var(--constellation-composer-action-button-working-shadow);
  }

  .constellation-composer-action-orb-glyph {
    display: inline-grid;
    place-items: center;
    width: 16px;
    height: 16px;
    pointer-events: none;
  }

  .constellation-composer-action-orb.is-idle .constellation-composer-action-orb-glyph {
    transform: translateY(-1px);
  }

  .constellation-composer-action-orb-glyph :global(svg) {
    width: 100%;
    height: 100%;
    display: block;
  }
</style>
