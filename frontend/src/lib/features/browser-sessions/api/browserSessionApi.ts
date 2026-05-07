import { api } from '$lib/api/client';
import { pickTypedApiMethods } from '$lib/api/featureApi';
import type { BrowserSessionState } from '$lib/types/cortex';

export type BrowserSessionCreateInput = Parameters<typeof api.createBrowserSession>[1];
export type BrowserSessionSnapshotInput = Parameters<typeof api.snapshotBrowserSession>[1];
export type BrowserSessionSnapshot = Awaited<ReturnType<typeof api.snapshotBrowserSession>>;
export type CloseBrowserSessionResponse = Awaited<ReturnType<typeof api.closeBrowserSession>>;

type BrowserSessionApiMethods = {
  getBrowserSession: (ideaId: string) => Promise<BrowserSessionState | null>;
  createBrowserSession: (
    ideaId: string,
    data: BrowserSessionCreateInput,
  ) => Promise<BrowserSessionState>;
  snapshotBrowserSession: (
    sessionId: string,
    data?: BrowserSessionSnapshotInput,
  ) => Promise<BrowserSessionSnapshot>;
  closeBrowserSession: (sessionId: string) => Promise<CloseBrowserSessionResponse>;
};

export const browserSessionApi = pickTypedApiMethods<BrowserSessionApiMethods>([
  'getBrowserSession',
  'createBrowserSession',
  'snapshotBrowserSession',
  'closeBrowserSession',
]);

export const {
  getBrowserSession,
  createBrowserSession,
  snapshotBrowserSession,
  closeBrowserSession,
} = browserSessionApi;
