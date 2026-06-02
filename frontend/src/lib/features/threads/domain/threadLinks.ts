export const CORTEX_THREAD_PARAM = 'idea';
export const THREAD_ROUTE_PREFIX = '/threads';

const TRANSIENT_THREAD_QUERY_PARAMS = [
  CORTEX_THREAD_PARAM,
  'onboarding',
  'open_existing',
  'modal',
];

function threadQueryParams(sourceParams?: URLSearchParams): URLSearchParams {
  const params = new URLSearchParams(sourceParams);
  for (const key of TRANSIENT_THREAD_QUERY_PARAMS) {
    params.delete(key);
  }
  return params;
}

function browserOrigin(): string | null {
  return typeof window === 'undefined' ? null : window.location.origin;
}

export function encodeThreadId(threadId: string | number): string {
  return encodeURIComponent(String(threadId));
}

export function threadRoute(threadId: string | number): string {
  return `${THREAD_ROUTE_PREFIX}/${encodeThreadId(threadId)}`;
}

export function threadUrl(threadId: string | number): string {
  const route = threadRoute(threadId);
  const origin = browserOrigin();
  if (!origin) return route;
  return `${origin}${route}`;
}

export function threadIdFromPath(pathname: string): string | null {
  const match = /^\/threads\/([^/?#]+)/.exec(pathname || '');
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

export function threadIdFromThreadPathname(pathname: string): string | null {
  return threadIdFromPath(pathname);
}

export function threadIdFromUrl(url: Pick<URL, 'pathname' | 'searchParams'>): string | null {
  return threadIdFromPath(url.pathname) || url.searchParams.get(CORTEX_THREAD_PARAM);
}

export function threadIdFromReference(value: string | null | undefined): string | null {
  const text = String(value || '').trim();
  if (!text) return null;
  try {
    const parsed = new URL(text, browserOrigin() ?? 'http://illo.local');
    const threadId = threadIdFromPath(parsed.pathname);
    if (threadId) return threadId;
    if (parsed.pathname === '/cortex') return parsed.searchParams.get(CORTEX_THREAD_PARAM);
  } catch {
    if (text.startsWith(`${THREAD_ROUTE_PREFIX}/`)) return threadIdFromPath(text);
  }
  return null;
}

export function isThreadRoutePathname(pathname: string): boolean {
  return Boolean(threadIdFromPath(pathname));
}

export function buildThreadHref(
  threadId: string,
  sourceParams?: URLSearchParams,
): string {
  const params = threadQueryParams(sourceParams);
  const query = params.toString();
  return `${threadRoute(threadId)}${query ? `?${query}` : ''}`;
}

export function buildCortexThreadHref(
  threadId: string,
  sourceParams?: URLSearchParams,
): string {
  return buildThreadHref(threadId, sourceParams);
}

export function buildCortexHrefWithoutThread(sourceParams?: URLSearchParams): string {
  const params = new URLSearchParams(sourceParams);
  params.delete(CORTEX_THREAD_PARAM);
  const query = params.toString();
  return `/cortex${query ? `?${query}` : ''}`;
}

export function buildAbsoluteCortexThreadUrl(
  threadId: string,
  origin: string,
  sourceParams?: URLSearchParams,
): string {
  return new URL(buildThreadHref(threadId, sourceParams), origin).toString();
}
