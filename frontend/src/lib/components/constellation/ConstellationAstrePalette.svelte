<script lang="ts">
  import Astre from './Astre.svelte';
  import type { ConstellationScale, ConstellationTone } from './constellationTypes';

  export type ConstellationAstrePaletteItem = {
    id: string;
    label: string;
    tone?: ConstellationTone;
    astreStyle?: string;
    disabled?: boolean;
    title?: string;
  };

  let {
    items,
    value,
    onValueChange,
    columns = 'auto',
    ariaLabel = 'Astre palette',
    className = '',
    previewLetter = '',
    previewOwner = '',
    astreScale = 'compact',
    astreArchivedCount = 0,
    swatchSize = 72,
  }: {
    items: ConstellationAstrePaletteItem[];
    value?: string;
    onValueChange?: (value: string) => void;
    columns?: number | 'auto';
    ariaLabel?: string;
    className?: string;
    previewLetter?: string;
    previewOwner?: string;
    astreScale?: ConstellationScale;
    astreArchivedCount?: number;
    swatchSize?: number;
  } = $props();

  const rootClass = $derived(['constellation-astre-palette', className].filter(Boolean).join(' '));
  const rootStyle = $derived(
    [
      typeof columns === 'number'
        ? `--astre-palette-template: repeat(${Math.max(1, columns)}, minmax(0, 1fr))`
        : null,
      `--astre-palette-swatch-size: ${Math.max(72, swatchSize)}px`,
      '--astre-palette-frame-inset: clamp(14px, calc(var(--astre-palette-swatch-size) * 0.13), 18px)',
    ]
      .filter(Boolean)
      .join('; '),
  );
</script>

<div
  role="radiogroup"
  aria-label={ariaLabel}
  data-astre-tone-control="true"
  class={rootClass}
  style={rootStyle}
>
  {#each items as item}
    {@const selected = item.id === value}
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-label={item.title ?? item.label}
      disabled={item.disabled}
      title={item.title ?? item.label}
      data-astre-tone-control="true"
      class={[
        'constellation-astre-palette-swatch',
        selected ? 'is-selected' : '',
        item.disabled ? 'is-disabled' : '',
      ]
        .filter(Boolean)
        .join(' ')}
      onclick={() => onValueChange?.(item.id)}
    >
      <span class="constellation-astre-palette-swatch-frame">
        <Astre
          letter={previewLetter}
          owner={previewOwner || item.label}
          tone={item.tone ?? 'spectral'}
          scale={astreScale}
          archivedCount={astreArchivedCount}
          animated={false}
          className="constellation-astre-palette-astre"
          style={`left: 50%; top: 50%; width: 100%; height: 100%; ${item.astreStyle ?? ''}`}
        />
      </span>
    </button>
  {/each}
</div>

<style>
  .constellation-astre-palette {
    display: grid;
    grid-template-columns: var(--astre-palette-template, repeat(auto-fit, minmax(var(--astre-palette-swatch-size), 1fr)));
    gap: 16px;
    width: 100%;
    align-items: start;
  }

  .constellation-astre-palette-swatch {
    position: relative;
    justify-self: center;
    width: min(100%, var(--astre-palette-swatch-size));
    aspect-ratio: 1 / 1;
    padding: 0;
    border: 0;
    border-radius: 20px;
    background: transparent;
    cursor: pointer;
    isolation: isolate;
    transition:
      transform var(--constellation-motion-hover-duration) ease,
      opacity var(--constellation-motion-hover-duration) ease;
  }

  .constellation-astre-palette-swatch::after {
    content: '';
    position: absolute;
    inset: 3px;
    border-radius: 18px;
    border: 1px solid transparent;
    transition:
      border-color var(--constellation-motion-hover-duration) ease,
      box-shadow var(--constellation-motion-hover-duration) ease,
      background-color var(--constellation-motion-hover-duration) ease;
    pointer-events: none;
    z-index: 0;
  }

  .constellation-astre-palette-swatch:hover {
    transform: translateY(-1px);
  }

  .constellation-astre-palette-swatch:hover::after,
  .constellation-astre-palette-swatch:focus-visible::after {
    border-color: color-mix(
      in srgb,
      var(--constellation-color-text-tertiary) 22%,
      var(--constellation-surface-nested-border)
    );
    background: color-mix(
      in srgb,
      var(--constellation-surface-nested-background) 88%,
      transparent
    );
  }

  .constellation-astre-palette-swatch:focus-visible {
    outline: none;
  }

  .constellation-astre-palette-swatch.is-selected::after {
    border-color: color-mix(
      in srgb,
      var(--constellation-color-text-secondary) 28%,
      var(--constellation-surface-nested-border)
    );
    box-shadow:
      var(--constellation-surface-nested-shadow),
      0 0 20px color-mix(in srgb, var(--constellation-color-text-tertiary) 8%, transparent);
    background: color-mix(
      in srgb,
      var(--constellation-surface-nested-strong-background) 92%,
      transparent
    );
  }

  .constellation-astre-palette-swatch.is-disabled {
    cursor: not-allowed;
    opacity: 0.42;
  }

  .constellation-astre-palette-swatch.is-disabled:hover {
    transform: none;
  }

  .constellation-astre-palette-swatch.is-disabled::before {
    content: '';
    position: absolute;
    inset: 18px;
    border-radius: 999px;
    background: linear-gradient(
      135deg,
      transparent 46%,
      color-mix(in srgb, var(--constellation-color-text-tertiary) 52%, transparent) 48%,
      transparent 52%
    );
    pointer-events: none;
    z-index: 2;
  }

  .constellation-astre-palette-swatch.is-disabled:hover::after {
    border-color: transparent;
    background: transparent;
  }

  .constellation-astre-palette-swatch-frame {
    position: absolute;
    inset: var(--astre-palette-frame-inset);
    z-index: 1;
  }

  .constellation-astre-palette-astre {
    pointer-events: none;
    filter: saturate(1.14) brightness(1.12);
    transition: filter var(--constellation-motion-hover-duration) ease;
  }

  .constellation-astre-palette-swatch:hover :global(.constellation-astre-palette-astre),
  .constellation-astre-palette-swatch:focus-visible :global(.constellation-astre-palette-astre),
  .constellation-astre-palette-swatch.is-selected :global(.constellation-astre-palette-astre) {
    filter: saturate(1.2) brightness(1.18);
  }

  .constellation-astre-palette-swatch.is-disabled :global(.constellation-astre-palette-astre) {
    filter: saturate(0.9) brightness(0.92);
  }
</style>
