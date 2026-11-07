import type {
  EmbedderKey,
  RuntimeVoiceLanguage,
  RuntimeVoiceModelSize,
  RuntimeVoiceProvider,
} from '$lib/types/runtimeSettings';

export type {
  EmbedderKey,
  RuntimeOption,
  RuntimeModelCatalogEntry,
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

export type { RuntimeHealthState, RuntimeStatusSnapshot } from '$lib/types/runtimeStatus';

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


export interface ExternalMcpConfig {
  name: string;
  url: string;
  transport: 'streamable_http';
  auth_mode: 'none' | 'bearer' | 'client_credentials';
  bearer_token_key: string | null;
  token_url: string | null;
  client_id_key: string | null;
  client_secret_key: string | null;
  scope: string | null;
  resource: string | null;
  allowed_tools: string[];
  enabled: boolean;
  timeout_seconds: number;
}

export interface ExternalMcpServer extends ExternalMcpConfig {
  id: number;
  created_at: string;
  updated_at: string;
  missing_secret_keys: string[];
}

export interface ExternalMcpTestResult {
  ok: boolean;
  tools: { name: string; description: string; allowed: boolean }[];
  error?: string;
}
