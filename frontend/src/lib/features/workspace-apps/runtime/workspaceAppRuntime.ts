export function buildWorkspaceAppSrcdoc({
  source,
  title,
  manifest,
  runtimeStyle,
  bridgeScript,
}: {
  source: string;
  title: string;
  manifest: Record<string, any>;
  runtimeStyle: string;
  bridgeScript: string;
}) {
  const injections = `
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>${escapeHtml(title)}</title>
    <script>window.__ILLO_APP_MANIFEST__ = ${jsonForScript(manifest || {})};<\/script>
    ${runtimeStyle}
    ${bridgeScript}
  `;

  if (/<html[\s>]/i.test(source)) {
    return injectIntoFullDocument(source, injections);
  }

  return `<!doctype html>
    <html>
      <head>${injections}</head>
      <body>
        <main class="illo-generated-app-root">${source}</main>
      </body>
    </html>`;
}

export function stableSignature(value: unknown) {
  return JSON.stringify(stableJsonValue(value));
}

export function jsonForScript(value: unknown) {
  return JSON.stringify(value).replace(/</g, '\\u003c');
}

export function escapeHtml(value: string) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function injectIntoFullDocument(source: string, injections: string) {
  if (source.match(/<head[^>]*>/i)) {
    return source.replace(/<head([^>]*)>/i, `<head$1>${injections}`);
  }
  if (source.match(/<\/head>/i)) {
    return source.replace(/<\/head>/i, `${injections}</head>`);
  }
  if (source.match(/<body[^>]*>/i)) {
    return source.replace(/<body([^>]*)>/i, `<body$1>${injections}`);
  }
  return `${injections}${source}`;
}

function stableJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => stableJsonValue(item));
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableJsonValue(item)]),
  );
}
