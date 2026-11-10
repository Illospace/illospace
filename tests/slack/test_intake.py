"""Replay Slack payloads through the real gateway and fake brain."""

import asyncio
import time

import pytest

from illo.brain.client import BrainEvent
from illo.channels.slack.adapter import ERROR
from illo.channels.slack.intake import CLIP_ERROR
from illo.db import all_rows, one


async def test_dm_placeholder_precedes_brain_and_final_edits(
    intake, message, fake_brain, slack_client
):
    original = fake_brain.run
    start = time.monotonic()

    async def checked(request):
        assert slack_client.calls[0]["text"] == "Looking…"
        assert time.monotonic() - start < 1
        async for event in original(request):
            yield event

    fake_brain.run = checked
    result = await intake.receive(message())
    assert result["decision"] == "run"
    posted = slack_client.calls[0]
    assert posted["method"] == "chat_postMessage" and posted["thread_ts"] is None
    edits = [c for c in slack_client.calls if c["method"] == "chat_update"]
    assert edits[-1]["ts"] == "1"
    assert edits[-1]["text"] == "The lane is working."
    assert not intake.adapter.working


async def test_retry_and_dual_mention_delivery_start_one_run(intake, message, fake_brain, app):
    payload = message(channel="C_ACME_TEAM", channel_type="channel", text="<@U_ACME_ILLO> Update")
    first, retry = await asyncio.gather(intake.receive(payload), intake.receive(payload))
    assert {first["decision"], retry["decision"]} == {"run", "ignore"}
    assert len(fake_brain.requests) == 1
    payload["event"]["type"] = "app_mention"
    payload["event_id"] = "Ev_ANOTHER"
    assert (await intake.receive(payload))["duplicate"]
    async with app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM runs")) == 1


@pytest.mark.parametrize(
    "subtype", ["message_changed", "message_deleted", "channel_join", "channel_leave"]
)
async def test_ignored_subtypes(intake, message, subtype, fake_brain, slack_client):
    assert await intake.receive(message(subtype=subtype)) is None
    assert not fake_brain.requests and not slack_client.calls


async def test_own_message_is_ignored(intake, message, fake_brain):
    assert await intake.receive(message(user="U_ACME_ILLO")) is None
    assert not fake_brain.requests


async def test_channel_modes_and_search_index(intake, message, fake_brain, app):
    result = await intake.receive(
        message(channel="C_ACME_TEAM", channel_type="channel", text="Release plan")
    )
    assert result["decision"] == "ignore"  # Listen without a mention.
    assert not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        item = await one(conn, "SELECT * FROM items WHERE kind='slack'")
        assert item["source_ref"] == "slack:C_ACME_TEAM:1700000000.000001"
        assert item["kind"] == "slack" and item["visibility"] == "team"
        assert "Ada" in item["title"] and "team" in item["title"]
        assert item["body"] == "Release plan" and item["links"]
    await intake.receive(message(ts="1700000001.000001"))
    async with app.state.ctx.pool.connection() as conn:
        assert len(await all_rows(conn, "SELECT * FROM items WHERE kind='slack'")) == 1


async def test_unconfigured_and_off_channel_do_nothing(
    intake, message, fake_brain, app, slack_client
):
    assert await intake.receive(message(channel="C_OTHER", channel_type="channel")) is None
    app.state.ctx.workspace.config.slack.channels[0].mode = "off"
    assert await intake.receive(message(channel="C_ACME_TEAM", channel_type="channel")) is None
    assert not fake_brain.requests and not slack_client.calls


async def test_thread_continues_after_restart(intake, message, app, slack_client, fake_brain):
    from illo.channels.slack.intake import SlackIntake

    await intake.receive(message(type="app_mention", channel="C_ACME_TEAM", channel_type="channel"))
    slack_client.messages = {
        "root": {"ts": "1700000000.000001", "user": "U_ACME_ADA", "text": "First request"},
        "reply": {"ts": "1700000000.000002", "user": "U_ACME_ILLO", "text": "First answer"},
    }
    restarted = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    await restarted.receive(
        message(
            channel="C_ACME_TEAM",
            channel_type="channel",
            thread_ts="1700000000.000001",
            ts="1700000001.000001",
            text="And now?",
        )
    )
    request = fake_brain.requests[-1]
    assert "Ada: First request" in request.user_message
    assert "Illo: First answer" in request.user_message
    assert "And now?" in request.user_message
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert posts[-1]["thread_ts"] == "1700000000.000001"


async def test_clip_transcript_after_placeholder(intake, message, fake_brain, slack_client):
    async def files_info(**kwargs):
        assert slack_client.calls[0]["text"] == "Looking…"
        return {
            "file": {
                "transcription": {
                    "status": "complete",
                    "preview": {"content": "Give me an update from the clip", "has_more": False},
                }
            }
        }

    slack_client.files_info = files_info
    await intake.receive(
        message(
            text="",
            subtype="file_share",
            files=[
                {
                    "id": "F_ACME_CLIP",
                    "mimetype": "audio/mp4",
                    "transcription": {"status": "processing"},
                }
            ],
        )
    )
    assert fake_brain.requests[-1].user_message == "Give me an update from the clip"
    assert len([c for c in slack_client.calls if c["method"] == "chat_postMessage"]) == 1


async def test_clip_without_transcript_never_starts_brain(
    intake, message, fake_brain, slack_client
):
    result = await intake.receive(
        message(text="", files=[{"id": "F_ACME", "mimetype": "video/mp4"}])
    )
    assert result["decision"] == "claimed"
    assert not fake_brain.requests
    assert slack_client.calls[-1]["text"] == CLIP_ERROR
    assert not intake.adapter.working


async def test_error_edits_placeholder(intake, message, fake_brain, slack_client):
    async def fail(request):
        yield BrainEvent(type="error")

    fake_brain.run = fail
    result = await intake.receive(message())
    assert result["error"]
    assert slack_client.calls[-1] == {
        "method": "chat_update",
        "channel": "D_ACME_ADA",
        "ts": "1",
        "text": ERROR,
    }


async def test_long_answer_conversion_and_progress_throttle(
    intake, message, fake_brain, slack_client
):
    fake_brain.final_text = "# Result\n**Ready** [Proof](https://example.com)\n" + "x" * 8000
    fake_brain.script = [{"name": "lanes_search", "arguments": {}}] * 3
    await intake.receive(message())
    edits = [c for c in slack_client.calls if c["method"] == "chat_update"]
    assert len(edits) == 2  # One progress edit and the final edit.
    assert edits[-1]["text"].startswith("*Result*\n*Ready* <https://example.com|Proof>")
    assert all(len(c["text"]) <= 3500 for c in slack_client.calls if "text" in c)
    posts = [c for c in slack_client.calls if c["method"] == "chat_postMessage"]
    assert len(posts) == 3
    assert all(c["thread_ts"] == "1" for c in posts[1:])


async def test_thread_context_reads_latest_page(intake, message, fake_brain, slack_client):
    pages = []

    async def replies(**kwargs):
        pages.append(kwargs.get("cursor"))
        if not kwargs.get("cursor"):
            return {
                "messages": [
                    {"ts": str(i), "user": "U_ACME_ADA", "text": f"Older {i}"} for i in range(25)
                ],
                "response_metadata": {"next_cursor": "last-page"},
            }
        return {"messages": [{"ts": "26", "user": "U_ACME_BEN", "text": "Latest context"}]}

    slack_client.conversations_replies = replies
    await intake.receive(message(thread_ts="1699999999.000001"))
    assert pages == [None, "last-page"]
    assert "Ben: Latest context" in fake_brain.requests[-1].user_message
    assert "Older 0\n" not in fake_brain.requests[-1].user_message


async def test_inline_video_transcript_and_duplicate(
    intake, message, fake_brain, slack_client, app
):
    payload = message(
        text="",
        files=[
            {
                "id": "F_ACME",
                "mimetype": "video/mp4",
                "transcription": {"status": "complete", "text": "Video request"},
            }
        ],
    )
    await asyncio.gather(intake.receive(payload), intake.receive(payload))
    assert len(fake_brain.requests) == 1
    assert fake_brain.requests[0].user_message == "Video request"
    assert not [c for c in slack_client.calls if c["method"] == "files_info"]
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT payload FROM events")
        assert row["payload"]["text"] == "Video request"


async def test_own_bot_without_user_is_ignored(intake, message, fake_brain):
    intake.bot_identity = "B_ACME_ILLO"
    assert (
        await intake.receive(message(user=None, subtype="bot_message", bot_id="B_ACME_ILLO"))
        is None
    )
    assert not fake_brain.requests


@pytest.mark.parametrize("transcript", ["Wednesday", None])
async def test_voice_answer_after_late_slack_signin(app, message, fake_brain, slack_client, transcript):
    from illo.channels.slack.intake import SlackIntake
    from illo.ledger import asks, people

    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ask = await asks.create(conn, owner_person_id=ada["id"], question="Which day?")
        await conn.execute(
            "UPDATE asks SET awaiting_text=true,slack_channel='D_ACME_ADA',"
            "slack_ts='1700000000.000001' WHERE id=%s", (ask["id"],)
        )
    # Business claims already exist when Slack signs in.
    intake = SlackIntake(ctx, "U_ACME_ILLO", clip_timeout=0.001, clip_poll=0.001)
    slack_client.messages = {"old": {"user": "U_ACME_ADA", "text": "Unrelated thread history"}}
    transcription = {"status": "complete", "text": transcript} if transcript else {}
    result = await intake.receive(message(
        text="", ts="1700000001.000001", thread_ts="1700000000.000001",
        files=[{"id": "F_VOICE", "mimetype": "audio/mp4", "transcription": transcription}],
    ))
    assert result["text"] == ("Answer saved." if transcript else CLIP_ERROR)
    assert not fake_brain.requests
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask["id"],))
    assert row["answer_text"] == transcript
    assert row["state"] == ("answered" if transcript else "open")
    assert not [c for c in slack_client.calls if c["method"] == "conversations_replies"]
