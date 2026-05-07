<script lang="ts">
  let {
    label,
    code,
    notes = [],
    className = '',
  }: {
    label: string;
    code: string;
    notes?: string[];
    className?: string;
  } = $props();

  const rootClass = $derived(['constellation-snippet-block', className].filter(Boolean).join(' '));
</script>

<div class={rootClass}>
  <span class="constellation-snippet-block-label">{label}</span>
  <pre class="constellation-snippet-block-code">{code}</pre>

  {#if notes.length}
    <ul class="constellation-snippet-block-notes">
      {#each notes as note}
        <li>{note}</li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .constellation-snippet-block {
    display: grid;
    gap: 10px;
  }

  .constellation-snippet-block-label {
    color: var(--constellation-label-eyebrow);
    font-family: var(--font-mono-direction);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-snippet-block-code {
    margin: 0;
    padding: 12px 0 0;
    border-top: 1px solid var(--constellation-surface-panel-separator);
    color: var(--constellation-color-text-primary);
    font-family: var(--font-mono-direction);
    font-size: 12px;
    line-height: 1.7;
    white-space: pre-wrap;
  }

  .constellation-snippet-block-notes {
    display: grid;
    gap: 8px;
    margin: 0;
    padding-left: 18px;
    color: var(--constellation-color-text-secondary);
    font-size: 12px;
    line-height: 1.6;
  }
</style>
