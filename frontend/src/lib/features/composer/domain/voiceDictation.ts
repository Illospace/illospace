export function appendVoiceTranscriptDraft(draft: string, transcript: string): string {
  const cleanDraft = draft.trimEnd();
  const cleanTranscript = transcript.trim();
  if (!cleanTranscript) return draft;
  if (!cleanDraft) return cleanTranscript;
  return `${cleanDraft} ${cleanTranscript}`;
}

export type VoiceDictationStatus = 'idle' | 'starting' | 'recording' | 'committing' | 'error';

export interface VoiceDictationSession {
  client_secret: string;
  model: string;
  expires_at?: number | null;
}

export interface VoiceDictationTransportHandlers {
  onTranscriptDelta: (delta: string) => void;
  onTranscriptCompleted?: (transcript: string) => void;
}

export interface VoiceDictationConnection {
  stop: () => Promise<void> | void;
}

export interface VoiceDictationControllerDeps {
  getDraft: () => string;
  setDraft: (value: string) => void;
  submit: () => Promise<void> | void;
  createSession: () => Promise<VoiceDictationSession>;
  connect: (
    session: VoiceDictationSession,
    handlers: VoiceDictationTransportHandlers,
  ) => Promise<VoiceDictationConnection>;
}

export interface VoiceDictationSnapshot {
  status: VoiceDictationStatus;
  transcript: string;
  error: string | null;
}

export function createVoiceDictationController(deps: VoiceDictationControllerDeps) {
  let status: VoiceDictationStatus = 'idle';
  let transcript = '';
  let error: string | null = null;
  let connection: VoiceDictationConnection | null = null;

  function snapshot(): VoiceDictationSnapshot {
    return { status, transcript, error };
  }

  async function start() {
    if (status !== 'idle') return;
    status = 'starting';
    transcript = '';
    error = null;
    try {
      const session = await deps.createSession();
      connection = await deps.connect(session, {
        onTranscriptDelta(delta) {
          transcript += delta;
        },
        onTranscriptCompleted(completedTranscript) {
          transcript = completedTranscript;
        },
      });
      status = 'recording';
    } catch (err) {
      status = 'error';
      error = err instanceof Error ? err.message : 'Voice dictation could not start.';
    }
  }

  function commitTranscript() {
    deps.setDraft(appendVoiceTranscriptDraft(deps.getDraft(), transcript));
    transcript = '';
  }

  async function stop(): Promise<boolean> {
    if (status !== 'recording') return false;
    status = 'committing';
    try {
      await connection?.stop();
      connection = null;
      commitTranscript();
      status = 'idle';
      return true;
    } catch (err) {
      connection = null;
      commitTranscript();
      status = 'error';
      error = err instanceof Error ? err.message : 'Voice dictation could not finish.';
      return false;
    }
  }

  async function send() {
    if (status !== 'recording') return;
    const stopped = await stop();
    if (!stopped) return;
    await deps.submit();
  }

  return {
    snapshot,
    start,
    stop,
    send,
  };
}
