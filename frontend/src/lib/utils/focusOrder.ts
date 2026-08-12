const FOCUSABLE_SELECTOR = [
  'a[href]',
  'area[href]',
  'button',
  'input',
  'select',
  'textarea',
  'summary',
  'iframe',
  'object',
  'embed',
  'audio[controls]',
  'video[controls]',
  '[contenteditable]:not([contenteditable="false"])',
  '[tabindex]',
].join(',');

type FocusableElementsOptions = {
  document?: Document;
  exclude?: Element | null;
};

function isExcluded(element: HTMLElement, exclude?: Element | null): boolean {
  return exclude !== null
    && exclude !== undefined
    && (element === exclude || exclude.contains(element));
}

function isAvailableForFocus(element: HTMLElement, exclude?: Element | null): boolean {
  return !isExcluded(element, exclude)
    && element.tabIndex >= 0
    && !element.matches(':disabled, [disabled]')
    && !element.closest('[inert], [hidden]')
    && element.getClientRects().length > 0;
}

export function focusableElementsInDocument(
  options: FocusableElementsOptions = {},
): HTMLElement[] {
  const sourceDocument = options.document
    ?? (typeof document === 'undefined' ? undefined : document);
  if (!sourceDocument) return [];

  return Array.from(sourceDocument.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    .filter((element) => isAvailableForFocus(element, options.exclude))
    .map((element, documentIndex) => ({ element, documentIndex }))
    .sort((left, right) => {
      const leftTabIndex = left.element.tabIndex;
      const rightTabIndex = right.element.tabIndex;
      if (leftTabIndex === rightTabIndex) return left.documentIndex - right.documentIndex;
      if (leftTabIndex === 0) return 1;
      if (rightTabIndex === 0) return -1;
      return leftTabIndex - rightTabIndex;
    })
    .map(({ element }) => element);
}

export function nextFocusableFrom(
  element: HTMLElement,
  direction: -1 | 1,
  exclude?: Element | null,
): HTMLElement | null {
  const focusable = focusableElementsInDocument({
    document: element.ownerDocument,
    exclude,
  });
  const currentIndex = focusable.indexOf(element);
  if (currentIndex < 0) return null;
  return focusable[currentIndex + direction] ?? null;
}
