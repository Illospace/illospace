const CODE_BLOCK_TOKEN = '\u0000CB';
const INLINE_CODE_TOKEN = '\u0000IC';
const INLINE_LINK_TOKEN = '\u0000LK';
const RENDER_CACHE_MAX_ENTRIES = 400;
const RENDER_CACHE_MAX_SOURCE_LENGTH = 100_000;

const renderCache = new Map<string, string>();

function getCachedRender(markdown: string): string | null {
  if (markdown.length > RENDER_CACHE_MAX_SOURCE_LENGTH) return null;
  if (!renderCache.has(markdown)) return null;
  const cached = renderCache.get(markdown) ?? '';
  renderCache.delete(markdown);
  renderCache.set(markdown, cached);
  return cached;
}

function setCachedRender(markdown: string, html: string) {
  if (markdown.length > RENDER_CACHE_MAX_SOURCE_LENGTH) return;
  renderCache.set(markdown, html);
  while (renderCache.size > RENDER_CACHE_MAX_ENTRIES) {
    const oldestKey = renderCache.keys().next().value;
    if (oldestKey === undefined) break;
    renderCache.delete(oldestKey);
  }
}

function escapeHtml(text: string): string {
  return text
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function decodeHrefEntities(text: string): string {
  return text
    .replaceAll('&amp;', '&')
    .replaceAll('&quot;', '"')
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>');
}

function safeHref(url: string): string | null {
  const trimmed = decodeHrefEntities(url || '').trim();
  if (/^https?:\/\//i.test(trimmed) || /^mailto:/i.test(trimmed)) return escapeHtml(trimmed);
  if (/^\/(?!\/)/.test(trimmed)) return escapeHtml(trimmed);
  return null;
}

function safeImageSrc(url: string): string | null {
  const trimmed = decodeHrefEntities(url || '').trim();
  if (!/^\/static\/uploads\//.test(trimmed)) return null;
  return safeHref(trimmed);
}

export function normalizeReadableMarkdown(input: string): string {
  return (input || '')
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n+\s*([,.;:])\s*\n+/g, '$1 ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function applyInlineMarkdown(text: string): string {
  const links: string[] = [];
  let output = text
    .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_match, label, url) => {
      const safe = safeImageSrc(url);
      if (!safe) return label;
      const cleanLabel = String(label || 'Thread asset');
      return `<a class="md-readable-image-link" href="${safe}" target="_blank" rel="noopener"><img class="md-readable-image" src="${safe}" alt="${cleanLabel}" loading="lazy" decoding="async"/></a>`;
    })
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, label, url) => {
      const safe = safeHref(url);
      if (!safe) return label;
      const idx = links.length;
      links.push(`<a href="${safe}" target="_blank" rel="noopener">${label}</a>`);
      return `${INLINE_LINK_TOKEN}${idx}\u0000`;
    })
    .replace(/https?:\/\/[^\s<>"']+/gi, (rawUrl) => {
      const url = rawUrl.replace(/[),.;!?]+$/g, '');
      const trailing = rawUrl.slice(url.length);
      const safe = safeHref(url);
      return safe ? `<a href="${safe}" target="_blank" rel="noopener">${url}</a>${trailing}` : rawUrl;
    })
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>');

  output = output.replace(new RegExp(`${INLINE_LINK_TOKEN}(\\d+)\\u0000`, 'g'), (_match, idx) => links[Number(idx)] || '');
  return output;
}

function renderParagraph(lines: string[]): string {
  return `<p>${applyInlineMarkdown(lines.join('<br/>'))}</p>`;
}

function restorePlaceholders(html: string, codeBlocks: string[], inlineCodes: string[]): string {
  return html
    .replace(new RegExp(`${CODE_BLOCK_TOKEN}(\\d+)\\u0000`, 'g'), (_match, idx) => codeBlocks[Number(idx)] || '')
    .replace(new RegExp(`${INLINE_CODE_TOKEN}(\\d+)\\u0000`, 'g'), (_match, idx) => inlineCodes[Number(idx)] || '');
}

export function renderReadableMarkdown(markdown: string): string {
  const cached = getCachedRender(markdown);
  if (cached !== null) return cached;

  const normalized = normalizeReadableMarkdown(markdown);
  if (!normalized) {
    setCachedRender(markdown, '');
    return '';
  }

  const codeBlocks: string[] = [];
  const inlineCodes: string[] = [];
  let processed = normalized.replace(/```([\w-]*)\n([\s\S]*?)```/g, (_match, lang, code) => {
    const idx = codeBlocks.length;
    const languageClass = lang ? ` lang-${escapeHtml(lang)}` : '';
    codeBlocks.push(`<pre class="md-code-block"><code class="${languageClass.trim()}">${escapeHtml(code.trim())}</code></pre>`);
    return `${CODE_BLOCK_TOKEN}${idx}\u0000`;
  });

  processed = processed.replace(/`([^`\n]+)`/g, (_match, code) => {
    const idx = inlineCodes.length;
    inlineCodes.push(`<code class="md-inline-code">${escapeHtml(code)}</code>`);
    return `${INLINE_CODE_TOKEN}${idx}\u0000`;
  });

  processed = escapeHtml(processed);

  const blocks: string[] = [];
  const paragraph: string[] = [];
  let listItems: string[] = [];
  let listType: 'ul' | 'ol' | null = null;
  let listStart = 1;
  let orderedContinuationStart: number | null = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(renderParagraph(paragraph));
    paragraph.length = 0;
  };

  const flushList = () => {
    if (!listType || !listItems.length) return;
    const startAttr = listType === 'ol' && listStart > 1 ? ` start="${listStart}"` : '';
    blocks.push(`<${listType}${startAttr}>${listItems.join('')}</${listType}>`);
    orderedContinuationStart = listType === 'ol' ? listStart + listItems.length : null;
    listItems = [];
    listType = null;
    listStart = 1;
  };

  const resetOrderedContinuation = () => {
    orderedContinuationStart = null;
  };

  for (const line of processed.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      continue;
    }

    if (trimmed.startsWith(CODE_BLOCK_TOKEN)) {
      flushParagraph();
      flushList();
      resetOrderedContinuation();
      blocks.push(trimmed);
      continue;
    }

    const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      resetOrderedContinuation();
      const level = heading[1].length;
      blocks.push(`<h${level}>${applyInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const unordered = trimmed.match(/^[-*]\s+(.+)$/);
    const ordered = trimmed.match(/^(\d+)\.\s+(.+)$/);
    if (unordered || ordered) {
      flushParagraph();
      const nextType = unordered ? 'ul' : 'ol';
      if (listType && listType !== nextType) flushList();
      if (nextType === 'ul') {
        resetOrderedContinuation();
      }
      if (!listType && nextType === 'ol') {
        const marker = Number(ordered?.[1] ?? 1);
        listStart = marker === 1 && orderedContinuationStart !== null
          ? orderedContinuationStart
          : Math.max(1, Number.isFinite(marker) ? marker : 1);
      }
      listType = nextType;
      listItems.push(`<li>${applyInlineMarkdown(unordered?.[1] || ordered?.[2] || '')}</li>`);
      continue;
    }

    const quote = trimmed.match(/^&gt;\s+(.+)$/);
    if (quote) {
      flushParagraph();
      flushList();
      resetOrderedContinuation();
      blocks.push(`<blockquote>${applyInlineMarkdown(quote[1])}</blockquote>`);
      continue;
    }

    paragraph.push(trimmed);
  }

  flushParagraph();
  flushList();

  const html = restorePlaceholders(blocks.join(''), codeBlocks, inlineCodes);
  setCachedRender(markdown, html);
  return html;
}
