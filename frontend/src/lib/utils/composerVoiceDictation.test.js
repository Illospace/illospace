import test from 'node:test';
import assert from 'node:assert/strict';

import {
  appendVoiceTranscriptDraft,
  createVoiceDictationController,
} from '../features/composer/domain/voiceDictation.ts';
import {
  appendVoiceLevel,
  voiceLevelToBarHeight,
} from '../features/composer/domain/voiceLevels.ts';

test('voice dictation appends transcript to the end of an existing draft with smart spacing', () => {
  assert.equal(
    appendVoiceTranscriptDraft('Please update the colors', 'and make the buttons calmer.'),
    'Please update the colors and make the buttons calmer.',
  );
  assert.equal(
    appendVoiceTranscriptDraft('Please update the colors ', 'and make the buttons calmer.'),
    'Please update the colors and make the buttons calmer.',
  );
  assert.equal(
    appendVoiceTranscriptDraft('', 'Create a launch checklist.'),
    'Create a launch checklist.',
  );
});

test('voice dictation stop commits hidden transcript without submitting', async () => {
  let draft = 'Please update';
  let submitted = false;
  let activeHandlers;

  const controller = createVoiceDictationController({
    getDraft: () => draft,
    setDraft: (next) => {
      draft = next;
    },
    submit: async () => {
      submitted = true;
    },
    createSession: async () => ({ client_secret: 'ek_test', model: 'gpt-realtime-whisper' }),
    connect: async (_session, handlers) => {
      activeHandlers = handlers;
      return {
        stop: async () => {},
      };
    },
  });

  await controller.start();
  activeHandlers.onTranscriptDelta('the UI.');
  await controller.stop();

  assert.equal(draft, 'Please update the UI.');
  assert.equal(submitted, false);
  assert.equal(controller.snapshot().status, 'idle');
});

test('voice dictation send commits hidden transcript before submitting', async () => {
  let draft = 'Please update';
  let submittedDraft = '';
  let activeHandlers;

  const controller = createVoiceDictationController({
    getDraft: () => draft,
    setDraft: (next) => {
      draft = next;
    },
    submit: async () => {
      submittedDraft = draft;
    },
    createSession: async () => ({ client_secret: 'ek_test', model: 'gpt-realtime-whisper' }),
    connect: async (_session, handlers) => {
      activeHandlers = handlers;
      return {
        stop: async () => {},
      };
    },
  });

  await controller.start();
  activeHandlers.onTranscriptDelta('the UI.');
  await controller.send();

  assert.equal(draft, 'Please update the UI.');
  assert.equal(submittedDraft, 'Please update the UI.');
  assert.equal(controller.snapshot().status, 'idle');
});

test('voice dictation stop leaves recoverable error state when transport cleanup fails', async () => {
  let draft = 'Please update';
  let activeHandlers;

  const controller = createVoiceDictationController({
    getDraft: () => draft,
    setDraft: (next) => {
      draft = next;
    },
    submit: async () => {},
    createSession: async () => ({ client_secret: 'ek_test', model: 'gpt-realtime-whisper' }),
    connect: async (_session, handlers) => {
      activeHandlers = handlers;
      return {
        stop: async () => {
          throw new Error('Could not commit audio.');
        },
      };
    },
  });

  await controller.start();
  activeHandlers.onTranscriptDelta('the UI.');
  await controller.stop();

  assert.equal(draft, 'Please update the UI.');
  assert.equal(controller.snapshot().status, 'error');
  assert.equal(controller.snapshot().error, 'Could not commit audio.');
});

test('voice level history clamps samples and keeps the latest values', () => {
  const history = [0.1, 0.2];
  assert.deepEqual(appendVoiceLevel(history, 2, 3), [0.1, 0.2, 1]);
  assert.deepEqual(appendVoiceLevel([0.1, 0.2, 0.3], -1, 3), [0.2, 0.3, 0]);
  assert.deepEqual(appendVoiceLevel([0.1, 0.2, 0.3], 0.4, 1), [0.4]);
});

test('voice level bar heights grow with louder input', () => {
  const silence = voiceLevelToBarHeight(0);
  const speech = voiceLevelToBarHeight(0.35);
  const loud = voiceLevelToBarHeight(1);

  assert.equal(silence, 3);
  assert.ok(speech > silence);
  assert.ok(loud > speech);
  assert.equal(loud, 24);
});
