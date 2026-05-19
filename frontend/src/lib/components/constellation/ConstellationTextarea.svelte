<script lang="ts">
  import type { HTMLTextareaAttributes } from 'svelte/elements';

  type Props = Omit<HTMLTextareaAttributes, 'class'> & {
    className?: string;
    value?: string;
    mono?: boolean;
  };

  let {
    className = '',
    mono = false,
    spellcheck = false,
    value = $bindable(''),
    rows = 3,
    ...rest
  }: Props = $props();

  const rootClass = $derived(
    ['constellation-textarea', mono ? 'is-mono' : '', className].filter(Boolean).join(' '),
  );
</script>

<label class={rootClass}>
  <textarea
    class="constellation-textarea-control"
    bind:value
    {rows}
    {spellcheck}
    {...rest}
  ></textarea>
</label>

<style>
  .constellation-textarea {
    display: flex;
    min-width: 0;
    min-height: 72px;
    padding: 9px 10px;
    border-radius: 8px;
    border: 1px solid var(--constellation-control-field-border);
    background: var(--constellation-control-field-background);
    color: var(--constellation-color-text-primary);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
  }

  .constellation-textarea:focus-within {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 2px;
  }

  .constellation-textarea.is-mono .constellation-textarea-control {
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
  }

  .constellation-textarea-control {
    min-width: 0;
    width: 100%;
    resize: vertical;
    border: 0;
    background: transparent;
    color: inherit;
    font-family: inherit;
    font-size: 13px;
    line-height: 1.45;
    letter-spacing: 0;
    outline: 0;
  }

  .constellation-textarea-control::placeholder {
    color: var(--constellation-control-field-placeholder);
  }

  .constellation-textarea-control:disabled {
    cursor: not-allowed;
  }
</style>
