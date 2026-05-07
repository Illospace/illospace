export type BrowserSessionLike = {
  id?: string | null;
  idea_id?: string | null;
  run_id?: string | number | null;
  status?: string | null;
  last_error?: string | null;
  [key: string]: any;
};

export type BrowserSessionViewState<
  Session extends BrowserSessionLike = BrowserSessionLike,
  Frame = any,
  Discovery = any,
  Extraction = any,
> = {
  session: Session | null;
  frame: Frame | null;
  discovery: Discovery | null;
  extraction: Extraction | null;
};

export function emptyBrowserSessionViewState<
  Session extends BrowserSessionLike = BrowserSessionLike,
  Frame = any,
  Discovery = any,
  Extraction = any,
>(): BrowserSessionViewState<Session, Frame, Discovery, Extraction> {
  return {
    session: null,
    frame: null,
    discovery: null,
    extraction: null,
  };
}

export function browserEventShouldFocusThread(
  msg: any,
  state: BrowserSessionLike | null,
): boolean {
  if (msg?.replayed) return false;
  return Boolean(msg?.idea_id && state?.id && (state.run_id || msg.run_id));
}

export function applyBrowserSessionSnapshot<
  Session extends BrowserSessionLike,
  Frame,
  Discovery,
  Extraction,
>(
  current: BrowserSessionViewState<Session, Frame, Discovery, Extraction>,
  state: Session | null,
  frame?: Frame | null,
): BrowserSessionViewState<Session, Frame, Discovery, Extraction> {
  return {
    ...current,
    session: state?.id ? state : current.session,
    frame: frame ?? current.frame,
  };
}

export function applyBrowserSessionDelta<
  Session extends BrowserSessionLike,
  Frame,
  Discovery,
  Extraction,
>(
  current: BrowserSessionViewState<Session, Frame, Discovery, Extraction>,
  msg: any,
): BrowserSessionViewState<Session, Frame, Discovery, Extraction> {
  if (!current.session?.id || msg?.session_id !== current.session.id || !msg?.result) {
    return current;
  }
  const nextState = msg.result.state || msg.result;
  const session = nextState?.id
    ? { ...current.session, ...nextState } as Session
    : current.session;
  return {
    session,
    frame: msg.result.frame ?? current.frame,
    discovery: msg.action === 'discover' && msg.result.elements ? msg.result as Discovery : current.discovery,
    extraction: msg.action === 'extract' && typeof msg.result.content === 'string'
      ? msg.result as Extraction
      : current.extraction,
  };
}

export function applyBrowserSessionError<Session extends BrowserSessionLike>(
  session: Session | null,
  msg: any,
): Session | null {
  if (!session?.id || msg?.session_id !== session.id) return session;
  return {
    ...session,
    status: 'error',
    last_error: msg.error || 'Browser error',
  };
}

export function browserCommandPayload(
  sessionId: string | null | undefined,
  data: Record<string, unknown> = {},
): Record<string, unknown> | null {
  if (!sessionId) return null;
  return { session_id: sessionId, ...data };
}
