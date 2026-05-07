const SHARE_DROP_TYPES = new Set(['Files', 'text/uri-list', 'text/plain', 'text/html']);

export function dragDataIsShareable(dataTransfer: DataTransfer | null | undefined) {
  if (!dataTransfer) return false;
  if (dataTransfer.files?.length) return true;

  return Array.from(dataTransfer.types ?? []).some((type) => SHARE_DROP_TYPES.has(type));
}

export function setCopyDropEffect(dataTransfer: DataTransfer | null | undefined) {
  if (!dataTransfer) return;
  dataTransfer.dropEffect = 'copy';
}

export function droppedFilesFromDataTransfer(dataTransfer: DataTransfer | null | undefined) {
  if (!dataTransfer?.files?.length) return [];
  return Array.from(dataTransfer.files);
}

export function droppedTextFromDataTransfer(dataTransfer: DataTransfer | null | undefined) {
  if (!dataTransfer) return '';

  const uriList = dataTransferText(dataTransfer, 'text/uri-list')
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('#'));
  if (uriList.length > 0) return uriList.join('\n');

  const plainText = dataTransferText(dataTransfer, 'text/plain').trim();
  if (plainText) return plainText;

  return textFromDroppedHtml(dataTransferText(dataTransfer, 'text/html'));
}

function dataTransferText(dataTransfer: DataTransfer, type: string) {
  try {
    return dataTransfer.getData(type) ?? '';
  } catch {
    return '';
  }
}

function textFromDroppedHtml(html: string) {
  if (!html.trim() || typeof DOMParser === 'undefined') return '';

  try {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const linkedElement = doc.querySelector<HTMLAnchorElement | HTMLImageElement | HTMLSourceElement | HTMLVideoElement>(
      'a[href], img[src], source[src], video[src]',
    );
    const linkedValue =
      linkedElement instanceof HTMLAnchorElement
        ? linkedElement.href
        : linkedElement instanceof HTMLImageElement ||
            linkedElement instanceof HTMLSourceElement ||
            linkedElement instanceof HTMLVideoElement
          ? linkedElement.src
          : '';
    return linkedValue || doc.body.textContent?.trim() || '';
  } catch {
    return '';
  }
}
