<script lang="ts">
  import type { Snippet } from 'svelte';

  type Props = {
    eyebrow: string;
    title: string;
    description?: string;
    divider?: boolean;
    className?: string;
    actions?: Snippet;
    children?: Snippet;
  };

  let {
    eyebrow,
    title,
    description = '',
    divider = true,
    className = '',
    actions,
    children,
  }: Props = $props();

  const rootClass = $derived(
    ['constellation-section', divider ? 'has-divider' : '', className].filter(Boolean).join(' '),
  );
</script>

<section class={rootClass}>
  <div class="constellation-section-head">
    <div class="constellation-section-copy">
      <p class="constellation-section-eyebrow">{eyebrow}</p>
      <h2 class="constellation-section-title">{title}</h2>
      {#if description}
        <p class="constellation-section-description">{description}</p>
      {/if}
    </div>

    {#if actions}
      <div class="constellation-section-actions">
        {@render actions()}
      </div>
    {/if}
  </div>

  <div class="constellation-section-body">
    {@render children?.()}
  </div>
</section>

<style>
  .constellation-section {
    display: grid;
    gap: 14px;
    padding: 0;
  }

  .constellation-section.has-divider {
    padding-top: var(--constellation-section-divider-offset, 16px);
    border-top: 1px solid var(--constellation-section-divider);
  }

  .constellation-section-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
  }

  .constellation-section-copy {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  .constellation-section-eyebrow {
    margin: 0;
    color: var(--constellation-label-eyebrow);
    font-family: var(--constellation-font-mono);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .constellation-section-title {
    margin: 0;
    color: var(--constellation-section-title);
    font-family: var(--constellation-font-sans);
    font-size: 15px;
    font-weight: 560;
    line-height: 1.3;
    letter-spacing: 0;
  }

  .constellation-section-description {
    margin: 0;
    max-width: 620px;
    color: var(--constellation-section-description);
    font-size: 12px;
    line-height: 1.5;
  }

  .constellation-section-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 8px;
  }

  .constellation-section-body {
    min-width: 0;
  }

  @media (max-width: 980px) {
    .constellation-section-head {
      flex-direction: column;
    }

    .constellation-section-actions {
      justify-content: flex-start;
    }
  }
</style>
