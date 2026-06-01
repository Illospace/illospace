from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ModelTier = Literal["low", "medium", "high"]
ConnectionStatus = Literal["connected", "missing", "error"]
EmbedderKey = Literal["local_gpu", "local_cpu", "openai", "gemini"]
RerankerKey = Literal["weighted"]
RuntimeUpdateStatus = Literal["idle", "running"]
RuntimeServiceStatus = Literal["idle", "running"]
VoiceProviderKey = Literal["openai", "gemini"]
VoiceLanguageKey = Literal["auto", "en", "fr"]
VoiceStatus = Literal["ready", "missing", "error"]


class RuntimeOption(BaseModel):
    key: str
    label: str
    description: str | None = None
    disabled: bool = False
    group: str | None = None


class RuntimeConnectionRead(BaseModel):
    status: ConnectionStatus
    setup_required: bool
    method: str | None = None
    source: str | None = None
    label: str | None = None
    detail: str | None = None


class RuntimeModelsRead(BaseModel):
    low: str
    medium: str
    high: str
    options: list[RuntimeOption]


class RuntimeMemoryRead(BaseModel):
    scope: Literal["installation"] = "installation"
    embedder: EmbedderKey
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    embedding_status: str
    embedding_detail: str | None = None
    indexed_vectors: int = 0
    api_key_statuses: dict[str, bool] = Field(default_factory=dict)
    reranker: RerankerKey = "weighted"
    embedder_options: list[RuntimeOption]
    embedding_model_options: list[RuntimeOption]
    reranker_options: list[RuntimeOption]


class RuntimeMemoryCheckRead(BaseModel):
    status: Literal["ok", "error"]
    detail: str
    dimensions: int | None = None
    duration_ms: int | None = None


class RuntimeVoiceRead(BaseModel):
    provider: VoiceProviderKey
    model: str
    source: Literal["memory"] = "memory"
    language: VoiceLanguageKey = "auto"
    status: VoiceStatus
    detail: str | None = None
    provider_options: list[RuntimeOption] = Field(default_factory=list)
    language_options: list[RuntimeOption] = Field(default_factory=list)


class RuntimeVoiceSessionRead(BaseModel):
    provider: VoiceProviderKey
    model: str
    language: VoiceLanguageKey = "auto"
    client_secret: str
    expires_at: int | None = None


class RuntimePermissionsRead(BaseModel):
    can_manage_settings: bool


class RuntimeUpdateRead(BaseModel):
    status: RuntimeUpdateStatus
    available: bool
    pid: int | None = None
    started_at: datetime | None = None
    active_agent_runs: int = 0
    log_path: str | None = None
    detail: str | None = None


class RuntimeServiceRead(BaseModel):
    id: str
    name: str
    description: str
    restartable: bool = True
    optional: bool = False


class RuntimeServicesRead(BaseModel):
    status: RuntimeServiceStatus
    available: bool
    services: list[RuntimeServiceRead]
    requested_services: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    log_path: str | None = None
    detail: str | None = None


class RuntimeSettingsRead(BaseModel):
    connection: RuntimeConnectionRead
    models: RuntimeModelsRead
    memory: RuntimeMemoryRead
    voice: RuntimeVoiceRead
    permissions: RuntimePermissionsRead


class OpenAIKeyConnectRequest(BaseModel):
    api_key: str = Field(min_length=1)


class OpenAIOAuthExchangeRequest(BaseModel):
    callback: str = Field(min_length=1)


class OpenAIOAuthStartResponse(BaseModel):
    url: str = Field(min_length=1)
    state: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)
    expires_in_seconds: int
    callback_available: bool = True
    callback_detail: str | None = None
    callback_mode: Literal["server", "local_bridge", "manual"] = "local_bridge"


class RuntimeModelsUpdate(BaseModel):
    low: str = Field(min_length=1)
    medium: str = Field(min_length=1)
    high: str = Field(min_length=1)


class RuntimeMemoryUpdate(BaseModel):
    embedder: EmbedderKey
    embedding_model: str | None = None
    reranker: RerankerKey = "weighted"


class RuntimeVoiceUpdate(BaseModel):
    provider: VoiceProviderKey = "openai"
    language: VoiceLanguageKey = "auto"
