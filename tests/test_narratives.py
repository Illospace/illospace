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


def _items(tag_groups: list[list[str]]) -> list[_FakeHarvestItem]:
    return [_FakeHarvestItem(topic_tags=tags) for tags in tag_groups]


@pytest.mark.parametrize(
    ("tag_groups", "max_tags", "expected"),
    [
        (
            [["auth", "api"], ["auth", "deploy"], ["auth", "api", "ci"]],
            2,
            ["auth", "api"],
        ),
        ([["Auth", "API"], ["auth", "api"]], 5, ["auth", "api"]),
        ([], 5, []),
        ([["a", "b", "c", "d", "e", "f"]], 3, ["a", "b", "c"]),
        ([[], []], 5, []),
    ],
)
def test_extract_topic_tags_counts_lowercases_and_limits(tag_groups, max_tags, expected):
    assert extract_topic_tags(_items(tag_groups), max_tags=max_tags) == expected


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("Auth System", "auth-system"),
        ("C++ Templates!", "c-templates"),
        ("a - - b", "a-b"),
        ("  -hello- ", "hello"),
        ("my-topic", "my-topic"),
        ("", ""),
    ],
)
def test_slugify_topic(topic, expected):
    assert slugify_topic(topic) == expected


@pytest.mark.parametrize(
    ("session_count", "expected"),
    [(0, False), (1, False), (2, True), (10, True)],
)
def test_should_create_narrative_threshold(session_count, expected):
    assert should_create_narrative(session_count) is expected


class TestSynthesizeArc:
    @pytest.mark.parametrize(
        ("summaries", "expected"),
        [
            (
                ["Session A happened", "Session B happened", "Session C happened"],
                "Session A happened | Session B happened | Session C happened",
            ),
            (["A", "B", "C", "D", "E"], "C | D | E"),
        ],
    )
    def test_fallback_on_api_failure_uses_last_three_summaries(self, summaries, expected):
        with patch(
            "brain.platform.integrations.completions.simple_text_completion",
            side_effect=Exception("no api key"),
        ):
            result = _synthesize_arc("My Topic", summaries)
        assert result == expected

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
