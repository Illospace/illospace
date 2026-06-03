import createDOMPurify from 'dompurify';

export type SvgSanitizer = (content: string) => string;

export interface VisualImageSourceOptions {
  sanitizeSvg?: SvgSanitizer;
}

type DOMPurifyLike = {
  sanitize: (dirty: string, config?: Record<string, unknown>) => string;
};

const SVG_DATA_URL_PREFIX = 'data:image/svg+xml;charset=utf-8,';

function defaultSanitizeSvg(content: string): string {
  const purifier = createDOMPurify as unknown as DOMPurifyLike | ((window: Window) => DOMPurifyLike);
  if (typeof (purifier as DOMPurifyLike).sanitize === 'function') {
    return String((purifier as DOMPurifyLike).sanitize(content, { USE_PROFILES: { svg: true } }));
  }
  if (typeof window !== 'undefined') {
    const windowPurifier = (purifier as (window: Window) => DOMPurifyLike)(window);
    return String(windowPurifier.sanitize(content, { USE_PROFILES: { svg: true } }));
  }
  return '';
}

export function isInlineSvgImageContent(content: string): boolean {
  return /^\s*(?:<\?xml[\s\S]*?\?>\s*)?<svg[\s>]/i.test(content);
}

export function inlineSvgImageSrc(content: string, sanitizeSvg: SvgSanitizer = defaultSanitizeSvg): string {
  const sanitized = String(sanitizeSvg(content)).trim();
  if (!isInlineSvgImageContent(sanitized)) return '';
  return `${SVG_DATA_URL_PREFIX}${encodeURIComponent(sanitized)}`;
}

export function safeVisualImageSrc(content: string, options: VisualImageSourceOptions = {}): string {
  const trimmed = content.trim();
  if (isInlineSvgImageContent(trimmed)) return inlineSvgImageSrc(trimmed, options.sanitizeSvg);
  if (/^https?:\/\//i.test(trimmed) || /^data:image\//i.test(trimmed) || /^blob:/i.test(trimmed)) {
    return trimmed;
  }
  return '';
}
