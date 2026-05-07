import { cortex } from '$lib/stores/cortex.svelte';
import type { BrowserSessionState } from '$lib/types/cortex';

export interface BrowserCommandStoreLike {
  selectedIdeaId: string | null;
  browserSession: BrowserSessionState | null;
  ensureBrowserSession(
    url?: string,
    options?: {
      storage_mode?: 'ephemeral' | 'idea';
      allow_downloads?: boolean;
      allow_file_uploads?: boolean;
    },
  ): Promise<BrowserSessionState | null> | BrowserSessionState | null;
  browserNavigate(url: string): void;
  browserClick(x: number, y: number): void;
  browserScroll(deltaX: number, deltaY: number): void;
  browserClickSelector(selector: string): void;
  browserType(text: string, pressEnter?: boolean): void;
  browserKey(key: string): void;
  browserRefresh(): void;
  browserBack(): void;
  browserForward(): void;
  browserNewTab(url?: string): void;
  browserSwitchTab(index: number): void;
  browserCloseTab(index?: number): void;
  browserUploadAttachment(selector: string, attachmentUrl: string): void;
  browserDiscover(selector?: string, maxResults?: number): void;
  browserExtract(selector?: string, mode?: string, maxChars?: number): void;
  browserSnapshot(persist?: boolean, title?: string): void;
  browserSaveScreenshot(fullPage?: boolean): void;
  browserPrintPdf(landscape?: boolean): void;
  browserClose(): Promise<void> | void;
}

export interface BrowserSessionStartOptions {
  url?: string;
  storageMode?: 'ephemeral' | 'idea';
  allowDownloads?: boolean;
  allowFileUploads?: boolean;
}

export interface BrowserCommandController {
  ensureSession(options?: BrowserSessionStartOptions): Promise<BrowserSessionState | null>;
  navigate(url: string): void;
  click(x: number, y: number): void;
  scroll(deltaX: number, deltaY: number): void;
  clickSelector(selector: string): void;
  type(text: string, pressEnter?: boolean): void;
  key(key: string): void;
  refresh(): void;
  back(): void;
  forward(): void;
  newTab(url?: string): void;
  switchTab(index: number): void;
  closeTab(index?: number): void;
  uploadAttachment(selector: string, attachmentUrl: string): void;
  discover(selector?: string, maxResults?: number): void;
  extract(selector?: string, mode?: string, maxChars?: number): void;
  snapshot(persist?: boolean, title?: string): void;
  saveScreenshot(fullPage?: boolean): void;
  printPdf(landscape?: boolean): void;
  close(): Promise<void>;
}

export function createBrowserCommandController(
  source: BrowserCommandStoreLike = cortex as unknown as BrowserCommandStoreLike,
): BrowserCommandController {
  return {
    ensureSession: async (options = {}) =>
      await source.ensureBrowserSession(options.url, {
        storage_mode: options.storageMode,
        allow_downloads: options.allowDownloads,
        allow_file_uploads: options.allowFileUploads,
      }),
    navigate: (url) => source.browserNavigate(url),
    click: (x, y) => source.browserClick(x, y),
    scroll: (deltaX, deltaY) => source.browserScroll(deltaX, deltaY),
    clickSelector: (selector) => source.browserClickSelector(selector),
    type: (text, pressEnter = false) => source.browserType(text, pressEnter),
    key: (key) => source.browserKey(key),
    refresh: () => source.browserRefresh(),
    back: () => source.browserBack(),
    forward: () => source.browserForward(),
    newTab: (url) => source.browserNewTab(url),
    switchTab: (index) => source.browserSwitchTab(index),
    closeTab: (index) => source.browserCloseTab(index),
    uploadAttachment: (selector, attachmentUrl) => source.browserUploadAttachment(selector, attachmentUrl),
    discover: (selector, maxResults) => source.browserDiscover(selector, maxResults),
    extract: (selector, mode, maxChars) => source.browserExtract(selector, mode, maxChars),
    snapshot: (persist = false, title) => source.browserSnapshot(persist, title),
    saveScreenshot: (fullPage = true) => source.browserSaveScreenshot(fullPage),
    printPdf: (landscape = false) => source.browserPrintPdf(landscape),
    close: async () => {
      await source.browserClose();
    },
  };
}

export const browserCommandController = createBrowserCommandController();
