const NAIVE_ISO_TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/;

function normalizeServerTimestamp(timestamp: string): string {
  return NAIVE_ISO_TIMESTAMP_RE.test(timestamp) ? `${timestamp}Z` : timestamp;
}

export function parseServerDate(timestamp: string | null | undefined): Date | null {
  if (!timestamp) return null;

  const parsed = new Date(normalizeServerTimestamp(timestamp));
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function parseServerTimeMs(timestamp: string | null | undefined): number {
  return parseServerDate(timestamp)?.getTime() ?? 0;
}

export function relativeTimeAgo(timestamp: string | null | undefined, nowMs = Date.now()): string {
  const parsed = parseServerDate(timestamp);
  if (!parsed) return '';

  const diff = Math.max(0, nowMs - parsed.getTime());
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function formatDurationSeconds(seconds: number | null | undefined): string {
  if (!seconds) return '';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${remainingSeconds}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function formatDurationMs(milliseconds: number | null | undefined): string {
  if (!milliseconds || milliseconds < 1000) return '< 1s';
  const seconds = Math.floor(milliseconds / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}
