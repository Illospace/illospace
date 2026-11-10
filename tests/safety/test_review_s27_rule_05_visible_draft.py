"""Rule 5: draft cards show hidden format characters before approval."""

import pytest

from illo.asks.cards import draft_card, draft_preview


@pytest.mark.parametrize(
    "char", ["\u202e", "\u202a", "\u202c", "\u2066", "\u2069", "\u200b", "\u200d", "\ufeff"]
)
@pytest.mark.parametrize("nested", [False, True])
def test_rule_05_hidden_format_characters_have_visible_escapes(char, nested):
    original = f"Send {char}the reviewed text"
    content = {"arguments": {"text": original}} if nested else {"text": original}
    draft = {
        "id": 1,
        "state": "pending",
        "destination": "outside:acme@example.com",
        "content": content,
    }
    preview = draft_preview(draft)
    assert char not in preview
    assert f"\\u{ord(char):04x}" in preview
    assert char not in str(draft_card(draft))
    assert draft["content"] == content
    assert (content["arguments"] if nested else content)["text"] == original


def test_rule_05_escaped_preview_must_fit_before_send_is_available():
    draft = {
        "id": 1,
        "state": "pending",
        "destination": "outside:acme@example.com",
        "content": {"text": "\u200b" * 20001},
    }
    assert draft_preview(draft) is None
    assert all(b["type"] != "actions" for b in draft_card(draft))
