"""Model-aware context budget policy for agent transcript compaction."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from brain.platform.model_catalog import get_model_catalog_entry
from brain.platform.providers.model_policy import EFFORT_TIER_SET, infer_provider_from_model

DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000

_PROVIDER_CONTEXT_WINDOWS = {
    "anthropic": 200_000,
    "openai": 128_000,
}

_MODEL_PREFIX_CONTEXT_WINDOWS = (
    (("openai", "gpt-5.5"), 400_000),
    (("openai", "gpt-5.4-pro"), 400_000),
    (("openai", "gpt-5.4"), 256_000),
    (("openai", "gpt-5"), 128_000),
)

_REASONING_RESERVES = {
    "none": 0,
    "low": 4_096,
    "medium": 8_192,
    "high": 16_384,
    "xhigh": 24_576,
}
assert frozenset(_REASONING_RESERVES) == EFFORT_TIER_SET


@dataclass(frozen=True)
class ModelContextBudget:
    provider: str
    model: str
    context_window_tokens: int
    reserved_output_tokens: int
    reserved_reasoning_tokens: int
    reserved_tool_tokens: int
    safety_margin_tokens: int
    effective_input_limit_tokens: int
    auto_compact_threshold_tokens: int
    target_tokens: int
    emergency_target_tokens: int
    source: str = "model_context_budget_v1"

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "context_window_tokens": self.context_window_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "reserved_reasoning_tokens": self.reserved_reasoning_tokens,
            "reserved_tool_tokens": self.reserved_tool_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "effective_input_limit_tokens": self.effective_input_limit_tokens,
            "auto_compact_threshold_tokens": self.auto_compact_threshold_tokens,
            "target_tokens": self.target_tokens,
            "emergency_target_tokens": self.emergency_target_tokens,
            "source": self.source,
        }


def _env_int(name: str, default: int) -> int:
    try:
        value = os.environ.get(name)
        if value is None or str(value).strip() == "":
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _strip_provider_prefix(model: str | None) -> str:
    value = str(model or "").strip()
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _resolve_provider(provider: str | None, model: str) -> str:
    if provider:
        return str(provider).strip().lower()
    return infer_provider_from_model(model, default="openai")


def _context_window_for(provider: str, model: str) -> int:
    override = os.environ.get("AGENT_MODEL_CONTEXT_WINDOW_TOKENS")
    if override:
        return max(1, _env_int("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", DEFAULT_CONTEXT_WINDOW_TOKENS))

    normalized = _strip_provider_prefix(model)
    catalog_entry = get_model_catalog_entry(f"{provider}/{normalized}")
    if catalog_entry is not None:
        return catalog_entry.context_window_tokens
    for (candidate_provider, prefix), tokens in _MODEL_PREFIX_CONTEXT_WINDOWS:
        if provider == candidate_provider and normalized.startswith(prefix):
            return tokens
    return _PROVIDER_CONTEXT_WINDOWS.get(provider, DEFAULT_CONTEXT_WINDOW_TOKENS)


def _reserved_output_tokens(max_output_tokens: int | None) -> int:
    configured = os.environ.get("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS")
    if configured:
        return max(0, _env_int("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", 16_384))
    if max_output_tokens is not None:
        try:
            return max(0, int(max_output_tokens))
        except (TypeError, ValueError):
            return 16_384
    return 16_384


def _reserved_reasoning_tokens(reasoning_effort: str | None) -> int:
    configured = os.environ.get("AGENT_CONTEXT_RESERVED_REASONING_TOKENS")
    if configured:
        return max(0, _env_int("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", 0))
    effort = str(reasoning_effort or "none").strip().lower()
    return _REASONING_RESERVES.get(effort, _REASONING_RESERVES["medium"])


def _reserved_tool_tokens(tools: list[dict] | None) -> int:
    configured = os.environ.get("AGENT_CONTEXT_RESERVED_TOOL_TOKENS")
    if configured:
        return max(0, _env_int("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", 0))
    return min(12_000, max(0, len(tools or [])) * 256)


def resolve_model_context_budget(
    *,
    model: str,
    provider: str | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
    tools: list[dict] | None = None,
) -> ModelContextBudget:
    """Resolve the model-visible input budget used before compaction/retry."""
    resolved_provider = _resolve_provider(provider, model)
    normalized_model = _strip_provider_prefix(model)
    context_window = _context_window_for(resolved_provider, normalized_model)
    output_reserve = _reserved_output_tokens(max_output_tokens)
    reasoning_reserve = _reserved_reasoning_tokens(reasoning_effort)
    tool_reserve = _reserved_tool_tokens(tools)
    safety_margin = max(
        0,
        _env_int("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", max(2_048, context_window // 50)),
    )

    reserved_total = output_reserve + reasoning_reserve + tool_reserve + safety_margin
    effective_input_limit = max(1, context_window - reserved_total)
    if effective_input_limit < context_window // 4:
        effective_input_limit = max(1, context_window // 4)

    configured_limit = os.environ.get("AGENT_AUTO_COMPACT_TOKEN_LIMIT")
    if configured_limit:
        threshold = max(1, _env_int("AGENT_AUTO_COMPACT_TOKEN_LIMIT", effective_input_limit * 9 // 10))
        threshold = min(threshold, effective_input_limit)
    else:
        threshold = max(1, effective_input_limit * 9 // 10)

    configured_target = os.environ.get("AGENT_AUTO_COMPACT_TARGET_TOKENS")
    if configured_target:
        target = max(1, _env_int("AGENT_AUTO_COMPACT_TARGET_TOKENS", threshold * 7 // 10))
        target = min(target, threshold)
    else:
        target = max(1, threshold * 7 // 10)

    emergency_target = max(
        1,
        min(
            threshold,
            _env_int("AGENT_EMERGENCY_COMPACT_TARGET_TOKENS", threshold // 2),
        ),
    )
    return ModelContextBudget(
        provider=resolved_provider,
        model=normalized_model,
        context_window_tokens=context_window,
        reserved_output_tokens=output_reserve,
        reserved_reasoning_tokens=reasoning_reserve,
        reserved_tool_tokens=tool_reserve,
        safety_margin_tokens=safety_margin,
        effective_input_limit_tokens=effective_input_limit,
        auto_compact_threshold_tokens=threshold,
        target_tokens=target,
        emergency_target_tokens=emergency_target,
    )
