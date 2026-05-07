import { cortex } from '$lib/stores/cortex.svelte';
import type {
  BrowserDiscoveryResult,
  BrowserExtractResult,
  BrowserFrame,
  BrowserSessionState,
} from '$lib/types/cortex';
import type { BrowserSessionViewState } from '../domain/browserSessionState.svelte';
import { createBrowserCommandController, type BrowserCommandController } from './browserCommandController';

export interface BrowserSessionStoreLike {
  browserSession: BrowserSessionState | null;
  browserFrame: BrowserFrame | null;
  browserDiscovery: BrowserDiscoveryResult | null;
  browserExtraction: BrowserExtractResult | null;
}

export interface BrowserSessionController {
  viewState(): BrowserSessionViewState;
  commands: BrowserCommandController;
}

export function buildBrowserSessionViewState(source: BrowserSessionStoreLike): BrowserSessionViewState {
  return {
    session: source.browserSession ?? null,
    frame: source.browserFrame ?? null,
    discovery: source.browserDiscovery ?? null,
    extraction: source.browserExtraction ?? null,
  };
}

export function createBrowserSessionController(
  source: BrowserSessionStoreLike = cortex as unknown as BrowserSessionStoreLike,
): BrowserSessionController {
  return {
    viewState: () => buildBrowserSessionViewState(source),
    commands: createBrowserCommandController(cortex as any),
  };
}

export const browserSessionController = createBrowserSessionController();
