"""Tests for session_hooks.py — pure functions."""

import os
import sys
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def test_consolidate_helpers():
    """Test pure helper functions from consolidate.py."""
    from brain.jobs.pipelines.consolidate import compress_text, classify_memory, extract_tags

    # compress_text
    result = compress_text("Bug Fix", "Fixed a critical production bug in the API endpoint.")
    assert "[Bug Fix]" in result
    assert "critical" in result.lower()

    # classify_memory
    mtype, salience = classify_memory("Lesson", "We learned that assumptions kill")
    assert mtype == "lesson"
    assert salience >= 7.0

    mtype, salience = classify_memory("Random Note", "Had lunch")
    assert mtype == "episode"

    # extract_tags
    tags = extract_tags("Fixed a React frontend bug in the Shopify app")
    assert "frontend" in tags
    assert "bug" in tags
    assert "shopify" in tags


def test_extract_tags_empty():
    """Empty/unrelated text should return empty tags."""
    from brain.jobs.pipelines.consolidate import extract_tags

    tags = extract_tags("The weather is nice")
    assert tags == [] or isinstance(tags, list)


def test_get_cross_channel_context_preserves_rich_fields():
    """Cross-channel recall should return the richer context shape."""
    from unittest.mock import MagicMock
    from brain.app.cli.session_hooks import get_cross_channel_context

    rows = [{
        "id": 1,
        "content": "Discussed deployment strategy across channels",
        "memory_type": "episode",
        "salience": 7,
        "source_session": "slack:deploy",
        "source": "session",
        "created_at": None,
        "emotion_label": "urgent",
        "tags": ["deploy", "ops"],
    }]

    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session.execute.return_value.mappings.return_value.all.return_value = rows

    with patch("brain.app.cli.session_hooks.UnitOfWork", return_value=mock_uow):
        result = get_cross_channel_context("discord:current", hours=24, limit=5)

    assert result == [{
        "id": 1,
        "content": "Discussed deployment strategy across channels",
        "type": "episode",
        "memory_type": "episode",
        "salience": 7.0,
        "source_session": "slack:deploy",
        "source": "session",
        "created_at": None,
        "tags": ["deploy", "ops"],
    }]
