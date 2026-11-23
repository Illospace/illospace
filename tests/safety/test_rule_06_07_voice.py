"""Default privacy and verified sender checks apply before audio leaves Slack."""

import httpx
import pytest
from pydantic import SecretStr

from illo.channels.slack.intake import CLIP_ERROR, SlackIntake
from illo.gateway import InboundEvent
from illo.workspace import VoiceConfig


@pytest.fixture
def voice_guard(app, monkeypatch, slack_client):
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    ctx = app.state.ctx
    ctx.config.slack_bot_token = SecretStr("test-bot-token")
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    calls = []

    def fail_http(request):
        calls.append(request)
        raise AssertionError("No audio may leave Slack")

    intake.voice_transport = httpx.MockTransport(fail_http)
    return ctx, intake, calls


def event(**changes):
    return InboundEvent(
        channel="slack",
        external_id="voice-safety",
        sender="U_ACME_ADA",
        kind="dm",
        payload={
            "channel_id": "D_ACME_ADA",
            "message_ts": "1",
            "files": [
                {
                    "id": "F_ACME",
                    "mimetype": "audio/mp4",
                    "subtype": "slack_audio",
                    "transcription": {"status": "none"},
                }
            ],
            **changes,
        },
    )


async def test_rule_06_default_mode_never_sends_audio_out(voice_guard, slack_client):
    ctx, intake, calls = voice_guard
    assert ctx.workspace.config.voice.transcription == "slack"
    result = await ctx.gateway.receive(event())
    assert result["text"] == CLIP_ERROR
    assert not calls
    assert not [c for c in slack_client.calls if c["method"] == "files_info"]


@pytest.mark.parametrize(
    "flag",
    [
        "bot",
        "bot_id",
        "webhook",
        "form",
        "forwarded",
        "from_automation",
        "outside_workspace",
        "contains_outside_text",
    ],
)
async def test_rule_06_07_tainted_event_never_downloads_or_transcribes(
    voice_guard, slack_client, flag
):
    ctx, intake, calls = voice_guard
    ctx.workspace.config.voice = VoiceConfig(transcription="openai")
    await ctx.gateway.receive(event(**{flag: True}))
    assert not calls
    assert not [c for c in slack_client.calls if c["method"] == "files_info"]


async def test_rule_07_unknown_person_never_downloads(voice_guard, slack_client):
    ctx, intake, calls = voice_guard
    ctx.workspace.config.voice = VoiceConfig(transcription="openai")
    inbound = event().model_copy(update={"sender": "U_UNKNOWN"})
    assert (await ctx.gateway.receive(inbound))["decision"] == "ignore"
    assert not calls and not slack_client.calls


async def test_rule_06_bot_channel_never_downloads(voice_guard, slack_client):
    ctx, intake, calls = voice_guard
    ctx.workspace.config.voice = VoiceConfig(transcription="openai")
    inbound = event(bot=True, bot_id="B_ACME", mentioned=True, channel_id="C_ACME_TEAM")
    inbound = inbound.model_copy(update={"kind": "message"})
    assert (await ctx.gateway.receive(inbound))["text"] == CLIP_ERROR
    assert not calls
    assert not [c for c in slack_client.calls if c["method"] == "files_info"]
