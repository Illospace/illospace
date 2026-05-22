from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from brain.systems.memory.embedding_service import (
    EmbeddingCredentialsUnavailable,
    EmbeddingService,
)
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig


def _runtime_config(*, api_key: str = "secret") -> EmbeddingRuntimeConfig:
    return EmbeddingRuntimeConfig(
        backend="api",
        provider="gemini",
        api_model="gemini-embedding-2",
        cpu_model="all-MiniLM-L6-v2",
        dimensions=768,
        api_key=api_key,
    )


async def test_embedding_service_loads_db_backed_runtime_config():
    runtime_config = _runtime_config()

    with patch(
        "brain.systems.memory.embedding_service.async_get_embedding_runtime_config",
        new=AsyncMock(return_value=runtime_config),
    ) as load_config:
        service = await EmbeddingService.from_session(object())

    assert service.runtime_config is runtime_config
    load_config.assert_awaited_once()


def test_embedding_service_rejects_missing_api_credentials_before_provider_call():
    service = EmbeddingService(runtime_config=_runtime_config(api_key=""))

    with pytest.raises(EmbeddingCredentialsUnavailable, match="Gemini embedding credentials"):
        service.query("hello")
