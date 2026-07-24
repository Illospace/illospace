"""Canonical reasoning-effort vocabulary and per-provider renderings.

Illo routes one canonical effort ladder everywhere; each provider transport
renders a canonical tier into its native API value at the request boundary.
Routing surfaces never speak provider vocabulary, and transports never
interpret canonical tiers themselves — this module is the only translation.

Deliberately dependency-free so both the provider policy layer and the
transports can import it without cycles.
"""

from __future__ import annotations

EFFORT_TIERS: tuple[str, ...] = ("none", "low", "medium", "high", "xhigh")
EFFORT_TIER_SET: frozenset[str] = frozenset(EFFORT_TIERS)

# Canonical tier -> provider-native effort value. None means "omit the
# reasoning/thinking configuration entirely" for that provider.
PROVIDER_EFFORT_RENDERINGS: dict[str, dict[str, str | None]] = {
    "openai": {
        "none": None,
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
    },
    "anthropic": {
        "none": None,
        "low": "low",
        "medium": "medium",
        "high": "high",
        # Anthropic's ceiling tier is named "max"; canonical xhigh renders to
        # it so callers can ask for the ceiling without provider vocabulary.
        "xhigh": "max",
    },
}


def render_reasoning_effort(provider: str, tier: str | None) -> str | None:
    """Render a canonical tier to a provider-native effort value.

    Values outside the canonical ladder pass through unchanged so a caller
    that deliberately speaks a provider's native vocabulary keeps working;
    "none"/empty always renders to None (omit reasoning).
    """
    normalized = str(tier or "").strip().lower()
    if not normalized or normalized == "none":
        return None
    renderings = PROVIDER_EFFORT_RENDERINGS.get(str(provider or "").strip().lower())
    if renderings is None or normalized not in renderings:
        return normalized
    return renderings[normalized]
