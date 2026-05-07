<script lang="ts">
  export interface RuntimeOption {
    key: string;
    label: string;
    description?: string | null;
    disabled?: boolean;
  }

  let {
    id,
    label,
    value,
    options,
    disabled = false,
    onValueChange,
  }: {
    id: string;
    label: string;
    value: string;
    options: RuntimeOption[];
    disabled?: boolean;
    onValueChange?: (value: string) => void;
  } = $props();

  function handleChange(event: Event) {
    onValueChange?.((event.currentTarget as HTMLSelectElement).value);
  }
</script>

<label class="runtime-field" for={id}>
  <span>{label}</span>
  <select {id} {value} {disabled} onchange={handleChange}>
    {#each options as option}
      <option value={option.key} disabled={option.disabled}>{option.label}</option>
    {/each}
  </select>
</label>

<style>
  .runtime-field {
    display: grid;
    gap: 7px;
    min-width: 0;
    color: var(--constellation-text-muted);
  }

  .runtime-field span {
    min-width: 0;
    font-family: var(--constellation-font-mono);
    font-size: var(--constellation-type-meta);
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  select {
    width: 100%;
    min-width: 0;
    height: 42px;
    box-sizing: border-box;
    border: 1px solid var(--constellation-control-input-border);
    border-radius: 8px;
    background: var(--constellation-control-input-background);
    color: var(--constellation-text-primary);
    font: inherit;
    padding: 0 12px;
  }

  select:focus {
    outline: 2px solid rgba(141, 183, 255, 0.48);
    outline-offset: 2px;
  }

  select:disabled {
    opacity: 0.56;
    cursor: not-allowed;
  }
</style>
