<script lang="ts">
  type Props = {
    className?: string;
    variant?: 'logo' | 'small' | 'icon' | 'animated';
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
  {#if variant === 'animated'}
    <svg
      class="illospace-logo-svg"
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 430 240"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <g class="illospace-logo-letter illospace-logo-letter-i">
        <rect x="38" y="47" width="22" height="156" rx="11" fill="currentColor" />
      </g>
      <g class="illospace-logo-letter illospace-logo-letter-mid">
        <rect x="92" y="47" width="22" height="156" rx="11" fill="currentColor" />
      </g>
      <g class="illospace-logo-letter illospace-logo-letter-near">
        <rect x="146" y="47" width="22" height="156" rx="11" fill="currentColor" />
      </g>
      <g class="illospace-logo-o">
        <path
          fill="currentColor"
          fill-rule="evenodd"
          d="M 298.6 62.4 A 72 72 0 1 0 333.5 180.2 L 317.2 165.5 A 50 50 0 1 1 292.9 83.7 Z"
        />
        <circle cx="295.8" cy="73.1" r="11" fill="currentColor" />
        <circle cx="325.3" cy="172.8" r="11" fill="currentColor" />
        <path
          fill="currentColor"
          fill-rule="evenodd"
          d="M 341.1 93.8 A 72 72 0 0 1 332.7 181.1 L 316.6 166.1 A 50 50 0 0 0 322.4 105.5 Z"
        />
        <circle cx="331.7" cy="99.7" r="11" fill="currentColor" />
        <circle cx="324.6" cy="173.6" r="11" fill="currentColor" />
        <circle cx="336" cy="64" r="17" fill="currentColor" />
      </g>
    </svg>
  {:else}
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
  {/if}
</span>

<style>
  .illospace-logo {
    display: inline-grid;
    width: 100%;
    height: 100%;
    place-items: center;
    line-height: 0;
  }

  .illospace-logo-animated {
    --illospace-logo-duration: 320ms;
    --illospace-logo-ease: cubic-bezier(0.22, 1, 0.36, 1);
    display: inline-block;
    width: var(--illospace-logo-width, 24px);
    height: var(--illospace-logo-height, 24px);
    overflow: hidden;
    color: var(--illospace-logo-color, currentColor);
    transition: width var(--illospace-logo-duration) var(--illospace-logo-ease);
  }

  .illospace-logo-svg {
    display: block;
    width: calc(var(--illospace-logo-height, 24px) * 1.7916667);
    height: var(--illospace-logo-height, 24px);
    transform: translateX(var(--illospace-logo-shift, -18.25px));
    transform-origin: center;
    transition: transform var(--illospace-logo-duration) var(--illospace-logo-ease);
  }

  .illospace-logo-letter {
    opacity: var(--illospace-logo-letter-opacity, 0);
    transform: translateX(var(--illospace-logo-letter-translate, 70px))
      scaleY(var(--illospace-logo-letter-scale-y, 0.86));
    transform-box: fill-box;
    transform-origin: center;
    transition:
      opacity 160ms ease,
      transform 280ms var(--illospace-logo-ease);
  }

  .illospace-logo-letter-near {
    transition-delay: var(--illospace-logo-near-delay, 90ms);
  }

  .illospace-logo-letter-mid {
    transition-delay: var(--illospace-logo-mid-delay, 45ms);
  }

  .illospace-logo-letter-i {
    transition-delay: var(--illospace-logo-i-delay, 0ms);
  }

  .illospace-logo-o {
    transform-box: fill-box;
    transform-origin: center;
    transition: transform var(--illospace-logo-duration) var(--illospace-logo-ease);
  }

  @media (prefers-reduced-motion: reduce) {
    .illospace-logo-animated,
    .illospace-logo-svg,
    .illospace-logo-letter,
    .illospace-logo-o {
      transition: none;
    }
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
