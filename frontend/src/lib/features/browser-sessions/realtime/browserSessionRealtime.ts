import { browserEventShouldFocusThread } from '$lib/utils/cortexBrowserSession';
import type { BrowserFrame, BrowserSessionState } from '$lib/types/cortex';
import {
  applyBrowserDeltaToState,
  applyBrowserErrorToState,
  applyBrowserSnapshotToState,
  emptyBrowserSessionState,
  type BrowserSessionViewState,
} from '../domain/browserSessionState.svelte';

export type BrowserSessionRealtimeEvent =
  | { type: 'state'; ideaId: string | null; session: BrowserSessionState | null }
  | { type: 'frame'; ideaId: string | null; session: BrowserSessionState | null; frame: BrowserFrame | null }
  | { type: 'delta'; message: any }
  | { type: 'closed'; ideaId: string | null }
  | { type: 'error'; message: any };

export interface BrowserSessionRealtimeResult {
  state: BrowserSessionViewState;
  shouldFocusThread: boolean;
  focusIdeaId: string | null;
  shouldSubscribeSessionId: string | null;
  shouldClear: boolean;
}

function eventIdeaId(msg: any): string | null {
  return typeof msg?.idea_id === 'string' ? msg.idea_id : null;
}

function eventSession(msg: any): BrowserSessionState | null {
  return msg?.state?.id ? (msg.state as BrowserSessionState) : null;
}

export function normalizeBrowserSessionRealtimeEvent(
  type: 'browser_session_state' | 'browser_session_frame' | 'browser_session_delta' | 'browser_session_closed' | 'browser_session_error',
  msg: any,
): BrowserSessionRealtimeEvent {
  if (type === 'browser_session_state') {
    return { type: 'state', ideaId: eventIdeaId(msg), session: eventSession(msg) };
  }
  if (type === 'browser_session_frame') {
    return {
      type: 'frame',
      ideaId: eventIdeaId(msg),
      session: eventSession(msg),
      frame: msg?.frame ? (msg.frame as BrowserFrame) : null,
    };
  }
  if (type === 'browser_session_delta') return { type: 'delta', message: msg };
  if (type === 'browser_session_closed') return { type: 'closed', ideaId: eventIdeaId(msg) };
  return { type: 'error', message: msg };
}

export function reduceBrowserSessionRealtimeEvent(options: {
  current: BrowserSessionViewState;
  selectedIdeaId: string | null;
  event: BrowserSessionRealtimeEvent;
}): BrowserSessionRealtimeResult {
  const { current, selectedIdeaId, event } = options;
  const base = {
    state: current,
    shouldFocusThread: false,
    focusIdeaId: null,
    shouldSubscribeSessionId: null,
    shouldClear: false,
  };

  if (event.type === 'state' || event.type === 'frame') {
    const ideaId = event.ideaId;
    if (!ideaId) return base;
    if (
      ideaId !== selectedIdeaId
      && browserEventShouldFocusThread(
        { idea_id: ideaId, run_id: event.session?.run_id },
        event.session,
      )
    ) {
      return {
        ...base,
        shouldFocusThread: true,
        focusIdeaId: ideaId,
      };
    }
    if (ideaId !== selectedIdeaId) return base;
    const state = applyBrowserSnapshotToState(
      current,
      event.session,
      event.type === 'frame' ? event.frame : undefined,
    );
    return {
      ...base,
      state,
      shouldSubscribeSessionId: state.session?.id && state.session.id !== current.session?.id
        ? state.session.id
        : null,
    };
  }

  if (event.type === 'delta') {
    return {
      ...base,
      state: applyBrowserDeltaToState(current, event.message),
    };
  }

  if (event.type === 'closed') {
    if (event.ideaId !== selectedIdeaId) return base;
    return {
      ...base,
      state: emptyBrowserSessionState(),
      shouldClear: true,
    };
  }

  return {
    ...base,
    state: applyBrowserErrorToState(current, event.message),
  };
}
