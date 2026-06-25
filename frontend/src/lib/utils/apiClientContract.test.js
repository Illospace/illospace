import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const apiClientSource = readFileSync(
  new URL('../api/client.ts', import.meta.url),
  'utf8',
);

test('local voice transcription uses a long request timeout', () => {
  assert.match(apiClientSource, /const VOICE_TRANSCRIPTION_TIMEOUT_MS = 5 \* 60_000;/);

  const methodSource = apiClientSource.split('transcribeRuntimeVoiceClip:', 2)[1]?.split('\n  },', 1)[0] ?? '';
  assert.match(methodSource, /timeoutMs:\s*VOICE_TRANSCRIPTION_TIMEOUT_MS/);
});
