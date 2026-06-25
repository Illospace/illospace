import type {
  EmbedderKey,
  RuntimeVoiceLanguage,
  RuntimeVoiceModelSize,
  RuntimeVoiceProvider,
} from '$lib/types/runtimeSettings';

export type {
  EmbedderKey,
  RuntimeOption,
  RuntimeSettings,
  RuntimeVoiceLanguage,
  RuntimeVoiceModelSize,
  RuntimeVoiceProvider,
  RuntimeVoiceSession,
  RuntimeVoiceSettings,
} from '$lib/types/runtimeSettings';

export type PillTone = 'muted' | 'warning' | 'success' | 'danger' | 'info';

export interface RuntimeUpdateStatus {
  status: 'idle' | 'running';
  available: boolean;
  pid?: number | null;
  started_at?: string | null;
  active_agent_runs: number;
  log_path?: string | null;
  detail?: string | null;
}

export interface MemoryCheck {
  status: 'ok' | 'error';
  detail: string;
  dimensions?: number | null;
  duration_ms?: number | null;
}

export interface MemoryDraft {
  embedder: EmbedderKey;
  embedding_model: string;
  reranker: string;
}

export interface VoiceDraft {
  provider: RuntimeVoiceProvider;
  language: RuntimeVoiceLanguage;
  model_size: RuntimeVoiceModelSize;
}

export interface NoticeState {
  tone: 'success' | 'warning' | 'danger' | 'info';
  title: string;
  detail?: string;
}

export interface MemoryNoticeState {
  tone: 'info' | 'warning' | 'danger' | 'success';
  title: string;
  detail?: string;
}
