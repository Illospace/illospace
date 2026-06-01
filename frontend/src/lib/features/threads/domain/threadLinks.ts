import { browser } from '$app/environment';

export function encodeThreadId(threadId: string | number): string {
  return encodeURIComponent(String(threadId));
}

export function threadRoute(threadId: string | number): string {
  return `/threads/${encodeThreadId(threadId)}`;
}

export function threadUrl(threadId: string | number): string {
  const route = threadRoute(threadId);
  if (!browser) return route;
  return `${window.location.origin}${route}`;
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

export function threadIdFromReference(value: string | null | undefined): string | null {
  const text = String(value || '').trim();
  if (!text) return null;
  try {
    const parsed = new URL(text, browser ? window.location.origin : 'http://illo.local');
    const threadId = threadIdFromPath(parsed.pathname);
    if (threadId) return threadId;
    if (parsed.pathname === '/cortex') return parsed.searchParams.get('idea');
  } catch {
    if (text.startsWith('/threads/')) return threadIdFromPath(text);
  }
  return null;
}
