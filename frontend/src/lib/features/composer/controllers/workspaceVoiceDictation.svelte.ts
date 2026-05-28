import { api } from '$lib/api/client';
import { createOpenAIRealtimeVoiceConnection } from '$lib/features/composer/controllers/openaiRealtimeVoice';
import {
  createVoiceDictationController,
  type VoiceDictationSnapshot,
} from '$lib/features/composer/domain/voiceDictation';
import type { RuntimeVoiceSettings } from '$lib/types/runtimeSettings';

type VoiceErrorReporter = (message: string) => void;

export interface WorkspaceVoiceDictationDeps {
  getDraft: () => string;
  setDraft: (value: string) => void;
  submit: () => Promise<void> | void;
  onError: VoiceErrorReporter;
  onSettled?: () => Promise<void> | void;
  focusDraft?: () => void;
}

function idleSnapshot(): VoiceDictationSnapshot {
  return { status: 'idle', transcript: '', error: null };
}

export class WorkspaceVoiceDictationController {
  settings = $state<RuntimeVoiceSettings | null>(null);
  snapshot = $state<VoiceDictationSnapshot>(idleSnapshot());
  elapsedMs = $state(0);

  private controller: ReturnType<typeof createVoiceDictationController> | null = null;
  private startedAt = 0;
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(private readonly deps: WorkspaceVoiceDictationDeps) {}

  get isRecording() {
    return this.snapshot.status === 'recording';
  }

  get isBusy() {
    return (
      this.snapshot.status === 'starting'
      || this.snapshot.status === 'recording'
      || this.snapshot.status === 'committing'
    );
  }

  get isReady() {
    return this.settings?.status === 'ready';
  }

  get controlLabel() {
    return this.isRecording ? 'Stop dictation' : 'Start dictation';
  }

  get controlTitle() {
    if (this.isReady) return this.controlLabel;
    return this.settings?.detail || 'Voice dictation needs an OpenAI API key in AI Runtime.';
  }

  get controlDisabled() {
    return !this.isReady || this.snapshot.status === 'starting' || this.snapshot.status === 'committing';
  }

  async loadSettings() {
    try {
      const runtime = await api.runtimeSettings();
      this.settings = runtime.voice ?? null;
    } catch {
      this.settings = null;
    }
  }

  toggle() {
    if (this.isRecording) {
      void this.stop();
      return;
    }
    void this.start();
  }

  async start() {
    if (!this.isReady) return;
    const controller = this.ensureController();
    await controller.start();
    this.refreshSnapshot();
    if (this.snapshot.status === 'recording') {
      this.startTimer();
      return;
    }
    this.reportCurrentError('Voice dictation could not start.');
  }

  async stop() {
    if (!this.controller || this.snapshot.status !== 'recording') return;
    await this.controller.stop();
    this.stopTimer();
    const snapshot = this.refreshSnapshot();
    await this.deps.onSettled?.();
    this.deps.focusDraft?.();
    if (snapshot.status === 'error') {
      this.reportCurrentError('Voice dictation could not finish.');
    }
  }

  async send() {
    if (!this.controller || this.snapshot.status !== 'recording') {
      await this.deps.submit();
      return;
    }
    await this.controller.send();
    this.stopTimer();
    const snapshot = this.refreshSnapshot();
    await this.deps.onSettled?.();
    if (snapshot.status === 'error') {
      this.reportCurrentError('Voice dictation could not finish.');
    }
  }

  destroy() {
    this.stopTimer();
  }

  private ensureController() {
    if (this.controller && this.snapshot.status !== 'error') return this.controller;
    this.controller = createVoiceDictationController({
      getDraft: this.deps.getDraft,
      setDraft: this.deps.setDraft,
      submit: this.deps.submit,
      createSession: () => api.createRuntimeVoiceSession(),
      connect: createOpenAIRealtimeVoiceConnection,
    });
    this.refreshSnapshot();
    return this.controller;
  }

  private refreshSnapshot() {
    this.snapshot = this.controller?.snapshot() ?? idleSnapshot();
    return this.snapshot;
  }

  private startTimer() {
    this.stopTimer();
    this.startedAt = Date.now();
    this.elapsedMs = 0;
    this.timer = window.setInterval(() => {
      this.elapsedMs = Math.max(0, Date.now() - this.startedAt);
    }, 250);
  }

  private stopTimer() {
    if (this.timer) {
      window.clearInterval(this.timer);
      this.timer = null;
    }
    this.startedAt = 0;
    this.elapsedMs = 0;
  }

  private reportCurrentError(fallback: string) {
    this.deps.onError(this.snapshot.error || fallback);
  }
}
