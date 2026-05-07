export type ModelTier = 'low' | 'medium' | 'high';
export type EmbedderKey = 'local_gpu' | 'local_cpu' | 'openai' | 'gemini';
export type PillTone = 'muted' | 'warning' | 'success' | 'danger' | 'info';
export type StartupStepKey = 'access' | 'models' | 'memory';
export type StartupStepStatus = 'complete' | 'current' | 'pending' | 'blocked';

export interface RuntimeOption {
  key: string;
  label: string;
  description?: string | null;
  disabled?: boolean;
  group?: string | null;
}

export interface RuntimeSettings {
  connection: {
    status: 'connected' | 'missing' | 'error';
    setup_required: boolean;
    method?: string | null;
    source?: string | null;
    label?: string | null;
    detail?: string | null;
  };
  models: {
    low: string;
    medium: string;
    high: string;
    options: RuntimeOption[];
  };
  memory: {
    scope?: 'installation';
    embedder: EmbedderKey;
    embedding_model?: string | null;
    embedding_dimensions?: number | null;
    embedding_status: string;
    embedding_detail?: string | null;
    indexed_vectors: number;
    api_key_statuses?: Record<string, boolean>;
    reranker: string;
    embedder_options: RuntimeOption[];
    embedding_model_options: RuntimeOption[];
    reranker_options: RuntimeOption[];
  };
  permissions: {
    can_manage_settings: boolean;
  };
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

export interface NoticeState {
  tone: 'success' | 'warning' | 'danger' | 'info';
  title: string;
  detail?: string;
}

export interface MemoryNoticeState {
  tone: 'info' | 'warning' | 'danger' | 'success';
  title: string;
  detail: string;
  showAddKeyAction?: boolean;
}

export interface StartupGuideStep {
  key: StartupStepKey;
  title: string;
  detail: string;
  status: StartupStepStatus;
}
