"""Invented Slack messages keep their content, escapes, and text limits."""

import copy
import time

import pytest

from illo.channels.slack.blocks import (
    CUT_MARKER,
    MAX_DEPTH,
    MAX_ELEMENTS,
    MAX_MESSAGE_CHARS,
    message_text,
)
from illo.db import one


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def rich_section(*elements):
    return {"type": "rich_text_section", "elements": list(elements)}


def text(value):
    return {"type": "text", "text": value}


@pytest.fixture
def alert():
    first = "Acme form report: " + "The sample request needs a reply. " * 12
    second = "Details: " + "The invented service returns a sample error. " * 12
    return {
        "text": ("New form report\n" + first)[:289],
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "New form report"}},
            section(first), section(second),
            {"type": "actions", "elements": [{
                "type": "button", "text": {"type": "plain_text", "text": "Read report"},
                "url": "https://example.com/report",
            }]},
        ],
        "attachments": [],
    }


def test_alert_uses_full_blocks_once_without_mutation(alert):
    original = copy.deepcopy(alert)
    result = message_text(alert)
    assert len(alert["text"]) == 289
    assert result.count("New form report") == 1
    assert result.count("Acme form report:") == 1
    assert alert["blocks"][2]["text"]["text"] in result
    assert result.endswith("<https://example.com/report|Read report>")
    assert alert == original


@pytest.mark.parametrize("value", [
    "  A typed message.\nKeep this spacing.  ",
    "&lt;!channel&gt; &lt;@U_ACME_BEN&gt; &gt; &amp; <@U_ACME_ADA> <#C_ACME_TEAM>",
    "connect my ChatGPT",
])
def test_typed_rich_text_is_unchanged(value):
    assert message_text({"text": value, "blocks": [{
        "type": "rich_text", "elements": [rich_section(text(value))],
    }]}) == value
    assert message_text({"text": value}) == value


def test_typed_slack_links_mentions_and_styles_are_unchanged():
    value = ("See <#C_ACME_TEAM|team> and <@U_ACME_BEN|Ben>: "
             "<https://example.com>. *Ready* &lt;!channel&gt;")
    message = {"text": value, "blocks": [{"type": "rich_text", "elements": [rich_section(
        text("See "), {"type": "channel", "channel_id": "C_ACME_TEAM"}, text(" and "),
        {"type": "user", "user_id": "U_ACME_BEN"}, text(": "),
        {"type": "link", "url": "https://example.com"}, text(". "),
        {"type": "text", "text": "Ready", "style": {"bold": True}}, text(" &lt;!channel&gt;"),
    )]}]}
    assert message_text(message) == value


def test_fallback_prefix_with_ellipsis_is_not_repeated():
    assert message_text({"text": "A short...", "blocks": [section("A short summary with details")]}) == (
        "A short summary with details"
    )


def test_fields_context_and_buttons_keep_order():
    result = message_text({"text": "Summary", "blocks": [
        {**section("Summary"), "fields": [
            {"type": "plain_text", "text": "Service: sample"},
            {"type": "mrkdwn", "text": "State: ready"},
        ]},
        {"type": "context", "elements": [text("Context"), {"type": "image"}]},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Read\nreport"},
             "url": "https://example.com/report"},
            {"type": "button", "text": {"type": "plain_text", "text": "Acknowledge"}},
        ]},
    ]})
    assert result == ("Summary\nService: sample\nState: ready\nContext\n"
                      "<https://example.com/report|Read report>\nAcknowledge")


def test_unrelated_text_and_legacy_attachments():
    assert message_text({"text": "Separate note", "attachments": [{
        "pretext": "Before", "title": "Report", "title_link": "https://example.com/report",
        "text": "Details &lt;!channel&gt;", "fields": [{"title": "State", "value": "Ready"}],
        "blocks": [section("More details")], "footer": "After",
    }]}) == ("Separate note\nBefore\n<https://example.com/report|Report>\n"
             "Details &lt;!channel&gt;\nState: Ready\nMore details\nAfter")


def test_rich_text_links_mentions_quotes_and_code():
    assert message_text({"blocks": [{"type": "rich_text", "elements": [
        rich_section(text("For "), {"type": "user", "user_id": "U_ACME_ADA"}, text(" in "),
                     {"type": "channel", "channel_id": "C_ACME_TEAM"}, text(" "),
                     {"type": "usergroup", "usergroup_id": "S_ACME"}, text(" "),
                     {"type": "link", "text": "report", "url": "https://example.com"}),
        {"type": "rich_text_quote", "elements": [text("A quote\nNext line")]},
        {"type": "rich_text_preformatted", "elements": [text("a < b &amp; c")]},
    ]}]}) == ("For <@U_ACME_ADA> in <#C_ACME_TEAM> <!subteam^S_ACME> <https://example.com|report>\n"
              "> A quote\n> Next line\n```\na < b &amp; c\n```")


def test_nested_lists_keep_indent_and_order():
    assert message_text({"blocks": [{"type": "rich_text", "elements": [
        {"type": "rich_text_list", "style": "ordered", "elements": [
            rich_section(text("First")), rich_section(text("Second")),
        ]},
        {"type": "rich_text_list", "style": "bullet", "indent": 1,
         "elements": [rich_section(text("Nested"))]},
        {"type": "rich_text_list", "style": "ordered", "offset": 2,
         "elements": [rich_section(text("Third"))]},
    ]}]}) == "1. First\n2. Second\n  - Nested\n3. Third"


@pytest.mark.parametrize("message", [
    {"text": "Keep", "blocks": [None, 7, {"type": "future", "text": "Skip"}]},
    {"text": "Keep", "blocks": [{"type": []}, {"type": "section", "text": 7}]},
    {"text": "Keep", "blocks": "bad", "attachments": [None, {"fields": "bad"}]},
    {"text": "Keep", "blocks": [section("Keep")]},
])
def test_unknown_and_malformed_content_is_skipped(message):
    assert message_text(message) == "Keep"


def test_huge_text_is_cut_within_configured_limit():
    result = message_text({"text": "x" * 1_000_000}, max_chars=100)
    assert len(result) == 100 and result.endswith(CUT_MARKER)
    result = message_text({"blocks": [section("x" * 1_000_000)]})
    assert len(result) == MAX_MESSAGE_CHARS and result.endswith(CUT_MARKER)


def test_deep_and_huge_payloads_stop_quickly():
    deep = text("Hidden")
    for _ in range(MAX_DEPTH * 100):
        deep = {"type": "rich_text", "elements": [deep]}
    huge = {"blocks": [{"type": "unknown"}] * (MAX_ELEMENTS * 1000) + [section("Hidden")]}
    start = time.monotonic()
    for message in ({"blocks": [deep]}, huge, {"attachments": [{}] * (MAX_ELEMENTS * 1000)}):
        result = message_text(message)
        assert len(result) <= MAX_MESSAGE_CHARS and result.endswith(CUT_MARKER)
        assert "Hidden" not in result
    assert time.monotonic() - start < 1


async def test_alert_event_run_and_search_item_have_full_text(intake, message, alert, app, fake_brain):
    expected = message_text(alert)
    await intake.receive(message(channel="C_ACME_ALERTS", channel_type="channel",
                                 bot_id="B_ACME_FORM", subtype="bot_message", **alert))
    assert fake_brain.requests[-1].user_message == expected
    async with app.state.ctx.pool.connection() as conn:
        event = await one(conn, "SELECT * FROM events")
        item = await one(conn, "SELECT * FROM items WHERE kind='slack'")
    assert event["payload"]["text"] == expected
    assert item["body"] == expected


async def test_thread_context_has_full_text(intake, message, alert, slack_client, fake_brain):
    slack_client.messages = {"root": {"ts": "1699999999.000001", "user": "U_ACME_BEN", **alert}}
    await intake.receive(message(thread_ts="1699999999.000001"))
    assert "Ben: " + message_text(alert) in fake_brain.requests[-1].user_message


async def test_read_thread_has_full_text(app, rpc, rpc_token, slack_client, alert):
    from tests.slack.test_tools import call

    slack_client.messages = {"root": {"ts": "100.1", "user": "U_ACME_BEN", **alert}}
    _, token = rpc_token
    result = await call(rpc, token, "slack_read_thread",
                        {"channel": "C_ACME_TEAM", "thread_ts": "100.1"})
    assert result["decision"] == "allow"
    assert "The invented service returns a sample error." in result["result"]["text"]
    assert "https://example.com/report" in result["result"]["text"]
