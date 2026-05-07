from __future__ import annotations

from dataclasses import dataclass

from .schemas import RuntimeOption


@dataclass(frozen=True)
class EmbeddingModelSpec:
    key: str
    provider: str
    label: str
    description: str
    dimensions: int


@dataclass(frozen=True)
class EmbedderSpec:
    key: str
    label: str
    description: str
    backend: str
    provider: str | None
    default_model: str | None
    dimensions: int


EMBEDDING_MODEL_SPECS: dict[str, EmbeddingModelSpec] = {
    "text-embedding-3-small": EmbeddingModelSpec(
        key="text-embedding-3-small",
        provider="openai",
        label="text-embedding-3-small",
        description="Fast OpenAI embedder.",
        dimensions=768,
    ),
    "text-embedding-3-large": EmbeddingModelSpec(
        key="text-embedding-3-large",
        provider="openai",
        label="text-embedding-3-large",
        description="Higher quality OpenAI embedder.",
        dimensions=768,
    ),
    "gemini-embedding-2": EmbeddingModelSpec(
        key="gemini-embedding-2",
        provider="gemini",
        label="gemini-embedding-2",
        description="Google Gemini Embedding 2 at 768 dimensions.",
        dimensions=768,
    ),
}

EMBEDDER_SPECS: dict[str, EmbedderSpec] = {
    "openai": EmbedderSpec(
        key="openai",
        label="OpenAI",
        description="Use the OpenAI embedding API.",
        backend="api",
        provider="openai",
        default_model="text-embedding-3-small",
        dimensions=768,
    ),
    "gemini": EmbedderSpec(
        key="gemini",
        label="Gemini",
        description="Use Google Gemini Embedding 2.",
        backend="api",
        provider="gemini",
        default_model="gemini-embedding-2",
        dimensions=768,
    ),
    "local_cpu": EmbedderSpec(
        key="local_cpu",
        label="Local CPU",
        description="Use the local CPU embedding model.",
        backend="cpu",
        provider=None,
        default_model=None,
        dimensions=384,
    ),
    "local_gpu": EmbedderSpec(
        key="local_gpu",
        label="Local GPU",
        description="Use the local GPU embedding worker.",
        backend="gpu",
        provider=None,
        default_model=None,
        dimensions=2000,
    ),
}

EMBEDDER_ORDER = ("openai", "gemini", "local_cpu", "local_gpu")
EMBEDDING_MODEL_ORDER = ("text-embedding-3-small", "text-embedding-3-large", "gemini-embedding-2")


def embedder_options() -> list[RuntimeOption]:
    return [
        RuntimeOption(key=spec.key, label=spec.label, description=spec.description)
        for spec in (EMBEDDER_SPECS[key] for key in EMBEDDER_ORDER)
    ]


def embedding_model_options() -> list[RuntimeOption]:
    return [
        RuntimeOption(
            key=spec.key,
            label=spec.label,
            description=spec.description,
            group=spec.provider,
        )
        for spec in (EMBEDDING_MODEL_SPECS[key] for key in EMBEDDING_MODEL_ORDER)
    ]


def default_embedding_model(embedder: str) -> str | None:
    spec = EMBEDDER_SPECS.get(embedder)
    return spec.default_model if spec else None


def embedding_model_supported(embedder: str, model: str | None) -> bool:
    spec = EMBEDDER_SPECS.get(embedder)
    if not spec or not spec.provider or not model:
        return True
    model_spec = EMBEDDING_MODEL_SPECS.get(model)
    return bool(model_spec and model_spec.provider == spec.provider)


def embedding_dimensions(embedder: str, model: str | None = None) -> int:
    model_spec = EMBEDDING_MODEL_SPECS.get(model or "")
    if model_spec:
        return model_spec.dimensions
    spec = EMBEDDER_SPECS.get(embedder)
    return spec.dimensions if spec else 768
