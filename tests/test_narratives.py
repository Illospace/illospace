"""Tests for brain.systems.memory.narratives — narrative lifecycle."""
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from brain.systems.memory.narratives import (
    _synthesize_arc,
    extract_topic_tags,
    should_create_narrative,
    slugify_topic,
)


# ---------------------------------------------------------------------------
# Lightweight stand-in for HarvestItem (avoids importing Ollama deps)
# ---------------------------------------------------------------------------


@dataclass
class _FakeHarvestItem:
    content: str = ""
    harvest_type: str = "insight"
    confidence: float = 0.8
    topic_tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# extract_topic_tags
# ---------------------------------------------------------------------------


class TestExtractTopicTags:
    def test_returns_most_common(self):
        items = [
            _FakeHarvestItem(topic_tags=["auth", "api"]),
            _FakeHarvestItem(topic_tags=["auth", "deploy"]),
            _FakeHarvestItem(topic_tags=["auth", "api", "ci"]),
        ]
        tags = extract_topic_tags(items, max_tags=2)
        assert tags == ["auth", "api"]

    def test_lowercases_tags(self):
        items = [
            _FakeHarvestItem(topic_tags=["Auth", "API"]),
            _FakeHarvestItem(topic_tags=["auth", "api"]),
        ]
        tags = extract_topic_tags(items, max_tags=5)
        assert all(t == t.lower() for t in tags)

    def test_empty_items(self):
        assert extract_topic_tags([]) == []

    def test_max_tags_respected(self):
        items = [
            _FakeHarvestItem(topic_tags=["a", "b", "c", "d", "e", "f"]),
        ]
        tags = extract_topic_tags(items, max_tags=3)
        assert len(tags) == 3

    def test_no_topic_tags_on_items(self):
        items = [_FakeHarvestItem(topic_tags=[]), _FakeHarvestItem(topic_tags=[])]
        assert extract_topic_tags(items) == []


# ---------------------------------------------------------------------------
# slugify_topic
# ---------------------------------------------------------------------------


class TestSlugifyTopic:
    def test_basic(self):
        assert slugify_topic("Auth System") == "auth-system"

    def test_removes_special_chars(self):
        assert slugify_topic("C++ Templates!") == "c-templates"

    def test_collapses_dashes(self):
        assert slugify_topic("a - - b") == "a-b"

    def test_strips_leading_trailing_dashes(self):
        assert slugify_topic("  -hello- ") == "hello"

    def test_already_slug(self):
        assert slugify_topic("my-topic") == "my-topic"

    def test_empty_string(self):
        assert slugify_topic("") == ""


# ---------------------------------------------------------------------------
# should_create_narrative
# ---------------------------------------------------------------------------


class TestShouldCreateNarrative:
    def test_below_threshold(self):
        assert should_create_narrative(0) is False
        assert should_create_narrative(1) is False

    def test_at_threshold(self):
        assert should_create_narrative(2) is True

    def test_above_threshold(self):
        assert should_create_narrative(10) is True


# ---------------------------------------------------------------------------
# _synthesize_arc
# ---------------------------------------------------------------------------


class TestSynthesizeArc:
    def test_fallback_on_api_failure(self):
        summaries = ["Session A happened", "Session B happened", "Session C happened"]
        with patch("brain.platform.integrations.completions.simple_text_completion", side_effect=Exception("no api key")):
            result = _synthesize_arc("My Topic", summaries)
        assert result == "Session A happened | Session B happened | Session C happened"

    def test_fallback_uses_last_3(self):
        summaries = ["A", "B", "C", "D", "E"]
        with patch("brain.platform.integrations.completions.simple_text_completion", side_effect=Exception("fail")):
            result = _synthesize_arc("Topic", summaries)
        assert result == "C | D | E"

    def test_success_returns_haiku_text(self):
        with patch(
            "brain.platform.integrations.completions.simple_text_completion",
            return_value="A concise arc summary.",
        ):
            result = _synthesize_arc("Topic", ["s1", "s2"])

        assert result == "A concise arc summary."

    def test_empty_summaries_returns_title(self):
        result = _synthesize_arc("My Topic", [])
        assert result == "My Topic"
