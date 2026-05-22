"""DB-backed embedding service for async server and job paths."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.memory import embeddings as embedding_backend
from brain.systems.runtime_settings.memory import (
    EmbeddingRuntimeConfig,
    async_get_embedding_runtime_config,
    get_embedding_runtime_config,
)

EmbeddingMode = Literal["document", "query"]


class EmbeddingServiceError(RuntimeError):
    """Base error for DB-backed embedding service failures."""


class EmbeddingConfigUnavailable(EmbeddingServiceError):
    """Runtime embedding settings could not be loaded or are invalid."""


class EmbeddingCredentialsUnavailable(EmbeddingServiceError):
    """The selected API embedding provider has no usable credentials."""


class EmbeddingProviderUnavailable(EmbeddingServiceError):
    """The selected embedding provider failed the request."""


@dataclass(frozen=True)
class EmbeddingService:
    """Small seam for embedding calls that must use DB-backed runtime settings."""

    runtime_config: EmbeddingRuntimeConfig

    @classmethod
    async def from_session(cls, session: AsyncSession) -> "EmbeddingService":
        try:
            runtime_config = await async_get_embedding_runtime_config(session, include_secret=True)
        except Exception as exc:
            raise EmbeddingConfigUnavailable(
                "Embedding runtime config could not be loaded. Check runtime memory settings."
            ) from exc
        return cls(runtime_config=runtime_config)

    @classmethod
    def from_legacy_sync_config(cls) -> "EmbeddingService":
        """Build from process defaults for legacy sync callers and CLIs."""
        return cls(runtime_config=get_embedding_runtime_config(include_secret=True))

    @property
    def provider(self) -> str:
        return (self.runtime_config.provider or "").lower()

    def query(self, text: str) -> np.ndarray:
        self._ensure_ready()
        try:
            return embedding_backend.embed_query(text, runtime_config=self.runtime_config)
        except EmbeddingServiceError:
            raise
        except RuntimeError as exc:
            raise self._typed_provider_error(exc) from exc

    def document(self, text: str) -> np.ndarray:
        self._ensure_ready()
        try:
            return embedding_backend.embed_document(text, runtime_config=self.runtime_config)
        except EmbeddingServiceError:
            raise
        except RuntimeError as exc:
            raise self._typed_provider_error(exc) from exc

    def batch(self, texts: list[str], *, mode: EmbeddingMode = "document") -> np.ndarray:
        self._ensure_ready()
        try:
            return embedding_backend.embed_batch(texts, mode=mode, runtime_config=self.runtime_config)
        except EmbeddingServiceError:
            raise
        except RuntimeError as exc:
            raise self._typed_provider_error(exc) from exc

    def _ensure_ready(self) -> None:
        backend = (self.runtime_config.backend or "").lower()
        if backend not in {"gpu", "cpu", "api"}:
            raise EmbeddingConfigUnavailable(
                f"Unknown embedding backend {self.runtime_config.backend!r}. Use gpu, cpu, or api."
            )
        if backend == "api" and not self.runtime_config.api_key:
            raise EmbeddingCredentialsUnavailable(_credential_message(self.provider))

    def _typed_provider_error(self, exc: RuntimeError) -> EmbeddingServiceError:
        detail = str(exc)
        lower = detail.lower()
        if "credentials are not configured" in lower or "embedding_api_key" in lower:
            return EmbeddingCredentialsUnavailable(_credential_message(self.provider))
        return EmbeddingProviderUnavailable(detail)


def _credential_message(provider: str) -> str:
    if provider in {"gemini", "google"}:
        return "Gemini embedding credentials are not configured. Add them in System/Access."
    if provider == "openai":
        return "OpenAI embedding credentials are not configured. Add them in System/Access."
    return "Embedding credentials are not configured. Add them in System/Access."


def embedding_degradation_reason(exc: Exception) -> str:
    """Return a stable degradation reason for embedding failures."""
    error_text = str(exc)
    if isinstance(exc, EmbeddingCredentialsUnavailable):
        return f"embedding_credentials_unavailable: {error_text[:200]}"
    if isinstance(exc, EmbeddingConfigUnavailable):
        return f"embedding_config_unavailable: {error_text[:200]}"
    if isinstance(exc, EmbeddingProviderUnavailable):
        return f"embedding_provider_unavailable: {error_text[:200]}"

    lower = error_text.lower()
    if any(term in lower for term in ("out of memory", "oom", "cuda")):
        return f"embedding_oom: {error_text[:200]}"
    return f"embedding_failed: {error_text[:200]}"
