from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ConnectionStatus = Literal["connected", "missing", "error"]
EmbedderKey = Literal["local_gpu", "local_cpu", "openai", "gemini"]
RerankerKey = Literal["weighted"]
RuntimeUpdateStatus = Literal["idle", "running"]
RuntimeServiceStatus = Literal["idle", "running"]
WorkspaceToolStatus = Literal["requested", "queued", "installing", "installed", "failed", "removed"]
WorkspaceToolQueueStatus = Literal["idle", "running"]
VoiceProviderKey = Literal["openai", "gemini", "local"]
VoiceLanguageKey = Literal["auto", "en", "fr"]
VoiceModelSizeKey = Literal["tiny", "base", "small"]
VoiceStatus = Literal["ready", "missing", "error"]


class RuntimeOption(BaseModel):
    key: str
    label: str
    description: str | None = None
    disabled: bool = False
    group: str | None = None


class RuntimeModelDefaultProvenance(BaseModel):
    provider_default: bool = False
    workspace_default: bool = False


class RuntimeModelCatalogEntry(BaseModel):
    id: str
    label: str
    provider: Literal["openai", "anthropic"]
    description: str
    supported_effort_tiers: list[Literal["none", "low", "medium", "high", "xhigh"]]
    auth_requirement: Literal["chatgpt", "api_key"]
    availability_fallback: str | None = None
    default_provenance: RuntimeModelDefaultProvenance


class RuntimeConnectionRead(BaseModel):
    status: ConnectionStatus
    setup_required: bool
    method: str | None = None
    source: str | None = None
    label: str | None = None
    detail: str | None = None
    has_personal_connection: bool = False
    has_org_key: bool = False


class RuntimeModelsRead(BaseModel):
    default: str
    thinking: Literal["none", "low", "medium", "high", "xhigh"] = "high"
    catalog: list[RuntimeModelCatalogEntry]
    thinking_options: list[RuntimeOption] = Field(default_factory=list)


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
    model_size: VoiceModelSizeKey = "base"
    status: VoiceStatus
    detail: str | None = None
    provider_options: list[RuntimeOption] = Field(default_factory=list)
    language_options: list[RuntimeOption] = Field(default_factory=list)
    model_size_options: list[RuntimeOption] = Field(default_factory=list)


class RuntimeVoiceSessionRead(BaseModel):
    provider: VoiceProviderKey
    model: str
    language: VoiceLanguageKey = "auto"
    client_secret: str
    expires_at: int | None = None


class RuntimeVoiceTranscriptRead(BaseModel):
    transcript: str
    provider: str
    model: str
    language: str
    transport: str
    bytes_streamed: int = 0


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


class WorkspaceToolBundleRead(BaseModel):
    id: str
    name: str
    description: str
    version: str | None = None
    provided_commands: list[str] = Field(default_factory=list)
    skill_dependencies: list[str] = Field(default_factory=list)
    install_profile: str | None = None
    optional: bool = False
    metadata: dict = Field(default_factory=dict)
    runtime: dict = Field(default_factory=dict)


class WorkspaceToolUserConfigRead(BaseModel):
    id: str | None = None
    org_id: str
    user_id: str
    bundle_id: str
    preferences: dict = Field(default_factory=dict)
    credential_refs: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkspaceToolInstallationRead(BaseModel):
    id: str | None = None
    bundle_id: str
    display_name: str
    version: str | None = None
    status: WorkspaceToolStatus
    install_root: str | None = None
    bin_path: str | None = None
    requested_by_user_id: str | None = None
    requested_at: datetime | None = None
    installed_at: datetime | None = None
    checked_at: datetime | None = None
    last_error: str | None = None
    health: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


class WorkspaceToolsRead(BaseModel):
    status: WorkspaceToolQueueStatus
    available: bool
    catalog: list[WorkspaceToolBundleRead]
    installations: list[WorkspaceToolInstallationRead] = Field(default_factory=list)
    requested_bundle_id: str | None = None
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
    default: str = Field(min_length=1)
    thinking: Literal["none", "low", "medium", "high", "xhigh"] | None = None


class RuntimeMemoryUpdate(BaseModel):
    embedder: EmbedderKey
    embedding_model: str | None = None
    reranker: RerankerKey = "weighted"


class RuntimeVoiceUpdate(BaseModel):
    provider: VoiceProviderKey = "openai"
    language: VoiceLanguageKey = "auto"
    model_size: VoiceModelSizeKey = "base"
