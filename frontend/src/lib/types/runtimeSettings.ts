export type EmbedderKey = 'local_gpu' | 'local_cpu' | 'openai' | 'gemini';
export type RuntimeVoiceProvider = 'openai' | 'local' | 'gemini';
export type RuntimeVoiceLanguage = 'auto' | 'en' | 'fr';
export type RuntimeVoiceModelSize = 'tiny' | 'base' | 'small';
export type RuntimeThinking = 'none' | 'low' | 'medium' | 'high' | 'xhigh';

export interface RuntimeOption {
  key: string;
  label: string;
  description?: string | null;
  disabled?: boolean;
  group?: string | null;
}

export interface RuntimeModelCatalogEntry {
  id: string;
  label: string;
  provider: 'openai' | 'anthropic';
  description: string;
  supported_effort_tiers: RuntimeThinking[];
  auth_requirement: 'chatgpt' | 'api_key';
  availability_fallback?: string | null;
  default_provenance: {
    provider_default: boolean;
    workspace_default: boolean;
  };
}

export interface RuntimeSettings {
  connection: {
    status: 'connected' | 'missing' | 'error';
    setup_required: boolean;
    method?: string | null;
    source?: string | null;
    label?: string | null;
    detail?: string | null;
    has_personal_connection?: boolean;
    has_org_key?: boolean;
  };
  models: {
    default: string;
    thinking: RuntimeThinking;
    catalog: RuntimeModelCatalogEntry[];
    thinking_options: RuntimeOption[];
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
  display: {
    scope: 'installation';
    display_timezone: string;
  };
  permissions: {
    can_manage_settings: boolean;
  };
}

export interface RuntimeVoiceSettings {
  provider: RuntimeVoiceProvider;
  model: string;
  source: 'memory';
  language: RuntimeVoiceLanguage;
  model_size: RuntimeVoiceModelSize;
  status: 'ready' | 'missing' | 'error';
  detail?: string | null;
  provider_options: RuntimeOption[];
  language_options: RuntimeOption[];
  model_size_options: RuntimeOption[];
}

export interface RuntimeVoiceSession {
  provider: RuntimeVoiceProvider;
  model: string;
  language: RuntimeVoiceLanguage;
  client_secret: string;
  expires_at?: number | null;
}

export interface RuntimeVoiceTranscript {
  transcript: string;
  provider: string;
  model: string;
  language: string;
  transport: string;
  bytes_streamed: number;
}
