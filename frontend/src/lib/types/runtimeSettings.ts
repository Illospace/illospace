export type ModelTier = 'low' | 'medium' | 'high';
export type EmbedderKey = 'local_gpu' | 'local_cpu' | 'openai' | 'gemini';
export type RuntimeVoiceProvider = 'openai';
export type RuntimeVoiceLanguage = 'auto' | 'en' | 'fr';

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
  voice: RuntimeVoiceSettings;
  permissions: {
    can_manage_settings: boolean;
  };
}

export interface RuntimeVoiceSettings {
  provider: RuntimeVoiceProvider;
  model: string;
  source: 'memory';
  language: RuntimeVoiceLanguage;
  status: 'ready' | 'missing' | 'error';
  detail?: string | null;
  provider_options: RuntimeOption[];
  language_options: RuntimeOption[];
}

export interface RuntimeVoiceSession {
  provider: RuntimeVoiceProvider;
  model: string;
  language: RuntimeVoiceLanguage;
  client_secret: string;
  expires_at?: number | null;
}
