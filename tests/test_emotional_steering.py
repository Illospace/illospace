# tests/test_emotional_steering.py
"""Tests for emotional steering — model escalation and strategy warnings."""
import pytest


def test_frustration_escalates_model():
    """When user is frustrated and model is low, escalate to medium."""
    from brain.systems.runs.emotions import apply_emotional_steering as _apply_emotional_steering

    model_tier = "low"
    emotion = {"emotion": "frustrated", "confidence": 0.8}
    adaptations = []

    new_tier, new_adaptations = _apply_emotional_steering(model_tier, emotion, adaptations)
    assert new_tier == "medium"
    assert len(new_adaptations) == 1
    assert new_adaptations[0]["type"] == "emotion"


def test_neutral_emotion_no_change():
    """Neutral emotion should not change model tier."""
    from brain.systems.runs.emotions import apply_emotional_steering as _apply_emotional_steering

    model_tier = "low"
    emotion = {"emotion": "neutral", "confidence": 0.5}
    adaptations = []

    new_tier, new_adaptations = _apply_emotional_steering(model_tier, emotion, adaptations)
    assert new_tier == "low"
    assert len(new_adaptations) == 0


def test_already_medium_no_change():
    """If model is already medium, frustration should not change it."""
    from brain.systems.runs.emotions import apply_emotional_steering as _apply_emotional_steering

    model_tier = "medium"
    emotion = {"emotion": "frustrated", "confidence": 0.8}
    adaptations = []

    new_tier, new_adaptations = _apply_emotional_steering(model_tier, emotion, adaptations)
    assert new_tier == "medium"
    assert len(new_adaptations) == 0  # No change needed


def test_angry_escalates_model():
    """Angry emotion should also escalate low to medium."""
    from brain.systems.runs.emotions import apply_emotional_steering as _apply_emotional_steering

    model_tier = "low"
    emotion = {"emotion": "angry", "confidence": 0.9}
    adaptations = []

    new_tier, new_adaptations = _apply_emotional_steering(model_tier, emotion, adaptations)
    assert new_tier == "medium"
    assert len(new_adaptations) == 1


def test_none_emotion_no_change():
    """None emotion should not change model tier."""
    from brain.systems.runs.emotions import apply_emotional_steering as _apply_emotional_steering

    model_tier = "low"
    emotion = None
    adaptations = []

    new_tier, new_adaptations = _apply_emotional_steering(model_tier, emotion, adaptations)
    assert new_tier == "low"
    assert len(new_adaptations) == 0


def test_adaptations_not_mutated():
    """Original adaptations list should not be mutated."""
    from brain.systems.runs.emotions import apply_emotional_steering as _apply_emotional_steering

    model_tier = "low"
    emotion = {"emotion": "frustrated", "confidence": 0.8}
    original = [{"type": "existing", "trigger": "t", "action_taken": "a"}]

    new_tier, new_adaptations = _apply_emotional_steering(model_tier, emotion, original)
    assert len(original) == 1  # Original unchanged
    assert len(new_adaptations) == 2  # Original + new


def test_negative_emotions_set():
    """All expected negative emotions should be in the set."""
    from brain.systems.runs.emotions import NEGATIVE_EMOTIONS

    assert "frustrated" in NEGATIVE_EMOTIONS
    assert "angry" in NEGATIVE_EMOTIONS
    assert "anxious" in NEGATIVE_EMOTIONS
    assert "disappointed" in NEGATIVE_EMOTIONS
    assert "urgent" in NEGATIVE_EMOTIONS
    assert "happy" not in NEGATIVE_EMOTIONS
    assert "neutral" not in NEGATIVE_EMOTIONS
