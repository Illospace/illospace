import {
  applyBrowserSessionDelta,
  applyBrowserSessionError,
  applyBrowserSessionSnapshot,
  emptyBrowserSessionViewState,
} from '$lib/utils/cortexBrowserSession';
import type {
  BrowserDiscoveryResult,
  BrowserExtractResult,
  BrowserFrame,
  BrowserSessionState,
} from '$lib/types/cortex';

export type BrowserSessionStatus =
  | 'idle'
  | 'starting'
  | 'running'
  | 'closed'
  | 'error'
  | string;

export interface BrowserSessionViewState {
  session: BrowserSessionState | null;
  frame: BrowserFrame | null;
  discovery: BrowserDiscoveryResult | null;
  extraction: BrowserExtractResult | null;
}

export interface BrowserSessionDomainState extends BrowserSessionViewState {
  commandPending: boolean;
  lastCommand: string | null;
  lastError: string | null;
}

export function emptyBrowserSessionState(): BrowserSessionViewState {
  return emptyBrowserSessionViewState<
    BrowserSessionState,
    BrowserFrame,
    BrowserDiscoveryResult,
    BrowserExtractResult
  >();
}

export function emptyBrowserSessionDomainState(): BrowserSessionDomainState {
  return {
    ...emptyBrowserSessionState(),
    commandPending: false,
    lastCommand: null,
    lastError: null,
  };
}

export function browserSessionStatus(state: BrowserSessionViewState): BrowserSessionStatus {
  return state.session?.status ?? 'idle';
}

export function browserSessionId(state: BrowserSessionViewState): string | null {
  return state.session?.id ?? null;
}

export function hasBrowserFrame(state: BrowserSessionViewState): boolean {
  return Boolean(state.frame?.image_url);
}

export function applyBrowserSnapshotToState(
  current: BrowserSessionViewState,
  session: BrowserSessionState | null,
  frame?: BrowserFrame | null,
): BrowserSessionViewState {
  return applyBrowserSessionSnapshot<
    BrowserSessionState,
    BrowserFrame,
    BrowserDiscoveryResult,
    BrowserExtractResult
  >(current, session, frame);
}

export function applyBrowserDeltaToState(
  current: BrowserSessionViewState,
  msg: any,
): BrowserSessionViewState {
  return applyBrowserSessionDelta<
    BrowserSessionState,
    BrowserFrame,
    BrowserDiscoveryResult,
    BrowserExtractResult
  >(current, msg);
}

export function applyBrowserErrorToState(
  current: BrowserSessionViewState,
  msg: any,
): BrowserSessionViewState {
  return {
    ...current,
    session: applyBrowserSessionError(current.session, msg),
  };
}

export class BrowserSessionStateStore {
  session = $state<BrowserSessionState | null>(null);
  frame = $state<BrowserFrame | null>(null);
  discovery = $state<BrowserDiscoveryResult | null>(null);
  extraction = $state<BrowserExtractResult | null>(null);
  commandPending = $state(false);
  lastCommand = $state<string | null>(null);
  lastError = $state<string | null>(null);

  get view(): BrowserSessionDomainState {
    return {
      session: this.session,
      frame: this.frame,
      discovery: this.discovery,
      extraction: this.extraction,
      commandPending: this.commandPending,
      lastCommand: this.lastCommand,
      lastError: this.lastError,
    };
  }

  replace(next: BrowserSessionViewState) {
    this.session = next.session;
    this.frame = next.frame;
    this.discovery = next.discovery;
    this.extraction = next.extraction;
  }

  reset() {
    this.replace(emptyBrowserSessionState());
    this.commandPending = false;
    this.lastCommand = null;
    this.lastError = null;
  }
}

export function createBrowserSessionStateStore(): BrowserSessionStateStore {
  return new BrowserSessionStateStore();
}
