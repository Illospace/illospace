<script lang="ts">
  import { ConstellationSelect } from '$lib/components/constellation';
  import type { RuntimeOption } from './types';

  let {
    id,
    label,
    value,
    options,
    disabled = false,
    labelHidden = false,
    className = '',
    onValueChange,
  }: {
    id: string;
    label: string;
    value: string;
    options: RuntimeOption[];
    disabled?: boolean;
    labelHidden?: boolean;
    className?: string;
    onValueChange?: (value: string) => void;
  } = $props();

  const selectOptions = $derived(
    options.map((option) => ({
      value: option.key,
      label: option.label,
      description: option.description ?? undefined,
      disabled: option.disabled,
    })),
  );
</script>

<ConstellationSelect
  {id}
  {label}
  {labelHidden}
  {value}
  options={selectOptions}
  {disabled}
  size="md"
  {className}
  {onValueChange}
/>
