"""Canonical provider-aware model catalog.

This dependency-light module owns the concrete models Illospace can select
and the provider facts attached to them. Routing and UI surfaces consume
derived views instead of maintaining their own model inventories.
"""

from __future__ import annotations

from dataclasses import dataclass

from brain.platform.effort import EFFORT_TIERS


@dataclass(frozen=True)
class ModelCatalogEntry:
    """Provider facts for one selectable model."""

    id: str
    label: str
    provider: str
    description: str
    supported_effort_tiers: tuple[str, ...]
    availability_fallback: str | None
    context_window_tokens: int
    input_price_per_million: float
    output_price_per_million: float
    provider_default: bool = False

    @property
    def model_name(self) -> str:
        return self.id.split("/", 1)[1]


_ALL_EFFORT_TIERS = EFFORT_TIERS
_NO_NATIVE_EFFORT = ("none",)

# These providers run locally and need no credentials.
CREDENTIAL_FREE_PROVIDERS: frozenset[str] = frozenset({"ollama"})

MODEL_CATALOG: tuple[ModelCatalogEntry, ...] = (
    ModelCatalogEntry(
        id="openai/gpt-5.6-sol",
        label="GPT-5.6 Sol",
        provider="openai",
        description="Organization default; falls back to GPT-5.5 when unavailable.",
        supported_effort_tiers=_ALL_EFFORT_TIERS,
        availability_fallback="openai/gpt-5.5",
        # https://developers.openai.com/api/docs/models/gpt-5.6-sol
        # Provider contract: 1,050,000 total tokens (922,000 input + 128,000 output).
        context_window_tokens=1_050_000,
        input_price_per_million=5.0,
        output_price_per_million=30.0,
        provider_default=True,
    ),
    ModelCatalogEntry(
        id="openai/gpt-5.6-luna",
        label="GPT-5.6 Luna",
        provider="openai",
        description=(
            "Bulk/execution lane; cheap high-volume work. Falls back to GPT-5.6 Sol "
            "when unavailable."
        ),
        supported_effort_tiers=_ALL_EFFORT_TIERS,
        availability_fallback="openai/gpt-5.6-sol",
        context_window_tokens=1_050_000,
        input_price_per_million=0.20,
        output_price_per_million=1.20,
    ),
    ModelCatalogEntry(
        id="openai/gpt-5.5",
        label="GPT-5.5",
        provider="openai",
        description=(
            "Availability fallback for GPT-5.6 Sol; not user-selectable policy — "
            "subscription-metered emergency fallback only."
        ),
        supported_effort_tiers=_ALL_EFFORT_TIERS,
        availability_fallback=None,
        context_window_tokens=400_000,
        input_price_per_million=5.0,
        output_price_per_million=30.0,
    ),
    ModelCatalogEntry(
        id="anthropic/claude-sonnet-5",
        label="Claude Sonnet 5",
        provider="anthropic",
        description="Anthropic default with a 1M-token context window.",
        supported_effort_tiers=_ALL_EFFORT_TIERS,
        availability_fallback=None,
        context_window_tokens=1_000_000,
        input_price_per_million=3.0,
        output_price_per_million=15.0,
        provider_default=True,
    ),
    ModelCatalogEntry(
        id="anthropic/claude-opus-5",
        label="Claude Opus 5",
        provider="anthropic",
        description="Anthropic's complex agentic and coding model.",
        supported_effort_tiers=_ALL_EFFORT_TIERS,
        availability_fallback=None,
        context_window_tokens=1_000_000,
        input_price_per_million=5.0,
        output_price_per_million=25.0,
    ),
    ModelCatalogEntry(
        id="anthropic/claude-fable-5",
        label="Claude Fable 5",
        provider="anthropic",
        description="Anthropic's long-running agent model.",
        supported_effort_tiers=_ALL_EFFORT_TIERS,
        availability_fallback=None,
        context_window_tokens=1_000_000,
        input_price_per_million=10.0,
        output_price_per_million=50.0,
    ),
    ModelCatalogEntry(
        id="anthropic/claude-haiku-4-5",
        label="Claude Haiku 4.5",
        provider="anthropic",
        description="Anthropic's fastest model; it does not accept native effort.",
        supported_effort_tiers=_NO_NATIVE_EFFORT,
        availability_fallback=None,
        context_window_tokens=200_000,
        input_price_per_million=1.0,
        output_price_per_million=5.0,
    ),
    ModelCatalogEntry(
        id="ollama/qwen3.6-27b",
        label="Qwen3.6 27B (local)",
        provider="ollama",
        description=(
            "Free local lane on illo-dev's RTX 5090; zero marginal cost and unlimited "
            "volume. Quality is well below Luna; use only for high-volume, low-stakes "
            "single-shot work such as heartbeat-class probes, classification, and "
            "summarization. Never use it for judgment, review, or long context. The model "
            "always emits a reasoning block, so short output budgets are raised automatically."
        ),
        supported_effort_tiers=_NO_NATIVE_EFFORT,
        availability_fallback="openai/gpt-5.6-luna",
        context_window_tokens=65_536,
        input_price_per_million=0.0,
        output_price_per_million=0.0,
        provider_default=True,
    ),
)

MODEL_CATALOG_BY_ID = {entry.id: entry for entry in MODEL_CATALOG}
_AVAILABILITY_FALLBACK_ALIASES = {
    "openai/gpt-5.6": "openai/gpt-5.5",
}


def canonical_catalog_model_id(model: str | None) -> str | None:
    """Return the canonical catalog id for a prefixed or unique bare model."""

    value = str(model or "").strip().lower().replace(":", "/", 1)
    if not value:
        return None
    if value in MODEL_CATALOG_BY_ID:
        return value
    if "/" in value:
        return None
    matches = [entry.id for entry in MODEL_CATALOG if entry.model_name == value]
    return matches[0] if len(matches) == 1 else None


def get_model_catalog_entry(model: str | None) -> ModelCatalogEntry | None:
    """Look up one model without applying a routing fallback."""

    canonical_id = canonical_catalog_model_id(model)
    return MODEL_CATALOG_BY_ID.get(canonical_id) if canonical_id else None


def model_accepts_effort(model: str | None, tier: str | None) -> bool:
    """Return whether a known model accepts the canonical effort tier.

    Unknown provider models retain the historical pass-through behavior so
    custom integrations are not silently stripped of effort configuration.
    """

    normalized_tier = str(tier or "").strip().lower()
    if not normalized_tier or normalized_tier == "none":
        return False
    entry = get_model_catalog_entry(model)
    return entry is None or normalized_tier in entry.supported_effort_tiers


def availability_fallback_for(model: str | None) -> str | None:
    """Return the cataloged availability fallback for a model."""

    entry = get_model_catalog_entry(model)
    if entry:
        return entry.availability_fallback
    value = str(model or "").strip().lower().replace(":", "/", 1)
    if "/" not in value:
        value = f"openai/{value}"
    return _AVAILABILITY_FALLBACK_ALIASES.get(value)
