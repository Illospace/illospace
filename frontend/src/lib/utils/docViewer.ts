const STATIC_UPLOAD_PREFIX = '/static/uploads/';
const PERCENT_ESCAPE = /%[0-9a-f]{2}/i;
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'svg']);

export type DocViewerRenderMode = 'image' | 'markdown' | 'pdf' | 'text';

/**
 * Validate the public doc viewer's `src` query param.
 *
 * The viewer only ever fetches same-origin files under the auth-free
 * /static/uploads mount; anything else (absolute URLs, traversal, other
 * routes) is rejected so the page cannot be used to frame arbitrary content.
 */
export function normalizeDocViewerSrc(raw: string | null | undefined): string | null {
  const value = (raw || '').trim();
  if (!value.startsWith(STATIC_UPLOAD_PREFIX)) return null;
  if (value.includes('\\') || /[?#\s]/.test(value)) return null;

  let decoded = value;
  for (let iteration = 0; iteration < 5 && PERCENT_ESCAPE.test(decoded); iteration += 1) {
    try {
      decoded = decodeURIComponent(decoded);
    } catch {
      return null;
    }
  }
  if (PERCENT_ESCAPE.test(decoded)) return null;
  if (!decoded.startsWith(STATIC_UPLOAD_PREFIX) || decoded.includes('\\')) return null;
  const segments = decoded.slice(1).split('/');
  if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) return null;

  return value;
}

export function docViewerRenderMode(src: string): DocViewerRenderMode {
  const filename = (src || '').split('/').pop() || '';
  const extension = filename.includes('.') ? (filename.split('.').pop() || '').toLowerCase() : '';
  if (extension === 'md' || extension === 'markdown') return 'markdown';
  if (extension === 'pdf') return 'pdf';
  if (IMAGE_EXTENSIONS.has(extension)) return 'image';
  return 'text';
}

export function firstMarkdownHeading(markdown: string): string {
  for (const line of (markdown || '').split('\n')) {
    const match = line.trim().match(/^#{1,6}\s+(.+?)\s*#*\s*$/);
    if (match) return match[1].replace(/[*_`]/g, '').trim();
  }
  return '';
}

export function docViewerFilename(src: string | null | undefined): string {
  const segment = (src || '').split('/').pop() || '';
  try {
    return decodeURIComponent(segment).trim();
  } catch {
    return segment.trim();
  }
}

/** Title precedence: explicit ?title= param, first markdown heading, filename. */
export function docViewerTitle(
  titleParam: string | null | undefined,
  src: string | null | undefined,
  documentText = '',
): string {
  const explicit = (titleParam || '').trim();
  if (explicit) return explicit;
  const heading = firstMarkdownHeading(documentText);
  if (heading) return heading;
  return docViewerFilename(src) || 'Shared document';
}
