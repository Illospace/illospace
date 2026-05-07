"""Tests for emotion detection."""
import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.systems.memory.emotions import detect_keyword_fallback, detect_emotion


class TestKeywordFallback:
    """Test the keyword fallback (always available, no LLM needed)."""

    def test_detects_frustration(self):
        result = detect_keyword_fallback("this is still broken, why does it keep happening")
        assert result["emotion"] == "frustrated"
        assert result["valence"] < 0

    def test_detects_happiness(self):
        result = detect_keyword_fallback("perfect, that's exactly what I wanted")
        assert result["emotion"] == "happy"
        assert result["valence"] > 0

    def test_detects_urgency(self):
        result = detect_keyword_fallback("production is down, customers are affected")
        assert result["emotion"] == "urgent"
        assert result["arousal"] > 0.5

    def test_detects_curiosity(self):
        result = detect_keyword_fallback("what if we tried a different approach, let's explore")
        assert result["emotion"] == "curious"

    def test_neutral_for_no_signals(self):
        result = detect_keyword_fallback("I updated the config file")
        assert result["emotion"] == "neutral"

    def test_confidence_capped_at_half(self):
        """Keyword confidence should never exceed 0.5 — it's not reliable enough."""
        result = detect_keyword_fallback("still broken, not working, keeps happening, seriously wtf")
        assert result["confidence"] <= 0.5

    def test_returns_method(self):
        result = detect_keyword_fallback("this is great")
        assert result["method"] == "keyword_fallback"

    def test_short_message_boosted(self):
        """Short messages with keywords should still detect."""
        result = detect_keyword_fallback("seriously?")
        # 'seriously' is a frustration keyword
        assert result["emotion"] == "frustrated"


class TestKeywordLimitations:
    """Document known limitations of keyword detection.
    These tests SHOULD fail with keywords — they prove why LLM detection is needed."""

    @pytest.mark.xfail(reason="Keyword matching can't detect sarcasm")
    def test_sarcasm_not_detected_keyword(self):
        result = detect_keyword_fallback("Oh perfect, another broken deploy")
        assert result["emotion"] != "happy"  # Keywords see "perfect" and say happy

    @pytest.mark.xfail(reason="Keyword matching misses subtle disappointment")
    def test_subtle_disappointment_keyword(self):
        result = detect_keyword_fallback("This could be better")
        assert result["emotion"] == "disappointed"

    @pytest.mark.xfail(reason="Keyword matching can't detect teaching tone")
    def test_teaching_without_keywords_keyword(self):
        result = detect_keyword_fallback(
            "There is never urgency with you — do what is most robust"
        )
        assert result["emotion"] in ("teaching", "corrective")


@pytest.mark.skip(reason="LLM emotion detection not yet integrated. Needs direct API or local model.")
class TestLLMDetection:
    """Tests for LLM-powered emotion detection — these handle what keywords can't.
    Skipped until LLM integration is completed (provider API or local model)."""

    def test_sarcasm_detected(self):
        result = detect_emotion("Oh perfect, another broken deploy")
        assert result["emotion"] != "happy"
        assert result["emotion"] in ("frustrated", "angry", "disappointed")
        assert result["method"] == "llm"

    def test_subtle_disappointment(self):
        result = detect_emotion("This could be better")
        assert result["emotion"] == "disappointed"
        assert result["method"] == "llm"

    def test_teaching_tone(self):
        result = detect_emotion(
            "There is never urgency with you — do what is most robust"
        )
        assert result["emotion"] in ("teaching", "corrective")
        assert result["method"] == "llm"

    def test_llm_has_higher_confidence_than_keywords(self):
        """LLM should generally have higher confidence than keyword fallback."""
        text = "this is still broken, not working, keeps happening"
        kw_result = detect_keyword_fallback(text)
        llm_result = detect_emotion(text)
        # Both should detect frustration
        assert kw_result["emotion"] == "frustrated"
        assert llm_result["emotion"] == "frustrated"
        # LLM confidence should be higher (keywords capped at 0.5)
        if llm_result["method"] == "llm":
            assert llm_result["confidence"] >= kw_result["confidence"]


class TestKeywordVsLLMComparison:
    """Compare keyword and LLM detection quality side by side."""

    CASES = [
        ("I updated the config file", "neutral", "neutral"),
        ("production is down, customers are affected", "urgent", "urgent"),
        ("perfect, that's exactly what I wanted", "happy", ("happy", "satisfied", "excited")),
        ("Oh perfect, another broken deploy", "happy", ("frustrated", "angry", "disappointed")),  # keyword fails here
        ("This could be better", "neutral", "disappointed"),  # keyword fails here
    ]

    @pytest.mark.parametrize("text,expected_kw,expected_llm", CASES,
                             ids=[c[0][:30] for c in CASES])
    def test_comparison(self, text, expected_kw, expected_llm):
        kw = detect_keyword_fallback(text)
        llm = detect_emotion(text)
        assert kw["emotion"] == expected_kw, f"Keyword: expected {expected_kw}, got {kw['emotion']}"
        if llm["method"] == "llm":
            if isinstance(expected_llm, tuple):
                assert llm["emotion"] in expected_llm, f"LLM: expected one of {expected_llm}, got {llm['emotion']}"
            else:
                assert llm["emotion"] == expected_llm, f"LLM: expected {expected_llm}, got {llm['emotion']}"
