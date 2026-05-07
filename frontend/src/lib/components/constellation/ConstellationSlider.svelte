<script lang="ts">
  type Props = {
    value?: number;
    min?: number;
    max?: number;
    step?: number;
    className?: string;
    label?: string;
    disabled?: boolean;
    onChange?: (value: number) => void;
  };

  let {
    value = $bindable(64),
    min = 0,
    max = 100,
    step = 1,
    className = '',
    label = 'Slider',
    disabled = false,
    onChange,
  }: Props = $props();

  const rootClass = $derived(['constellation-slider', className].filter(Boolean).join(' '));
  const resolvedMax = $derived(max <= min ? min + 1 : max);
  const clampedValue = $derived(Math.min(resolvedMax, Math.max(min, value)));
  const percent = $derived(((clampedValue - min) / (resolvedMax - min)) * 100);

  function handleInput(event: Event) {
    const nextValue = Number((event.currentTarget as HTMLInputElement).value);
    value = nextValue;
    onChange?.(nextValue);
  }
</script>

<label class={rootClass} aria-label={label}>
  <span class="constellation-slider-track" aria-hidden="true">
    <span class="constellation-slider-knob" style={`left:${percent}%;`}></span>
  </span>
  <input
    type="range"
    class="constellation-slider-input"
    bind:value
    {min}
    max={resolvedMax}
    {step}
    {disabled}
    aria-label={label}
    oninput={handleInput}
  />
</label>

<style>
  .constellation-slider {
    position: relative;
    display: flex;
    width: 50px;
    align-items: center;
  }

  .constellation-slider-track {
    position: relative;
    width: 100%;
    height: 2px;
    border-radius: var(--constellation-radius-pill);
    background: var(--constellation-control-slider-track);
    pointer-events: none;
  }

  .constellation-slider-knob {
    position: absolute;
    top: 50%;
    width: 10px;
    height: 10px;
    border-radius: var(--constellation-radius-pill);
    transform: translate(-50%, -50%);
    background: var(--constellation-control-slider-knob);
    box-shadow: 0 0 12px color-mix(in srgb, var(--constellation-control-slider-knob) 45%, transparent);
  }

  .constellation-slider-input {
    position: absolute;
    inset: -8px 0;
    width: 100%;
    margin: 0;
    opacity: 0;
    cursor: pointer;
  }

  .constellation-slider-input:disabled {
    cursor: default;
  }

  .constellation-slider:has(.constellation-slider-input:focus-visible) {
    outline: 2px solid var(--constellation-control-focus-ring);
    outline-offset: 4px;
    border-radius: var(--constellation-radius-pill);
  }
</style>
