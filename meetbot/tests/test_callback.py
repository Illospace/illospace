from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from meetbot.callback import CompletionCallback
from meetbot.config import MeetbotConfig
from meetbot.models import Origin, SessionRecord
from meetbot.transcript import TranscriptWriter


class _Response:
    def raise_for_status(self) -> None:
        return None


class _Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
    ) -> _Response:
        self.posts.append((url, json, headers))
        if self.fail:
            raise httpx.ConnectError("callback unavailable")
        return _Response()


def _terminal_record(session_id: str = "session-1") -> SessionRecord:
    transcript_path, transcript_md_path = TranscriptWriter.public_paths(session_id)
    return SessionRecord(
        session_id=session_id,
        meeting_url="https://meet.google.com/abc-defg-hij",
        display_name="Illo (notetaker)",
        origin=Origin(channel="C123", thread_ts="1234.500"),
        requested_by="U123",
        transcript_path=transcript_path,
        transcript_md_path=transcript_md_path,
        status="ended",
        started_at="2026-08-03T14:00:00Z",
        ended_at="2026-08-03T15:00:00Z",
        caption_lines=4,
        participants=["Alice", "Bob"],
    )


@pytest.mark.asyncio
async def test_completion_callback_uses_webhook_envelope_and_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr("meetbot.callback.httpx.AsyncClient", lambda **_: client)
    config = MeetbotConfig(
        bridge_token="bridge-secret",
        callback_url="http://api:8000",
        private_root=tmp_path,
    )
    sender = CompletionCallback(config)

    await sender.send(_terminal_record())

    assert len(client.posts) == 1
    url, body, headers = client.posts[0]
    assert url == "http://api:8000/webhooks"
    assert body["origin"] == "meetbot"
    assert body["kind"] == "meeting_transcript"
    assert body["idempotency_key"] == "meeting-session-1"
    assert body["payload"]["origin"] == {"channel": "C123", "thread_ts": "1234.500"}
    assert headers == {
        "Authorization": "Bearer bridge-secret",
        "X-Illo-Idempotency-Key": "meeting-session-1",
    }


@pytest.mark.asyncio
async def test_completion_callback_retries_three_times_then_dead_letters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(fail=True)
    monkeypatch.setattr("meetbot.callback.httpx.AsyncClient", lambda **_: client)

    async def no_sleep(_: float) -> None:
        return None

    sender = CompletionCallback(
        MeetbotConfig(private_root=tmp_path),
        sleep=no_sleep,
    )
    await sender.send(_terminal_record("failed-callback"))

    assert len(client.posts) == 3
    dead_letter = tmp_path / "dead-letter-meeting-failed-callback.json"
    content = json.loads(dead_letter.read_text(encoding="utf-8"))
    assert content["attempts"] == 3
    assert content["envelope"]["kind"] == "meeting_transcript"
    assert content["envelope"]["payload"]["session_id"] == "failed-callback"
