"""Canonical requested/effective routing metadata helpers for AgentRuns."""

from __future__ import annotations

from typing import Any, Mapping

from brain.platform.providers.model_policy import (
    infer_provider_from_model,
    required_openai_auth_mode,
)


_INFER_AUTH_MODE = object()


def effective_routing_snapshot(
    model: str,
    effort: str | None,
    *,
    provider: str | None = None,
    auth_mode: Any = _INFER_AUTH_MODE,
) -> dict[str, Any]:
    """Return the canonical route selected for one model execution."""

    resolved_provider = str(
        provider or infer_provider_from_model(model)
    ).strip().lower()
    model_name = str(model or "").strip()
    for prefix in ("anthropic/", "openai/", "anthropic:", "openai:"):
        if model_name.startswith(prefix):
            model_name = model_name[len(prefix):]
            break
    canonical_model = (
        model_name
        if model_name == "local"
        else f"{resolved_provider}/{model_name}"
    )
    if auth_mode is _INFER_AUTH_MODE:
        resolved_auth_mode = (
            required_openai_auth_mode(canonical_model)
            if resolved_provider == "openai"
            else None
        )
    else:
        resolved_auth_mode = (
            str(auth_mode).strip()
            if isinstance(auth_mode, str) and auth_mode.strip()
            else None
        )
    return {
        "model": canonical_model,
        "effort": str(effort or "none").strip().lower(),
        "provider": resolved_provider,
        "auth_mode": resolved_auth_mode,
    }


def routing_metadata_with_effective(
    metadata: Mapping[str, Any] | None,
    effective: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge an effective snapshot without discarding routing provenance."""

    routing_value = (metadata or {}).get("routing")
    routing = dict(routing_value) if isinstance(routing_value, Mapping) else {}
    routing.update({
        "model": effective["model"],
        "effort": effective["effort"],
        "effective": dict(effective),
    })
    return routing


__all__ = ["effective_routing_snapshot", "routing_metadata_with_effective"]
