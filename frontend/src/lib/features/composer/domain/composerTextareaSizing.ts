export interface ComposerTextareaLike {
  value: string;
  selectionStart: number | null;
  selectionEnd: number | null;
  scrollHeight: number;
  scrollTop: number;
  style: Pick<CSSStyleDeclaration, 'height'>;
}

export interface ResizeComposerTextareaOptions {
  value?: string;
  minHeight?: number;
  maxHeight: number;
  emptyHeight?: number;
}

export function shouldPinTextareaScrollToEnd(
  textarea: Pick<ComposerTextareaLike, 'selectionStart' | 'selectionEnd' | 'value'>,
  value = textarea.value,
): boolean {
  return textarea.selectionStart === value.length && textarea.selectionEnd === value.length;
}

export function resizeComposerTextareaToContent(
  textarea: ComposerTextareaLike | null | undefined,
  options: ResizeComposerTextareaOptions,
): number {
  if (!textarea) return 0;

  const value = options.value ?? textarea.value;
  const minHeight = options.minHeight ?? 0;
  const shouldPinScroll = shouldPinTextareaScrollToEnd(textarea, value);

  textarea.style.height = 'auto';

  const nextHeight =
    value.trim().length === 0 && options.emptyHeight !== undefined
      ? options.emptyHeight
      : Math.max(minHeight, Math.min(textarea.scrollHeight, options.maxHeight));

  textarea.style.height = `${nextHeight}px`;

  if (shouldPinScroll && textarea.scrollHeight > nextHeight) {
    textarea.scrollTop = textarea.scrollHeight;
  }

  return textarea.scrollTop;
}
