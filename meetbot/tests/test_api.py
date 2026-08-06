from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from meetbot.app import create_app
from meetbot.config import MeetbotConfig
from meetbot.models import EngineResult, SessionEvents, SessionRecord


class FakeEngine:
    def __init__(
        self,
        *,
        admit: bool = False,
        captions: bool = False,
        warnings: tuple[str, ...] = (),
        result: EngineResult | None = None,
    ) -> None:
        self.admit = admit
        self.captions = captions
        self.warnings = warnings
        self.result = result
        self.started = threading.Event()
        self.leave_requested = threading.Event()
        self.chat_messages: list[str] = []

    async def run(
        self,
        *,
        session_id: str,
        meeting_url: str,
        display_name: str,
        events: SessionEvents,
    ) -> EngineResult:
        await events.status("lobby")
        if self.admit:
            await events.status("admitted")
        for warning in self.warnings:
            await events.warning(warning)
        if self.captions:
            await events.caption("Alice", "A partial", "line-1")
            await events.caption("Alice", "A complete line", "line-1")
            await events.caption("Alice", "A second line", "line-2")
        self.started.set()
        if self.result is not None:
            return self.result
        while not self.leave_requested.is_set():
            await asyncio.sleep(0.01)
        return EngineResult(reason="leave_requested")

    async def request_leave(self, session_id: str) -> None:
        self.leave_requested.set()

    async def send_chat(self, session_id: str, text: str) -> None:
        self.chat_messages.append(text)


class FakeCompletionSender:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []
        self.sent = threading.Event()

    async def send(self, record: SessionRecord) -> None:
        self.payloads.append(record.completion_payload())
        self.sent.set()

    async def send_health(
        self,
        record: SessionRecord,
        *,
        sequence: int,
        warning: str | None = None,
    ) -> None:
        return None


def _config(tmp_path: Path, *, token: str | None = "meetbot-secret", warning: int = 90) -> MeetbotConfig:
    return MeetbotConfig(
        api_token=token,
        uploads_root=tmp_path / "uploads",
        private_root=tmp_path / "private",
        storage_state_path=tmp_path / "private" / "google-storage-state.json",
        caption_warning_seconds=warning,
    )


def _join(client: TestClient, *, token: bool = True, url: str | None = None) -> Any:
    headers = {"X-Meetbot-Token": "meetbot-secret"} if token else {}
    return client.post(
        "/join",
        headers=headers,
        json={
            "meeting_url": url or "https://meet.google.com/abc-defg-hij?authuser=0",
            "origin": {"channel": "C123", "thread_ts": "1234.500"},
            "requested_by": "U123",
        },
    )


def _wait_for_status(
    client: TestClient,
    session_id: str,
    expected: str,
    *,
    timeout: float = 1.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(
            f"/sessions/{session_id}",
            headers={"X-Meetbot-Token": "meetbot-secret"},
        )
        if response.json()["status"] == expected:
            return response.json()
        time.sleep(0.01)
    raise AssertionError(f"Session {session_id} did not reach {expected}")


def test_health_is_public_but_session_routes_require_token(tmp_path: Path) -> None:
    app = create_app(
        config=_config(tmp_path),
        engine=FakeEngine(),
        completion_sender=FakeCompletionSender(),
    )
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert _join(client, token=False).status_code == 401
        assert client.get("/sessions/missing").status_code == 401


def test_unset_token_skips_auth_and_logs_warning(
    tmp_path: Path,
    caplog: Any,
) -> None:
    caplog.set_level(logging.WARNING)
    engine = FakeEngine()
    app = create_app(
        config=_config(tmp_path, token=None),
        engine=engine,
        completion_sender=FakeCompletionSender(),
    )
    with TestClient(app) as client:
        response = _join(client, token=False)
        assert response.status_code == 202
    assert "HTTP API authentication is disabled" in caplog.text


def test_join_validates_url_and_rejects_second_active_session(tmp_path: Path) -> None:
    engine = FakeEngine()
    app = create_app(
        config=_config(tmp_path),
        engine=engine,
        completion_sender=FakeCompletionSender(),
    )
    with TestClient(app) as client:
        invalid = _join(client, url="https://example.com/abc-defg-hij")
        assert invalid.status_code == 422

        first = _join(client)
        assert first.status_code == 202
        assert set(first.json()) == {"session_id", "status"}
        assert first.json()["status"] == "starting"
        assert engine.started.wait(1.0)

        second = _join(client)
        assert second.status_code == 409
        assert second.json() == {"active_session_id": first.json()["session_id"]}


def test_join_accepts_empty_origin_for_non_slack_runs(tmp_path: Path) -> None:
    engine = FakeEngine()
    app = create_app(
        config=_config(tmp_path),
        engine=engine,
        completion_sender=FakeCompletionSender(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/join",
            headers={"X-Meetbot-Token": "meetbot-secret"},
            json={
                "meeting_url": "https://meet.google.com/abc-defg-hij",
                "origin": {},
            },
        )

        assert response.status_code == 202


def test_caption_mutation_is_required_for_captions_flowing_and_transcript(
    tmp_path: Path,
) -> None:
    engine = FakeEngine(admit=True, captions=True)
    callback = FakeCompletionSender()
    app = create_app(
        config=_config(tmp_path),
        engine=engine,
        completion_sender=callback,
    )
    with TestClient(app) as client:
        joined = _join(client)
        session_id = joined.json()["session_id"]
        status_response = _wait_for_status(client, session_id, "captions_flowing")
        assert status_response["joined_at"] is not None
        assert status_response["caption_lines"] == 1
        assert status_response["transcript_path"] == (
            f"brain/uploads/meetings/{session_id}/transcript.jsonl"
        )
        assert status_response["error"] is None

        chat = client.post(
            f"/sessions/{session_id}/chat",
            headers={"X-Meetbot-Token": "meetbot-secret"},
            json={"text": "  We will follow up in Slack.  "},
        )
        assert chat.status_code == 202
        assert engine.chat_messages == ["We will follow up in Slack."]

        leave = client.post(
            f"/sessions/{session_id}/leave",
            headers={"X-Meetbot-Token": "meetbot-secret"},
        )
        assert leave.status_code == 202
        ended = _wait_for_status(client, session_id, "ended")
        assert ended["caption_lines"] == 2
        assert callback.sent.wait(1.0)
        assert callback.payloads[0]["status"] == "ended"
        assert callback.payloads[0]["caption_lines"] == 2

        session_dir = tmp_path / "uploads" / "meetings" / session_id
        assert (session_dir / "transcript.jsonl").is_file()
        assert (session_dir / "transcript.md").is_file()
        assert (session_dir / "session.json").is_file()


def test_admitted_without_caption_reports_warning_without_fake_success(tmp_path: Path) -> None:
    engine = FakeEngine(admit=True, captions=False)
    app = create_app(
        config=_config(tmp_path, warning=0),
        engine=engine,
        completion_sender=FakeCompletionSender(),
    )
    with TestClient(app) as client:
        joined = _join(client)
        session_id = joined.json()["session_id"]
        response = _wait_for_status(client, session_id, "admitted")
        deadline = time.monotonic() + 1.0
        while not response["warning"] and time.monotonic() < deadline:
            time.sleep(0.01)
            response = client.get(
                f"/sessions/{session_id}",
                headers={"X-Meetbot-Token": "meetbot-secret"},
            ).json()
        assert response["status"] == "admitted"
        assert "No caption mutations" in str(response["warning"])


def test_multiple_warnings_reach_the_completion_callback(tmp_path: Path) -> None:
    language_warning = (
        "Could not confirm the caption language is fr-FR; "
        "the transcript may be translated or empty."
    )
    engine = FakeEngine(admit=True, warnings=(language_warning,))
    callback = FakeCompletionSender()
    app = create_app(
        config=_config(tmp_path, warning=0),
        engine=engine,
        completion_sender=callback,
    )
    with TestClient(app) as client:
        joined = _join(client)
        session_id = joined.json()["session_id"]
        response = _wait_for_status(client, session_id, "admitted")
        deadline = time.monotonic() + 1.0
        while (
            "No caption mutations" not in str(response["warning"])
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            response = client.get(
                f"/sessions/{session_id}",
                headers={"X-Meetbot-Token": "meetbot-secret"},
            ).json()

        warning = str(response["warning"])
        assert language_warning in warning
        assert "No caption mutations" in warning

        client.post(
            f"/sessions/{session_id}/leave",
            headers={"X-Meetbot-Token": "meetbot-secret"},
        )
        _wait_for_status(client, session_id, "ended")
        assert callback.sent.wait(1.0)
        callback_warning = str(callback.payloads[0]["warning"])
        assert language_warning in callback_warning
        assert "No caption mutations" in callback_warning


def test_lobby_timeout_fails_with_distinct_reason_and_actionable_error(
    tmp_path: Path,
) -> None:
    message = (
        "Nobody admitted the bot within 10 minutes. Invite its Google account "
        "to the calendar event, or admit it manually when it knocks."
    )
    engine = FakeEngine(
        result=EngineResult(
            reason="not_admitted",
            terminal_status="failed",
            error=message,
        )
    )
    callback = FakeCompletionSender()
    app = create_app(
        config=_config(tmp_path),
        engine=engine,
        completion_sender=callback,
    )
    with TestClient(app) as client:
        joined = _join(client)
        session_id = joined.json()["session_id"]
        response = _wait_for_status(client, session_id, "failed")

        assert response["error"] == message
        assert response["end_reason"] == "not_admitted"
        record = app.state.session_manager.get(session_id)
        assert record.end_reason == "not_admitted"
        assert callback.sent.wait(1.0)
        assert callback.payloads[0]["status"] == "failed"
        assert callback.payloads[0]["end_reason"] == "not_admitted"
        assert callback.payloads[0]["error"] == message


def test_unknown_session_and_chat_before_admission_are_honest(tmp_path: Path) -> None:
    engine = FakeEngine()
    app = create_app(
        config=_config(tmp_path),
        engine=engine,
        completion_sender=FakeCompletionSender(),
    )
    headers = {"X-Meetbot-Token": "meetbot-secret"}
    with TestClient(app) as client:
        assert client.get("/sessions/missing", headers=headers).status_code == 404
        joined = _join(client)
        session_id = joined.json()["session_id"]
        assert engine.started.wait(1.0)
        chat = client.post(
            f"/sessions/{session_id}/chat",
            headers=headers,
            json={"text": "Hello"},
        )
        assert chat.status_code == 409
