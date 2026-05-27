import type {
  VoiceDictationConnection,
  VoiceDictationSession,
  VoiceDictationTransportHandlers,
} from '$lib/features/composer/domain/voiceDictation';

const OPENAI_REALTIME_CALLS_URL = 'https://api.openai.com/v1/realtime/calls';

type OpenAIRealtimeEvent = {
  type?: string;
  delta?: string;
  transcript?: string;
};

export async function createOpenAIRealtimeVoiceConnection(
  session: VoiceDictationSession,
  handlers: VoiceDictationTransportHandlers,
): Promise<VoiceDictationConnection> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const pc = new RTCPeerConnection();
  const dc = pc.createDataChannel('oai-events');
  let transcriptCompleted: (() => void) | null = null;
  let closed = false;

  function cleanup() {
    if (closed) return;
    closed = true;
    transcriptCompleted = null;
    for (const track of stream.getTracks()) {
      track.stop();
    }
    if (dc.readyState !== 'closed') dc.close();
    pc.close();
  }

  try {
    for (const track of stream.getAudioTracks()) {
      pc.addTrack(track, stream);
    }

    dc.onmessage = (event) => {
      const data = parseRealtimeEvent(event.data);
      if (!data?.type) return;
      if (data.type === 'conversation.item.input_audio_transcription.delta' && typeof data.delta === 'string') {
        handlers.onTranscriptDelta(data.delta);
      }
      if (
        data.type === 'conversation.item.input_audio_transcription.completed'
        && typeof data.transcript === 'string'
      ) {
        handlers.onTranscriptCompleted?.(data.transcript);
        transcriptCompleted?.();
        transcriptCompleted = null;
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const answerSdp = await postRealtimeOffer(session.client_secret, offer.sdp || '');
    await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp });
  } catch (err) {
    cleanup();
    throw err;
  }

  return {
    async stop() {
      try {
        for (const track of stream.getTracks()) {
          track.stop();
        }
        if (dc.readyState === 'open') {
          const committed = waitForTranscriptCompletion((resolve) => {
            transcriptCompleted = resolve;
          });
          dc.send(JSON.stringify({ type: 'input_audio_buffer.commit' }));
          await committed;
        }
      } finally {
        cleanup();
      }
    },
  };
}

async function postRealtimeOffer(clientSecret: string, sdp: string): Promise<string> {
  const response = await fetch(OPENAI_REALTIME_CALLS_URL, {
    method: 'POST',
    body: sdp,
    headers: {
      Authorization: `Bearer ${clientSecret}`,
      'Content-Type': 'application/sdp',
    },
  });
  if (!response.ok) {
    throw new Error('Could not start voice dictation.');
  }
  return response.text();
}

function parseRealtimeEvent(value: unknown): OpenAIRealtimeEvent | null {
  if (typeof value !== 'string') return null;
  try {
    const parsed = JSON.parse(value) as OpenAIRealtimeEvent;
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

async function waitForTranscriptCompletion(register: (resolve: () => void) => void) {
  let timeout: ReturnType<typeof window.setTimeout> | null = null;
  await Promise.race([
    new Promise<void>((resolve) => {
      register(resolve);
    }),
    new Promise<void>((resolve) => {
      timeout = window.setTimeout(resolve, 2500);
    }),
  ]);
  if (timeout) window.clearTimeout(timeout);
}
