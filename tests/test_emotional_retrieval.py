"""Tests for emotion-weighted retrieval, behavioral hints, and trajectory tracking."""

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.memory.emotional import (
    BEHAVIORAL_HINTS,
    EMOTION_BOOST_MAP,
    get_behavioral_hints,
    reset_trajectory,
    track_emotion,
)


# ============================================================
# Behavioral hints
# ============================================================

class TestBehavioralHints:
    def test_frustrated_hints(self):
        hints = get_behavioral_hints("frustrated")
        assert hints["pace"] == "slower"
        assert "empathetic" in hints["tone"]
        assert hints["verification"] == "thorough"

    def test_curious_hints(self):
        hints = get_behavioral_hints("curious")
        assert hints["pace"] == "exploratory"
        assert "collaborative" in hints["tone"]

    def test_urgent_hints(self):
        hints = get_behavioral_hints("urgent")
        assert "fast" in hints["pace"]
        assert "direct" in hints["tone"]

    def test_unknown_emotion_falls_back_to_neutral(self):
        hints = get_behavioral_hints("nonexistent")
        assert hints == BEHAVIORAL_HINTS["neutral"]

    def test_all_emotions_have_required_keys(self):
        required = {"pace", "verification", "tone", "strategy"}
        for emotion, hints in BEHAVIORAL_HINTS.items():
            assert required <= set(hints.keys()), f"{emotion} missing keys"


# ============================================================
# Emotion boost map
# ============================================================

class TestEmotionBoostMap:
    def test_frustrated_boosts_frustrated(self):
        assert EMOTION_BOOST_MAP["frustrated"]["frustrated"] == 1.3

    def test_curious_boosts_exploration(self):
        boosts = EMOTION_BOOST_MAP["curious"]
        assert boosts["curious"] == 1.3
        assert boosts["excited"] == 1.2

    def test_no_boost_for_unrelated(self):
        boosts = EMOTION_BOOST_MAP.get("frustrated", {})
        assert boosts.get("happy", 1.0) == 1.0


# ============================================================
# Trajectory tracking
# ============================================================

class TestTrajectoryTracking:
    def setup_method(self):
        reset_trajectory()

    def test_single_emotion_insufficient_data(self):
        result = track_emotion("neutral")
        assert result["trend"] == "insufficient_data"
        assert result["trajectory_length"] == 1

    def test_negative_escalation(self):
        track_emotion("neutral")
        track_emotion("disappointed")
        result = track_emotion("frustrated")
        assert result["trend"] == "declining"
        assert "escalation_warning" in result
        assert result["escalation_warning"]["trend"] == "negative"

    def test_positive_trend(self):
        track_emotion("neutral")
        track_emotion("satisfied")
        result = track_emotion("happy")
        assert result["trend"] == "improving"
        assert "escalation_warning" not in result

    def test_stable_trend(self):
        track_emotion("neutral")
        track_emotion("neutral")
        result = track_emotion("neutral")
        # neutral → neutral → neutral, valences all 0.0, last not < -0.2
        assert result["trend"] == "stable"

    def test_mixed_no_escalation(self):
        track_emotion("frustrated")
        track_emotion("happy")
        result = track_emotion("frustrated")
        # not monotonically declining
        assert "escalation_warning" not in result

    def test_deep_negative_escalation(self):
        track_emotion("satisfied")
        track_emotion("neutral")
        track_emotion("disappointed")
        track_emotion("frustrated")
        result = track_emotion("angry")
        assert "escalation_warning" in result
        assert result["escalation_warning"]["recent_emotions"] == [
            "disappointed", "frustrated", "angry"
        ]

    def test_reset_clears_trajectory(self):
        track_emotion("angry")
        track_emotion("angry")
        reset_trajectory()
        result = track_emotion("happy")
        assert result["trajectory_length"] == 1
        assert result["trend"] == "insufficient_data"
