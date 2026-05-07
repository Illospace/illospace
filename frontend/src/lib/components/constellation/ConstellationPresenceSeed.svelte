<script lang="ts">
  import type { ConstellationTone } from './constellationTypes';

  export type ConstellationPresenceSeedRole = 'user' | 'illo';
  export type ConstellationPresenceSeedSize = 'xs' | 'sm' | 'md';
  export type ConstellationPresenceSeedTreatment = 'signal' | 'plain';

  type Props = {
    label: string;
    tone?: ConstellationTone;
    role?: ConstellationPresenceSeedRole;
    size?: ConstellationPresenceSeedSize;
    treatment?: ConstellationPresenceSeedTreatment;
    className?: string;
    style?: string;
    title?: string;
  };

  let {
    label,
    tone = 'spectral',
    role = 'user',
    size = 'sm',
    treatment = 'signal',
    className = '',
    style = '',
    title,
  }: Props = $props();

  function markFor(source: string, nextRole: ConstellationPresenceSeedRole) {
    if (nextRole === 'illo') {
      return 'I';
    }

    return source.trim().charAt(0).toUpperCase() || '?';
  }

  const rootClass = $derived(
    [
      'constellation-presence-seed',
      role === 'illo' ? 'is-illo' : tone === 'amber' ? 'is-amber' : 'is-spectral',
      `constellation-presence-seed-${size}`,
      treatment === 'plain' ? 'is-plain' : 'is-signal',
      className,
    ]
      .filter(Boolean)
      .join(' '),
  );
</script>

<span
  class={rootClass}
  aria-label={title ?? label}
  style={style}
  title={title}
>
  <span class="constellation-presence-seed-halo" aria-hidden="true"></span>
  <span class="constellation-presence-seed-core">
    <span class="constellation-presence-seed-mark">{markFor(label, role)}</span>
  </span>
</span>

<style>
  .constellation-presence-seed {
    --seed-size: 22px;
    --seed-mark-size: 8px;
    --seed-halo-offset: 2.4px;
    --seed-accent: var(--constellation-color-spectral);
    --seed-core: var(--constellation-color-spectral-core);
    --seed-owner: var(--constellation-color-spectral-owner);
    position: relative;
    display: inline-grid;
    width: var(--seed-size);
    height: var(--seed-size);
    place-items: center;
    isolation: isolate;
    overflow: visible;
    flex-shrink: 0;
    vertical-align: middle;
  }

  .constellation-presence-seed::before {
    content: '';
    position: absolute;
    inset: calc(var(--seed-halo-offset) * -1);
    border-radius: 50%;
    border: 1px solid color-mix(in srgb, var(--seed-owner) 18%, transparent);
    opacity: 0.64;
    pointer-events: none;
  }

  .constellation-presence-seed-halo,
  .constellation-presence-seed-core {
    position: absolute;
    inset: 0;
    border-radius: 50%;
  }

  .constellation-presence-seed-halo {
    inset: calc(var(--seed-halo-offset) * -0.55);
    border: 1px solid color-mix(in srgb, var(--seed-accent) 18%, transparent);
    opacity: 0.84;
    pointer-events: none;
  }

  .constellation-presence-seed-core {
    display: grid;
    place-items: center;
    overflow: hidden;
    background: var(--seed-core);
    border: 1px solid color-mix(in srgb, var(--seed-owner) 26%, transparent);
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--seed-owner) 16%, transparent),
      0 0 calc(var(--seed-size) * 0.95) color-mix(in srgb, var(--seed-accent) 18%, transparent);
  }

  .constellation-presence-seed-mark {
    position: relative;
    z-index: 1;
    color: var(--seed-owner);
    font-family: var(--constellation-font-mono);
    font-size: var(--seed-mark-size);
    font-weight: 700;
    letter-spacing: 0.08em;
    line-height: 1;
    text-transform: uppercase;
  }

  .constellation-presence-seed.is-spectral {
    --seed-accent: var(--constellation-color-spectral);
    --seed-core: var(--constellation-color-spectral-core);
    --seed-owner: var(--constellation-color-spectral-owner);
  }

  .constellation-presence-seed.is-amber {
    --seed-accent: var(--constellation-color-amber);
    --seed-core: var(--constellation-color-amber-core);
    --seed-owner: var(--constellation-color-amber-owner);
  }

  .constellation-presence-seed.is-illo {
    --seed-accent: var(--constellation-presence-seed-illo-accent);
    --seed-core: var(--constellation-presence-seed-illo-core);
    --seed-owner: var(--constellation-presence-seed-illo-owner);
  }

  .constellation-presence-seed.is-illo::before {
    border-color: var(--constellation-presence-seed-illo-ring);
    opacity: 0.58;
  }

  .constellation-presence-seed.is-illo .constellation-presence-seed-halo {
    border-color: var(--constellation-presence-seed-illo-halo);
  }

  .constellation-presence-seed.is-illo .constellation-presence-seed-core {
    border-color: var(--constellation-presence-seed-illo-halo);
    box-shadow: var(--constellation-presence-seed-illo-shadow);
  }

  .constellation-presence-seed-xs {
    --seed-size: 18px;
    --seed-mark-size: 7px;
    --seed-halo-offset: 1.4px;
  }

  .constellation-presence-seed.is-plain::before,
  .constellation-presence-seed.is-plain .constellation-presence-seed-halo {
    display: none;
  }

  .constellation-presence-seed.is-plain .constellation-presence-seed-core {
    box-shadow:
      inset 0 0 0 1px color-mix(in srgb, var(--seed-owner) 18%, transparent);
  }

  .constellation-presence-seed.is-plain .constellation-presence-seed-mark {
    letter-spacing: 0;
  }

  .constellation-presence-seed-sm {
    --seed-size: 22px;
    --seed-mark-size: 8px;
    --seed-halo-offset: 2.4px;
  }

  .constellation-presence-seed-md {
    --seed-size: 28px;
    --seed-mark-size: 10px;
    --seed-halo-offset: 3px;
  }
</style>
