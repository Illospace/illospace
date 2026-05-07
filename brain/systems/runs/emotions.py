"""Runtime emotion adaptations for agent execution."""

from __future__ import annotations

from brain.platform.providers.model_policy import (
    DEFAULT_MODEL_TIER,
    LOW_MODEL_TIER,
    MEDIUM_MODEL_TIER,
    normalize_model_tier,
)


NEGATIVE_EMOTIONS = {"frustrated", "angry", "anxious", "disappointed", "urgent"}


def apply_emotional_steering(
    model_tier: str,
    emotion: dict | None,
    adaptations: list[dict],
) -> tuple[str, list[dict]]:
    """Escalate runtime model tier for negative emotions without downgrading."""
    if not emotion or emotion.get("emotion", "neutral") == "neutral":
        return model_tier, adaptations

    emotion_label = emotion["emotion"]
    normalized_tier = normalize_model_tier(model_tier) or DEFAULT_MODEL_TIER
    if emotion_label in NEGATIVE_EMOTIONS and normalized_tier == LOW_MODEL_TIER:
        return MEDIUM_MODEL_TIER, adaptations + [{
            "type": "emotion",
            "trigger": f"user emotion: {emotion_label}",
            "action_taken": "escalated model tier from low to medium",
        }]

    return normalized_tier, adaptations


def detect_run_emotion(message: str) -> dict | None:
    """Detect non-neutral user emotion for runtime adaptation."""
    try:
        from brain.systems.memory.emotions import detect_emotion

        result = detect_emotion(str(message or "")[:500])
        if result and result.get("emotion") != "neutral":
            return result
    except Exception:
        return None
    return None


def behavioral_hints_for_emotion(emotion_label: str) -> dict | None:
    """Return behavioral hints for an emotion when available."""
    try:
        from brain.systems.memory.emotional import get_behavioral_hints

        return get_behavioral_hints(emotion_label)
    except Exception:
        return None


__all__ = [
    "NEGATIVE_EMOTIONS",
    "apply_emotional_steering",
    "behavioral_hints_for_emotion",
    "detect_run_emotion",
]
