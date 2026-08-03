from __future__ import annotations

import sys

from meetbot.captions import RollingCaptionBuffer


def test_progressive_updates_replace_until_next_dom_line() -> None:
    timestamps = iter(
        [
            "2026-08-03T14:00:00Z",
            "2026-08-03T14:00:10Z",
        ]
    )
    captions = RollingCaptionBuffer(clock=lambda: next(timestamps))

    assert captions.observe("Alice", "We should", line_id="line-1") == []
    assert captions.observe("Alice", "We should ship", line_id="line-1") == []
    committed = captions.observe("Alice", "Next topic", line_id="line-2")

    assert [line.as_dict() for line in committed] == [
        {
            "ts": "2026-08-03T14:00:00Z",
            "speaker": "Alice",
            "text": "We should ship",
        }
    ]
    assert [line.text for line in captions.flush()] == ["Next topic"]


def test_stale_dom_updates_do_not_duplicate_committed_lines() -> None:
    captions = RollingCaptionBuffer(clock=lambda: "2026-08-03T14:00:00Z")
    captions.observe("Alice", "First", line_id="line-1")
    assert [line.text for line in captions.observe("Alice", "Second", line_id="line-2")] == [
        "First"
    ]

    assert captions.observe("Alice", "First", line_id="line-1") == []
    assert [line.text for line in captions.flush()] == ["Second"]


def test_line_identity_is_scoped_to_each_speaker() -> None:
    captions = RollingCaptionBuffer(clock=lambda: "2026-08-03T14:00:00Z")
    captions.observe("Alice", "Alpha", line_id="shared-dom-id")
    captions.observe("Alice", "Beta", line_id="alice-next")

    assert captions.observe("Bob", "Hello", line_id="shared-dom-id") == []
    assert [line.text for line in captions.flush()] == ["Beta", "Hello"]


def test_text_shape_fallback_handles_observers_without_line_ids() -> None:
    captions = RollingCaptionBuffer(clock=lambda: "2026-08-03T14:00:00Z")
    captions.observe("Alice", "A progressive", line_id=None)
    assert captions.observe("Alice", "A progressive caption", line_id=None) == []
    assert [line.text for line in captions.observe("Alice", "A separate thought", line_id=None)] == [
        "A progressive caption"
    ]


def test_caption_module_does_not_import_playwright() -> None:
    assert "playwright" not in sys.modules
