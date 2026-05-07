<script lang="ts">
  import type { Snippet } from 'svelte';

  type Props = {
    title: string;
    text: string;
    className?: string;
    actions?: Snippet;
  };

  let { title, text, className = '', actions }: Props = $props();

  const rootClass = $derived(
    ['constellation-callout', className].filter(Boolean).join(' '),
  );
</script>

<section class={rootClass}>
  <div class="constellation-callout-copy">
    <p class="constellation-callout-title">{title}</p>
    <p class="constellation-callout-text">{text}</p>
  </div>

  {#if actions}
    <div class="constellation-callout-actions">
      {@render actions()}
    </div>
  {/if}
</section>

<style>
  .constellation-callout {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
    padding: 10px 0 0;
    border-top: 1px solid var(--constellation-callout-border);
  }

  .constellation-callout-copy {
    display: grid;
    gap: 4px;
    min-width: 0;
  }

  .constellation-callout-title {
    margin: 0;
    color: var(--constellation-callout-title);
    font-size: 12px;
    font-weight: 600;
    line-height: 1.45;
  }

  .constellation-callout-text {
    margin: 0;
    color: var(--constellation-callout-text);
    font-size: 12px;
    line-height: 1.5;
  }

  .constellation-callout-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 12px;
  }

  @media (max-width: 980px) {
    .constellation-callout {
      flex-direction: column;
    }

    .constellation-callout-actions {
      justify-content: flex-start;
    }
  }
</style>
