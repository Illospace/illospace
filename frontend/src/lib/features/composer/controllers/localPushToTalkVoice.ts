import { api } from '$lib/api/client';
import type {
  VoiceDictationConnection,
  VoiceDictationSession,
  VoiceDictationTransportHandlers,
} from '$lib/features/composer/domain/voiceDictation';

import { createAudioLevelMonitor } from './openaiRealtimeVoice';

const RECORDER_MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
];

/**
 * Push-to-talk transport for the local (faster-whisper) voice provider.
 *
 * Unlike the OpenAI realtime transport, transcription happens server-side: we
 * record audio in the browser and, when the user stops, upload the clip to the
 * backend which runs faster-whisper and returns the transcript in one shot.
 * The dictation controller flow (start/stop/send) is identical — only the
 * transcript arrives on stop instead of streaming word by word.
 */
export async function createLocalPushToTalkVoiceConnection(
  _session: VoiceDictationSession,
  handlers: VoiceDictationTransportHandlers,
): Promise<VoiceDictationConnection> {
  if (typeof MediaRecorder === 'undefined') {
    throw new Error('Local voice dictation is not supported in this browser.');
  }

  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const mimeType = pickRecorderMimeType();
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const chunks: Blob[] = [];
  const levelMonitor = createAudioLevelMonitor(stream, handlers.onAudioLevel);
  let stopped = false;

  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) chunks.push(event.data);
  };

  const recordingStopped = new Promise<void>((resolve) => {
    recorder.onstop = () => resolve();
  });

  function cleanup() {
    levelMonitor?.stop();
    for (const track of stream.getTracks()) {
      track.stop();
    }
  }

  recorder.start();

  return {
    async stop() {
      if (stopped) {
        cleanup();
        return;
      }
      stopped = true;
      try {
        if (recorder.state !== 'inactive') {
          recorder.stop();
          await recordingStopped;
        }
        const blob = new Blob(chunks, { type: recorder.mimeType || mimeType || 'audio/webm' });
        if (blob.size > 0) {
          const result = await api.transcribeRuntimeVoiceClip(blob, recorderFilename(blob.type));
          if (result?.transcript) {
            handlers.onTranscriptCompleted?.(result.transcript);
          }
        }
      } finally {
        cleanup();
      }
    },
  };
}

function pickRecorderMimeType(): string | undefined {
  if (typeof MediaRecorder === 'undefined' || typeof MediaRecorder.isTypeSupported !== 'function') {
    return undefined;
  }
  for (const candidate of RECORDER_MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(candidate)) return candidate;
  }
  return undefined;
}

function recorderFilename(mimeType: string): string {
  if (mimeType.includes('ogg')) return 'voice-clip.ogg';
  if (mimeType.includes('mp4')) return 'voice-clip.mp4';
  return 'voice-clip.webm';
}
