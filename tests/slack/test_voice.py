"""Exercise voice input with recorded Slack data and a fake HTTP transport."""

import asyncio
import logging
import shutil
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import SecretStr, ValidationError

from illo.app import create_app
from illo.channels.slack.clips import read_clips
from illo.channels.slack.intake import CLIP_ERROR, SlackIntake
from illo.channels.slack.voice import OpenAITranscriber
from illo.config import Config
from illo.db import all_rows, one
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.ledger import asks, people
from illo.workspace import VoiceConfig, WorkspaceError, load_workspace
from tests.conftest import ROOT

VOICE_TEXT = "Please show the open tasks."


@pytest.fixture
def voice(intake, app, monkeypatch, slack_client):
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    ctx = app.state.ctx
    ctx.config.slack_bot_token = SecretStr("test-bot-token")
    ctx.workspace.config.voice = VoiceConfig(transcription="openai")
    state = {
        "file": {
            "id": "F_ACME_VOICE",
            "user": "U_ACME_ADA",
            "mimetype": "audio/mp4",
            "filetype": "m4a",
            "duration_ms": 4000,
            "size": 4,
            "transcription": {"status": "none"},
            "url_private_download": "https://files.slack.com/files-pri/acme/clip.m4a",
        },
        "requests": [],
        "status": 200,
        "audio": b"test",
        "text": VOICE_TEXT,
    }

    async def files_info(**kwargs):
        await slack_client._record("files_info", **kwargs)
        return {"file": dict(state["file"])}

    def handler(request):
        state["requests"].append(request)
        if request.method == "GET":
            assert request.url.host == "files.slack.com"
            assert request.headers["authorization"] == "Bearer test-bot-token"
            return httpx.Response(200, content=state["audio"])
        assert str(request.url) == "https://api.openai.com/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer test-api-key"
        assert b"gpt-4o-mini-transcribe" in request.content
        assert b"clip.m4a" in request.content
        assert b"test-bot-token" not in request.content
        return httpx.Response(state["status"], json={"text": state["text"]})

    slack_client.files_info = files_info
    intake.voice_transport = httpx.MockTransport(handler)
    return state


def clip_message(message, **changes):
    return message(
        text="",
        files=[
            {
                "id": "F_ACME_VOICE",
                "subtype": "slack_audio",
                "mimetype": "audio/mp4",
                "transcription": {"status": "none"},
                # A URL in the event must never decide the download destination.
                "url_private_download": "https://attacker.example/clip",
            }
        ],
        **changes,
    )


async def test_openai_none_uses_verified_download_and_preserves_authority(
    intake,
    message,
    voice,
    fake_brain,
    app,
    slack_client,
):
    result = await intake.receive(clip_message(message))
    assert result["decision"] == "run"
    assert fake_brain.requests[-1].user_message == VOICE_TEXT
    assert [r.method for r in voice["requests"]] == ["GET", "POST"]
    async with app.state.ctx.pool.connection() as conn:
        event = await one(conn, "SELECT * FROM events")
        run = await one(conn, "SELECT * FROM runs")
        records = await all_rows(conn, "SELECT * FROM audit WHERE tool='slack_voice_transcribe'")
    assert event["payload"]["payload"]["clip_transcribed_by"] == "openai"
    assert event["payload"]["payload"]["clip_duration_ms"] == 4000
    assert run["principal"] == "person:ada" and run["profile"] == "person_dm"
    assert not run["tainted"]
    assert len(records) == 1 and records[0]["args"] == {}
    assert VOICE_TEXT not in str(records)
    assert "test-api-key" not in str(event)
    assert "test-bot-token" not in str(event)
    assert "attacker.example" not in str(event)
    assert slack_client.calls[0]["text"] == "Looking…"


@pytest.mark.parametrize("inline", [True, False])
async def test_slack_transcript_wins(intake, message, voice, fake_brain, inline):
    payload = clip_message(message)
    complete = {"status": "complete", "text": "Slack transcript wins"}
    if inline:
        payload["event"]["files"][0]["transcription"] = complete
    else:
        voice["file"]["transcription"] = complete
    await intake.receive(payload)
    assert fake_brain.requests[-1].user_message == "Slack transcript wins"
    assert not voice["requests"]


async def test_missing_transcription_uses_openai(intake, message, voice, fake_brain):
    payload = clip_message(message)
    del payload["event"]["files"][0]["transcription"]
    del voice["file"]["transcription"]
    await intake.receive(payload)
    assert fake_brain.requests[-1].user_message == VOICE_TEXT


@pytest.mark.parametrize(
    "change",
    [
        {"duration_ms": 120001},
        {"duration_ms": None},
        {"duration_ms": float("nan")},
        {"size": 10485761},
        {"size": None},
        {"size": 0},
        {"id": "F_OTHER"},
        {"user": "U_ACME_BEN"},
        {"url_private_download": "http://files.slack.com/clip"},
        {"url_private_download": "https://files.slack.com.attacker.example/clip"},
        {"url_private_download": "https://files.slack.com@attacker.example/clip"},
        {"url_private_download": "https://files.slack.com:444/clip"},
    ],
)
async def test_metadata_and_url_caps_fail_before_download(
    intake,
    message,
    voice,
    fake_brain,
    slack_client,
    change,
):
    voice["file"].update(change)
    result = await intake.receive(clip_message(message))
    assert result["text"] == CLIP_ERROR
    assert not voice["requests"] and not fake_brain.requests
    assert slack_client.calls[-1]["text"] == CLIP_ERROR


class AudioStream(httpx.AsyncByteStream):
    def __init__(self):
        self.chunks = 0

    async def __aiter__(self):
        for _ in range(10):
            self.chunks += 1
            yield b"x" * 65536


async def test_streaming_size_cap_stops_before_upload(intake, message, voice, app):
    app.state.ctx.workspace.config.voice.max_bytes = 100000
    stream = AudioStream()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)

    intake.voice_transport = httpx.MockTransport(handler)
    result = await intake.receive(clip_message(message))
    assert result["text"] == CLIP_ERROR
    assert [r.method for r in requests] == ["GET"]
    assert stream.chunks < 10


@pytest.mark.parametrize(
    "destination", ["https://attacker.example/clip", "https://files.slack.com/other"]
)
async def test_download_redirect_is_not_followed(intake, message, voice, destination):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": destination})

    intake.voice_transport = httpx.MockTransport(handler)
    assert (await intake.receive(clip_message(message)))["text"] == CLIP_ERROR
    assert len(requests) == 1


async def test_rate_limit_is_durable_and_serialized(intake, message, voice, app, slack_client):
    ctx = app.state.ctx
    ctx.workspace.config.voice.max_per_person_per_hour = 1
    await ctx.scheduler.close()
    # Two admissions race. Only one can download and spend the key.
    results = await asyncio.gather(
        *(intake.receive(clip_message(message, ts=f"170000000{i}.000001")) for i in range(2))
    )
    assert sum(r.get("text") == CLIP_ERROR for r in results) == 1
    assert len(voice["requests"]) == 2
    restarted = SlackIntake(ctx, "U_ACME_ILLO")
    restarted.voice_transport = intake.voice_transport
    assert (await restarted.receive(clip_message(message, ts="1700000003.000001")))[
        "text"
    ] == CLIP_ERROR
    assert len(voice["requests"]) == 2
    # Ben has a separate cap.
    voice["file"]["user"] = "U_ACME_BEN"
    await restarted.receive(clip_message(message, user="U_ACME_BEN", ts="1700000004.000001"))
    assert len(voice["requests"]) == 4


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_api_errors_give_plain_message_without_logs(
    intake,
    message,
    voice,
    status,
    caplog,
    fake_brain,
):
    caplog.set_level(logging.DEBUG)
    voice["status"] = status
    voice["text"] = "private response text test-api-key test-bot-token"
    result = await intake.receive(clip_message(message))
    assert result["text"] == CLIP_ERROR
    assert not fake_brain.requests
    assert [r.method for r in voice["requests"]] == ["GET", "POST"]
    for private in (voice["text"], VOICE_TEXT, "test-api-key", "test-bot-token"):
        assert private not in caplog.text


async def test_success_does_not_log_transcript_or_credentials(intake, message, voice, caplog):
    caplog.set_level(logging.DEBUG)
    await intake.receive(clip_message(message))
    assert VOICE_TEXT not in caplog.text
    assert "test-api-key" not in caplog.text and "test-bot-token" not in caplog.text


async def test_request_timeout_gives_clip_message(intake, message, voice, app):
    app.state.ctx.workspace.config.voice.timeout_s = 0.01

    async def handler(request):
        await asyncio.sleep(1)
        raise AssertionError("The deadline must cancel this request")

    intake.voice_transport = httpx.MockTransport(handler)
    assert (await intake.receive(clip_message(message)))["text"] == CLIP_ERROR


async def test_openai_voice_answer_reaches_ask_before_brain(
    intake,
    message,
    voice,
    app,
    fake_brain,
):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ask = await asks.create(conn, owner_person_id=ada["id"], question="Which tasks?")
        await conn.execute(
            "UPDATE asks SET awaiting_text=true,slack_channel='D_ACME_ADA',"
            "slack_ts='1700000000.000001' WHERE id=%s",
            (ask["id"],),
        )
    result = await intake.receive(
        clip_message(
            message,
            thread_ts="1700000000.000001",
            ts="1700000001.000001",
        )
    )
    assert result["text"] == "Answer saved." and not fake_brain.requests
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask["id"],))
    assert row["answer_text"] == VOICE_TEXT


async def test_tainted_thread_prevents_download(intake, message, voice, slack_client):
    slack_client.messages = {
        "outside": {"user": "U_UNKNOWN", "text": "Outside data", "ts": "1699999999.000001"}
    }
    result = await intake.receive(clip_message(message, thread_ts="1"))
    assert result["text"] == CLIP_ERROR
    assert not voice["requests"]


async def test_tainted_authority_cannot_download(app, voice):
    event = InboundEvent(
        channel="slack", external_id="tainted", kind="message", sender="U_ACME_ADA", payload={}
    )
    service = OpenAITranscriber(
        app.state.ctx, event, Authority("team", "tainted", tainted=True), "U_ACME_ILLO", None
    )
    assert await service("F_ACME_VOICE") is None
    assert not voice["requests"]


@pytest.mark.parametrize("status", [None, "none", "failed", "complete"])
async def test_no_poll_for_terminal_or_missing_transcript(slack_client, status):
    file = {"id": "F_ACME_VOICE", "mimetype": "audio/mp4"}
    if status:
        file["transcription"] = {"status": status}
    assert await read_clips(slack_client, [file], 0.1, 0.001) is None
    assert not slack_client.calls


async def test_only_pending_status_polls(slack_client):
    count = 0

    async def files_info(**kwargs):
        nonlocal count
        count += 1
        return {"file": {"transcription": {"status": "processing" if count == 1 else "none"}}}

    slack_client.files_info = files_info
    assert (
        await read_clips(
            slack_client,
            [{"id": "F_VOICE", "is_audio": True, "transcription": {"status": "pending"}}],
            1,
            0,
        )
        is None
    )
    assert count == 2


@pytest.fixture
def voice_workspace(tmp_path):
    path = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", path)
    config = path / "illo.yaml"
    data = yaml.safe_load(config.read_text())
    data["voice"] = {"transcription": "openai"}
    config.write_text(yaml.safe_dump(data))
    return path


def test_config_defaults():
    assert VoiceConfig().model_dump() == {
        "transcription": "slack",
        "model": "gpt-4o-mini-transcribe",
        "max_duration_s": 120,
        "max_bytes": 10485760,
        "timeout_s": 30,
        "max_per_person_per_hour": 20,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"transcription": "other"},
        {"max_bytes": 0},
        {"max_duration_s": 0},
        {"timeout_s": 0},
        {"max_per_person_per_hour": 0},
        {"model": ""},
    ],
)
def test_bad_voice_config(change):
    with pytest.raises(ValidationError):
        VoiceConfig(**change)


def test_openai_workspace_needs_key(voice_workspace, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", " ")
    with pytest.raises(WorkspaceError, match="voice.transcription: openai needs OPENAI_API_KEY"):
        load_workspace(voice_workspace)
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    assert load_workspace(voice_workspace).config.voice.transcription == "openai"


async def test_bad_reload_keeps_workspace(app, voice_workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ctx = app.state.ctx
    previous = ctx.workspace
    ctx.config.workspace_dir = voice_workspace
    with pytest.raises(WorkspaceError, match="OPENAI_API_KEY"):
        await ctx.reload()
    assert ctx.workspace is previous


async def test_startup_rejects_openai_without_key(pool, fake_brain, voice_workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    application = create_app(
        Config(workspace_dir=Path(voice_workspace)), pool=pool, brain_client=fake_brain
    )
    with pytest.raises(WorkspaceError, match="OPENAI_API_KEY"):
        async with application.router.lifespan_context(application):
            pytest.fail("Startup must reject the workspace")


@pytest.mark.parametrize("finishes", [True, False])
async def test_pending_metadata_waits_for_slack(
    intake, message, voice, fake_brain, slack_client, finishes
):
    original = slack_client.files_info
    count = 0

    async def files_info(**kwargs):
        nonlocal count
        count += 1
        response = await original(**kwargs)
        response["file"]["transcription"] = (
            {"status": "complete", "text": "Slack finished"}
            if finishes and count > 1
            else {"status": "pending"}
        )
        return response

    slack_client.files_info = files_info
    result = await intake.receive(clip_message(message))
    if finishes:
        assert fake_brain.requests[-1].user_message == "Slack finished"
    else:
        assert result["text"] == CLIP_ERROR
    assert not voice["requests"]


async def test_custom_model_and_base_url(intake, message, voice, monkeypatch, app):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://transcription.example/v1/")
    app.state.ctx.workspace.config.voice.model = "custom-transcription-model"
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            assert request.headers["authorization"] == "Bearer test-bot-token"
            return httpx.Response(200, content=b"test")
        assert str(request.url) == "https://transcription.example/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer test-api-key"
        assert b"custom-transcription-model" in request.content
        return httpx.Response(200, json={"text": VOICE_TEXT})

    intake.voice_transport = httpx.MockTransport(handler)
    assert (await intake.receive(clip_message(message)))["decision"] == "run"
    assert len(requests) == 2


async def test_temp_audio_closes_after_cancel(intake, message, voice, monkeypatch):
    import tempfile

    original = tempfile.TemporaryFile
    opened = []

    def temporary_file():
        file = original()
        opened.append(file)
        return file

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"test")
        raise asyncio.CancelledError

    monkeypatch.setattr("illo.channels.slack.voice.tempfile.TemporaryFile", temporary_file)
    intake.voice_transport = httpx.MockTransport(handler)
    with pytest.raises(asyncio.CancelledError):
        await intake.receive(clip_message(message))
    assert opened and all(file.closed for file in opened)


async def test_multiple_clips_record_each_transcription(intake, message, voice, app):
    payload = clip_message(message)
    payload["event"]["files"] *= 2
    await intake.receive(payload)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT payload FROM events")
        records = await all_rows(conn, "SELECT * FROM audit WHERE tool='slack_voice_transcribe'")
    assert row["payload"]["payload"]["clip_transcription_count"] == 2
    assert row["payload"]["payload"]["clip_duration_ms"] == 8000
    assert len(records) == 2


@pytest.mark.parametrize("team", ["T_ACME", "T_OUTSIDE"])
async def test_thread_workspace_is_verified(intake, message, voice, slack_client, team):
    slack_client.messages = {
        "prior": {
            "user": "U_ACME_BEN",
            "user_team": team,
            "text": "Prior context",
            "ts": "1699999999.000001",
        }
    }
    result = await intake.receive(clip_message(message, thread_ts="1699999999.000001"))
    if team == "T_ACME":
        assert result["decision"] == "run"
        assert len(voice["requests"]) == 2
    else:
        assert result["text"] == CLIP_ERROR
        assert not voice["requests"]
