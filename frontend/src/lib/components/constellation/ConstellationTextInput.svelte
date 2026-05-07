<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { HTMLInputAttributes } from 'svelte/elements';

  type Props = Omit<HTMLInputAttributes, 'size' | 'class'> & {
    className?: string;
    leadingVisual?: Snippet;
    trailingVisual?: Snippet;
    mono?: boolean;
    value?: string;
  };

  let {
    className = '',
    leadingVisual,
    trailingVisual,
    mono = false,
    spellcheck = false,
    value = $bindable(''),
    ...rest
  }: Props = $props();

  const rootClass = $derived(
    ['constellation-text-input', mono ? 'is-mono' : '', className].filter(Boolean).join(' '),
  );
</script>

<label class={rootClass}>
  {#if leadingVisual}
    <span class="constellation-text-input-leading" aria-hidden="true">
      {@render leadingVisual()}
    </span>
  {/if}

  <input class="constellation-text-input-control" bind:value {spellcheck} {...rest} />

  {#if trailingVisual}
    <span class="constellation-text-input-trailing" aria-hidden="true">
      {@render trailingVisual()}
    </span>
  {/if}
</label>

<style>
  .constellation-text-input {
    display: flex;
    align-items: center;
    gap: 8px;
    min-height: 32px;
    min-width: 0;
    padding: 0 10px;
    border-radius: 8px;
    border: 1px solid var(--constellation-control-field-border);
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .constellation-text-input:focus-within {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-text-input.is-mono .constellation-text-input-control {
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
  }

  .constellation-text-input-leading,
  .constellation-text-input-trailing {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--constellation-color-text-muted);
    flex: 0 0 auto;
  }

  .constellation-text-input-leading :global(svg),
  .constellation-text-input-trailing :global(svg) {
    width: 14px;
    height: 14px;
  }

  .constellation-text-input-control {
    align-self: stretch;
    min-width: 0;
    width: 100%;
    border: 0;
    background: transparent;
    color: inherit;
    font-size: 13px;
    line-height: 1.2;
    letter-spacing: 0;
    outline: 0;
  }

  .constellation-text-input-control:-webkit-autofill,
  .constellation-text-input-control:-webkit-autofill:hover,
  .constellation-text-input-control:-webkit-autofill:focus,
  .constellation-text-input-control:autofill {
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: var(--constellation-color-text-primary);
    caret-color: var(--constellation-color-text-primary);
    transition: background-color 9999s ease-in-out 0s;
  }

  .constellation-text-input-control::placeholder {
    color: var(--constellation-control-field-placeholder);
  }

  .constellation-text-input-control:disabled {
    cursor: not-allowed;
  }
</style>
