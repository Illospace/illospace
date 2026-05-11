<script lang="ts">
  type Props = {
    className?: string;
    variant?: 'logo' | 'small' | 'icon';
    title?: string;
  };

  let { className = '', variant = 'logo', title = 'Illospace' }: Props = $props();

  const rootClass = $derived(
    ['illospace-logo', `illospace-logo-${variant}`, className].filter(Boolean).join(' '),
  );
  const darkSrc = $derived(`/brand/illo/illo-${variant}-dark.svg`);
  const lightSrc = $derived(`/brand/illo/illo-${variant}-light.svg`);
</script>

<span class={rootClass} role="img" aria-label={title}>
  <img
    class="illospace-logo-image illospace-logo-on-dark"
    src={darkSrc}
    alt=""
    aria-hidden="true"
    draggable="false"
  />
  <img
    class="illospace-logo-image illospace-logo-on-light"
    src={lightSrc}
    alt=""
    aria-hidden="true"
    draggable="false"
  />
</span>

<style>
  .illospace-logo {
    display: inline-grid;
    width: 100%;
    height: 100%;
    place-items: center;
    line-height: 0;
  }

  .illospace-logo-image {
    grid-area: 1 / 1;
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
    pointer-events: none;
    user-select: none;
  }

  .illospace-logo-on-light {
    display: none;
  }

  :global(html[data-color-scheme='light']) .illospace-logo-on-dark {
    display: none;
  }

  :global(html[data-color-scheme='light']) .illospace-logo-on-light {
    display: block;
  }
</style>
