type SelectKeyboardOption = {
  value: string;
  disabled?: boolean;
};

export function enabledOptionIndexes(
  options: ReadonlyArray<SelectKeyboardOption>,
): number[] {
  return options.flatMap((option, index) => option.disabled ? [] : [index]);
}

export function initialOptionIndex(
  options: ReadonlyArray<SelectKeyboardOption>,
  value: string,
): number {
  const selectedIndex = options.findIndex(
    (option) => option.value === value && !option.disabled,
  );
  return selectedIndex >= 0 ? selectedIndex : (enabledOptionIndexes(options)[0] ?? -1);
}

export function nextOptionIndex(
  options: ReadonlyArray<SelectKeyboardOption>,
  currentIndex: number,
  direction: -1 | 1,
): number {
  const indexes = enabledOptionIndexes(options);
  if (!indexes.length) return -1;
  const position = indexes.indexOf(currentIndex);
  const nextPosition = position < 0
    ? 0
    : (position + direction + indexes.length) % indexes.length;
  return indexes[nextPosition];
}

export function edgeOptionIndex(
  options: ReadonlyArray<SelectKeyboardOption>,
  edge: 'first' | 'last',
): number {
  const indexes = enabledOptionIndexes(options);
  return (edge === 'first' ? indexes[0] : indexes[indexes.length - 1]) ?? -1;
}
