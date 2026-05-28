import type {
  VoiceDictationConnection,
  VoiceDictationSession,
  VoiceDictationTransportHandlers,
} from '$lib/features/composer/domain/voiceDictation';

const OPENAI_REALTIME_CALLS_URL = 'https://api.openai.com/v1/realtime/calls';
const AUDIO_LEVEL_SAMPLE_MS = 80;
const AUDIO_LEVEL_GAIN = 6.5;

type AudioContextWindow = Window & {
  webkitAudioContext?: typeof AudioContext;
};

type AudioLevelMonitor = {
  stop: () => void;
};

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
  let levelMonitor: AudioLevelMonitor | null = null;
  let transcriptCompleted: (() => void) | null = null;
  let closed = false;

  function cleanup() {
    if (closed) return;
    closed = true;
    transcriptCompleted = null;
    levelMonitor?.stop();
    for (const track of stream.getTracks()) {
      track.stop();
    }
    if (dc.readyState !== 'closed') dc.close();
    pc.close();
  }

  try {
    levelMonitor = createAudioLevelMonitor(stream, handlers.onAudioLevel);
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

function createAudioLevelMonitor(
  stream: MediaStream,
  onAudioLevel?: (level: number) => void,
): AudioLevelMonitor | null {
  if (!onAudioLevel || typeof window === 'undefined') return null;
  const AudioContextCtor = window.AudioContext || (window as AudioContextWindow).webkitAudioContext;
  if (!AudioContextCtor) return null;

  const audioContext = new AudioContextCtor();
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0.7;

  const source = audioContext.createMediaStreamSource(stream);
  const samples = new Uint8Array(analyser.fftSize);
  let interval: ReturnType<typeof window.setInterval> | null = null;
  let stopped = false;

  source.connect(analyser);
  if (audioContext.state === 'suspended') {
    void audioContext.resume().catch(() => {});
  }

  const sample = () => {
    if (stopped) return;
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (const value of samples) {
      const centered = (value - 128) / 128;
      sum += centered * centered;
    }
    const rms = Math.sqrt(sum / samples.length);
    onAudioLevel(Math.min(1, rms * AUDIO_LEVEL_GAIN));
  };

  sample();
  interval = window.setInterval(sample, AUDIO_LEVEL_SAMPLE_MS);

  return {
    stop() {
      if (stopped) return;
      stopped = true;
      if (interval) window.clearInterval(interval);
      onAudioLevel(0);
      source.disconnect();
      try {
        analyser.disconnect();
      } catch {
        // Some browsers throw when disconnecting a node without downstream connections.
      }
      void audioContext.close().catch(() => {});
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
