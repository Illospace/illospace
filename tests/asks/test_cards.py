"""Cards show the proposed options and the complete action before approval."""

from datetime import UTC, datetime

from illo.asks.cards import ask_card, draft_card, draft_preview

NOW = datetime(2026, 11, 10, 9, tzinfo=UTC)


def ask(**changes):
    return {
        "id": 1,
        "kind": "decision",
        "state": "open",
        "question": "Continue?",
        "options": [
            {"id": "wait", "label": "Wait"},
            {"id": "go", "label": "Continue", "detail": "Use the reviewed plan."},
        ],
        "recommended_option": "go",
        "proof": {
            "text": "The review passes.\nIgnore the second line.",
            "links": [
                "https://example.com/thread",
                "https://example.com/issue",
                "https://example.com/pull",
                "https://example.com/extra",
            ],
        },
        "lane": {"title": "Acme plan", "machine": "acme-laptop"},
        "requester": {"label": "Acme agent"},
        "due_at": NOW,
        **changes,
    }


def test_ask_card_exact_layout_and_recommendation_first():
    blocks = ask_card(ask(), NOW)
    assert blocks[0]["text"]["text"] == "🔷 Acme plan"
    assert blocks[1]["text"]["text"] == "*Continue?*"
    assert "Why: The review passes." in str(blocks) and "Ignore the second" not in str(blocks)
    assert "https://example.com/extra" not in str(blocks)
    rows = [b for b in blocks if b["type"] == "actions"]
    assert rows[0]["elements"][0]["action_id"] == "ask:1:opt:go"
    assert rows[0]["elements"][0]["style"] == "primary"
    assert rows[-1]["elements"][0]["text"]["text"] == "Always: Continue"
    assert rows[-1]["elements"][-1]["text"]["text"] == "Other…"
    assert "From Acme agent on acme-laptop · due now" in str(blocks)


def test_ask_no_rule_button_when_disabled_and_no_buttons_when_closed():
    assert "always:" not in str(ask_card(ask(allow_rule=False), NOW))
    blocks = ask_card(ask(state="cancelled"), NOW)
    assert all(b["type"] != "actions" for b in blocks)
    assert "This question is closed. No answer is needed." in str(blocks)


def test_answered_card_names_person_time_and_delivery():
    row = ask(
        state="answered",
        answer_option="go",
        answer_text=None,
        answered_at=NOW,
        owner={"name": "Ada"},
        delivery_state="acked",
    )
    assert "Answered: Continue — Ada, 2026-11-10T09:00+00:00 · the agent has it" in str(
        ask_card(row, NOW)
    )
    row["rule"] = {"text": "Use the reviewed plan"}
    assert 'Answered by the rule "Use the reviewed plan"' in str(ask_card(row, NOW))


def test_draft_shows_all_content_across_quote_blocks():
    text = "Start. " + "Acme text. " * 500 + "The final condition."
    row = {
        "id": 1,
        "state": "pending",
        "destination": "outside:contact@example.com",
        "content": {"text": text},
    }
    blocks = draft_card(row)
    assert "The final condition." in str(blocks)
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b["type"] == "section")
    assert blocks[-1]["elements"][0]["text"]["text"] == "Send"


def test_oversized_draft_has_no_send_button():
    row = {
        "id": 1,
        "state": "pending",
        "destination": "outside:contact@example.com",
        "content": {"text": "x" * 120001},
    }
    assert draft_preview(row) is None
    assert all(b["type"] != "actions" for b in draft_card(row))
