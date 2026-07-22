from __future__ import annotations

from typing import Any

import pytest

from brain.systems.slack.client import SlackWebClient, split_slack_message


def _unmarked_parts(chunks: list[str]) -> list[str]:
    if len(chunks) == 1:
        return chunks

    total = len(chunks)
    first_marker = f"\n\n(1/{total}) ↓ continued"
    assert chunks[0].endswith(first_marker)
    parts = [chunks[0][: -len(first_marker)]]
    for index, chunk in enumerate(chunks[1:], start=2):
        marker = f"({index}/{total}) continuation\n\n"
        assert chunk.startswith(marker)
        parts.append(chunk[len(marker) :])
    return parts


def _real_defect_payload(limit: int) -> str:
    heading = "\n\n*Per-person recap*\n"
    bullet_start = limit - 8
    lead_length = bullet_start - len(heading)
    lead = "*13:00 ET daily engineering brief*\n"
    evidence_line = "Evidence: " + ("detail " * 9) + "item.\n"
    while len(lead) + len(evidence_line) <= lead_length:
        lead += evidence_line
    lead += "x" * (lead_length - len(lead))
    payload = (
        lead
        + heading
        + "• <@U04R1A6MZST|Reda>: top action — merge staging PR #661.\n"
        + "• <@U_AXEL|Axel>: top action — review the agent queue.\n"
        + "• <@U_JB|JB>: top action — verify the deploy.\n\n"
        + "*Rebalancing*\nNo move recommended."
    )

    mention_start = payload.index("<@U04R1A6MZST|Reda>")
    mention_end = payload.index(">", mention_start)
    assert mention_start < limit < mention_end
    return payload


class _RecordingSlackClient(SlackWebClient):
    def __init__(self) -> None:
        super().__init__("xoxb-test")
        self.posts: list[dict[str, Any]] = []

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert method == "chat.postMessage"
        self.posts.append(dict(payload))
        ts = f"1716900200.{len(self.posts):06d}"
        return {
            "ok": True,
            "channel": payload["channel"],
            "ts": ts,
            "message": {"text": payload["text"], "ts": ts},
        }


def test_split_slack_message_leaves_under_budget_text_unchanged():
    body = "*Daily brief*\nAll systems nominal."

    assert split_slack_message(body, 4000) == [body]


def test_split_slack_message_reproduces_411_without_splitting_the_mention():
    limit = 200
    body = _real_defect_payload(limit)

    chunks = split_slack_message(body, limit)
    parts = _unmarked_parts(chunks)

    assert len(chunks) > 1
    assert all(len(chunk) <= limit for chunk in chunks)
    assert "".join(parts) == body
    assert all(part.count("<") == part.count(">") for part in parts)
    assert all(not part.startswith(":") for part in parts[1:])


def test_split_slack_message_prefers_sections_then_bullets():
    body = (
        "*Daily brief*\n"
        + ("Summary " * 7)
        + "\n\n*Movement*\n"
        + "• First complete action with supporting evidence.\n"
        + "• Second complete action with supporting evidence.\n\n"
        + "*Per-person recap*\n"
        + "• Reda: merge the release.\n"
        + "• Axel: review the queue.\n"
        + "• JB: verify the deploy."
    )

    chunks = split_slack_message(body, 120)
    parts = _unmarked_parts(chunks)

    assert "".join(parts) == body
    assert all(part.startswith(("*", "• ")) for part in parts)
    assert any(part.startswith("*Movement*") for part in parts[1:])
    assert any(part.startswith("• ") for part in parts[1:])


def test_split_slack_message_does_not_break_a_fenced_code_block():
    body = (
        "*Details*\n"
        + ("x" * 62)
        + "\n```\nfirst line\nsecond line\n```\n\n"
        + "*Next*\nShip after review."
    )

    chunks = split_slack_message(body, 120)
    parts = _unmarked_parts(chunks)

    assert "".join(parts) == body
    assert all(part.count("```") % 2 == 0 for part in parts)


def test_split_slack_message_does_not_break_a_slack_link():
    link = "<https://example.com/a/long/path|the complete link label>"
    body = ("Context " * 7) + link + (" trailing detail" * 7)

    chunks = split_slack_message(body, 100)
    parts = _unmarked_parts(chunks)

    assert "".join(parts) == body
    assert all(part.count("<") == part.count(">") for part in parts)
    assert sum(link in part for part in parts) == 1


def test_split_slack_message_hard_cuts_only_as_a_lossless_last_resort():
    body = "x" * 250

    chunks = split_slack_message(body, 80)

    assert all(len(chunk) <= 80 for chunk in chunks)
    assert "".join(_unmarked_parts(chunks)) == body


@pytest.mark.parametrize("limit", [32, 47, 80])
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("```\n" + ("fenced-content\n" * 20) + "```", id="giant-fence"),
        pytest.param("<@" + ("U" * 240) + "|oversized-mention>", id="giant-entity"),
        pytest.param("x" * 257, id="no-whitespace"),
    ],
)
def test_split_slack_message_is_bounded_and_lossless_for_adversarial_input(
    body: str,
    limit: int,
):
    chunks = split_slack_message(body, limit)

    assert all(len(chunk) <= limit for chunk in chunks)
    assert "".join(_unmarked_parts(chunks)) == body


def test_split_slack_message_sizes_markers_from_the_stable_chunk_count():
    body = "x" * 1000

    chunks = split_slack_message(body, 26)

    assert len(chunks) >= 100
    assert all(len(chunk) <= 26 for chunk in chunks)
    assert "".join(_unmarked_parts(chunks)) == body


@pytest.mark.asyncio
async def test_post_message_keeps_oversized_channel_digest_in_channel():
    client = _RecordingSlackClient()
    body = _real_defect_payload(4000)

    result = await client.post_message(channel="C_SOFTWARE", text=body)
    posted_chunks = [str(post["text"]) for post in client.posts]

    assert result["chunk_count"] == len(client.posts) > 1
    assert result["truncated"] is False
    assert all("thread_ts" not in post for post in client.posts)
    assert "".join(_unmarked_parts(posted_chunks)) == body
    assert posted_chunks[0].endswith(f"(1/{len(posted_chunks)}) ↓ continued")
    assert all(
        chunk.startswith(f"({index}/{len(posted_chunks)}) continuation\n\n")
        for index, chunk in enumerate(posted_chunks[1:], start=2)
    )
    channel_text = "\n".join(posted_chunks)
    assert all(name in channel_text for name in ("Reda", "Axel", "JB"))


@pytest.mark.asyncio
async def test_post_message_preserves_an_existing_thread_for_all_chunks():
    client = _RecordingSlackClient()
    body = ("Thread evidence line.\n" * 220) + "Done."

    await client.post_message(
        channel="C_SOFTWARE",
        text=body,
        thread_ts="1716900000.000100",
    )

    assert len(client.posts) > 1
    assert {post.get("thread_ts") for post in client.posts} == {"1716900000.000100"}


@pytest.mark.asyncio
async def test_post_message_leaves_under_budget_delivery_unchanged():
    client = _RecordingSlackClient()
    body = "*Daily brief*\nAll systems nominal."

    result = await client.post_message(channel="C_SOFTWARE", text=body)

    assert client.posts == [{"channel": "C_SOFTWARE", "text": body}]
    assert result["chunk_count"] == 1
    assert result["truncated"] is False
