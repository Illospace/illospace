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

export function threadIdFromThreadPathname(pathname: string): string | null {
  const prefix = `${THREAD_ROUTE_PREFIX}/`;
  if (!pathname.startsWith(prefix)) return null;
  const encoded = pathname.slice(prefix.length).split('/', 1)[0];
  if (!encoded) return null;
  try {
    return decodeURIComponent(encoded);
  } catch {
    return encoded;
  }
}

export function threadIdFromUrl(url: Pick<URL, 'pathname' | 'searchParams'>): string | null {
  return threadIdFromThreadPathname(url.pathname) || url.searchParams.get(CORTEX_THREAD_PARAM);
}

export function isThreadRoutePathname(pathname: string): boolean {
  return Boolean(threadIdFromThreadPathname(pathname));
}

export function buildThreadHref(
  threadId: string,
  sourceParams?: URLSearchParams,
): string {
  const params = threadQueryParams(sourceParams);
  const query = params.toString();
  return `${THREAD_ROUTE_PREFIX}/${encodeURIComponent(threadId)}${query ? `?${query}` : ''}`;
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
