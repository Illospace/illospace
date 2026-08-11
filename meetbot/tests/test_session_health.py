from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meetbot.config import MeetbotConfig
from meetbot.models import (
    EngineResult,
    Origin,
    SessionEvents,
    SessionHealthSnapshot,
    SessionRecord,
)
from meetbot.session import SessionManager
from meetbot.session_health import SessionHealthMonitor


class _ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for _ in range(30):
            ready = [item for item in self._sleepers if item[0] <= self.now]
            self._sleepers = [item for item in self._sleepers if item[0] > self.now]
            for _, future in ready:
                if not future.done():
                    future.set_result(None)
            await asyncio.sleep(0)


class _HoldingEngine:
    def __init__(self, *, admitted: bool, caption: bool = False) -> None:
        self.admitted = admitted
        self.caption = caption
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def run(
        self,
        *,
        session_id: str,
        meeting_url: str,
        display_name: str,
        events: SessionEvents,
    ) -> EngineResult:
        await events.status("lobby")
        if self.admitted:
            await events.status("admitted")
        if self.caption:
            await events.caption("Alice", "A pending caption", "line-1")
        self.started.set()
        await self.finish.wait()
        return EngineResult(reason="call_ended")

    async def request_leave(self, session_id: str) -> None:
        self.finish.set()

    async def send_chat(self, session_id: str, text: str) -> None:
        return None


class _RecordingSender:
    def __init__(self) -> None:
        self.health: list[dict[str, object]] = []
        self.terminal: list[dict[str, object]] = []

    async def send_transcript(self, record: SessionRecord) -> None:
        self.terminal.append(record.completion_payload())

    async def send_status(self, snapshot: SessionHealthSnapshot) -> None:
        return None

    async def send_health(
        self,
        snapshot: SessionHealthSnapshot,
        *,
        sequence: int,
        warning: str | None = None,
    ) -> None:
        self.health.append(
            {
                **snapshot.webhook_payload(warning=warning),
                "sequence": sequence,
            }
        )


def _config(
    tmp_path: Path,
    *,
    caption_warning_seconds: int = 90,
    health_interval_seconds: int = 60,
    stale_session_seconds: int = 180,
) -> MeetbotConfig:
    return MeetbotConfig(
        uploads_root=tmp_path / "uploads",
        private_root=tmp_path / "private",
        storage_state_path=tmp_path / "private" / "state.json",
        caption_warning_seconds=caption_warning_seconds,
        health_interval_seconds=health_interval_seconds,
        stale_session_seconds=stale_session_seconds,
    )


async def _join(
    tmp_path: Path,
    *,
    admitted: bool,
    clock: _ManualClock,
    sender: _RecordingSender,
    caption: bool = False,
    caption_warning_seconds: int = 90,
    stale_session_seconds: int = 180,
) -> tuple[SessionManager, _HoldingEngine, SessionRecord]:
    engine = _HoldingEngine(admitted=admitted, caption=caption)
    manager = SessionManager(
        _config(
            tmp_path,
            caption_warning_seconds=caption_warning_seconds,
            stale_session_seconds=stale_session_seconds,
        ),
        engine,
        sender,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    record = await manager.join(
        session_id="health-session",
        meeting_url="https://meet.google.com/abc-defg-hij",
        display_name=None,
        origin=Origin(channel="C-meetings", thread_ts="1722700000.001"),
        requested_by="U-reda",
    )
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    await clock.advance(0)
    return manager, engine, record


@pytest.mark.asyncio
async def test_active_status_delivery_does_not_block_transitions_and_drains_on_shutdown(
    tmp_path: Path,
) -> None:
    class _BlockingStatusSender(_RecordingSender):
        def __init__(self) -> None:
            super().__init__()
            self.statuses: list[str] = []
            self.release = asyncio.Event()

        async def send_status(self, snapshot: SessionHealthSnapshot) -> None:
            self.statuses.append(snapshot.status)
            await self.release.wait()

    engine = _HoldingEngine(admitted=True)
    sender = _BlockingStatusSender()
    manager = SessionManager(_config(tmp_path), engine, sender)

    record = await manager.join(
        session_id="nonblocking-status",
        meeting_url="https://meet.google.com/abc-defg-hij",
        display_name=None,
        origin=Origin(channel="C-meetings", thread_ts="1722700000.001"),
        requested_by="U-reda",
    )
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    for _ in range(10):
        if len(sender.statuses) == 2:
            break
        await asyncio.sleep(0)

    assert record.status == "admitted"
    assert sender.statuses == ["lobby", "admitted"]

    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    sender.release.set()
    await asyncio.wait_for(shutdown, timeout=1)

    assert not manager._status_tasks


def test_deadline_seam_catches_heartbeat_up_without_duplicate_events(
    tmp_path: Path,
) -> None:
    clock = _ManualClock()
    record = SessionRecord(
        session_id="deadline-seam",
        meeting_url="https://meet.google.com/abc-defg-hij",
        display_name="Illo",
        origin=Origin(channel="C-meetings", thread_ts="1722700000.001"),
        requested_by="U-reda",
        transcript_path="brain/uploads/meetings/deadline-seam/transcript.jsonl",
        transcript_md_path="brain/uploads/meetings/deadline-seam/transcript.md",
        status="lobby",
    )
    monitor = SessionHealthMonitor(
        _config(tmp_path),
        record,
        _RecordingSender(),
        record_warning=record.add_warning,
        monotonic=clock.monotonic,
    )

    assert monitor.next_deadline() == 60
    assert monitor.due_events(125) == ("heartbeat",)
    assert monitor.next_deadline() == 180


@pytest.mark.asyncio
async def test_health_snapshot_counts_pending_caption_without_mutating_record(
    tmp_path: Path,
) -> None:
    clock = _ManualClock()
    sender = _RecordingSender()
    manager, engine, record = await _join(
        tmp_path,
        admitted=True,
        caption=True,
        clock=clock,
        sender=sender,
    )

    assert record.caption_lines == 0
    await clock.advance(60)
    assert sender.health[-1]["caption_lines"] == 1
    assert record.caption_lines == 0

    engine.finish.set()
    await asyncio.wait_for(manager._managed_sessions[record.session_id].task, timeout=1)
    assert sender.terminal[-1]["caption_lines"] == 1


@pytest.mark.asyncio
async def test_zero_participant_session_warns_inviting_thread_at_threshold(
    tmp_path: Path,
) -> None:
    clock = _ManualClock()
    sender = _RecordingSender()
    manager, engine, record = await _join(
        tmp_path,
        admitted=False,
        clock=clock,
        sender=sender,
        caption_warning_seconds=10_000,
    )

    await clock.advance(179)
    assert not [item for item in sender.health if item.get("warning")]

    await clock.advance(1)
    warning = [item for item in sender.health if item.get("warning")][-1]
    assert warning["meeting_url"] == "https://meet.google.com/abc-defg-hij"
    assert warning["participant_count"] == 0
    assert warning["caption_lines"] == 0
    assert warning["origin"] == {
        "channel": "C-meetings",
        "thread_ts": "1722700000.001",
    }
    assert "wrong meeting" in str(warning["warning"])
    assert "never admitted" in str(warning["warning"])
    assert "captions off" in str(warning["warning"])

    engine.finish.set()
    await asyncio.wait_for(manager._managed_sessions[record.session_id].task, timeout=1)


@pytest.mark.asyncio
async def test_existing_caption_warning_is_delivered_during_session(
    tmp_path: Path,
) -> None:
    clock = _ManualClock()
    sender = _RecordingSender()
    manager, engine, record = await _join(
        tmp_path,
        admitted=True,
        clock=clock,
        sender=sender,
        stale_session_seconds=10_000,
    )

    await clock.advance(90)

    warning = [item for item in sender.health if item.get("warning")][-1]
    assert "No caption mutations" in str(warning["warning"])
    assert record.warning and "No caption mutations" in record.warning

    engine.finish.set()
    await asyncio.wait_for(manager._managed_sessions[record.session_id].task, timeout=1)


@pytest.mark.asyncio
async def test_2026_08_05_shape_warns_before_five_minutes_and_ends_empty(
    tmp_path: Path,
) -> None:
    clock = _ManualClock()
    sender = _RecordingSender()
    manager, engine, record = await _join(
        tmp_path,
        admitted=True,
        clock=clock,
        sender=sender,
    )

    await clock.advance(90)
    first_warning = next(item for item in sender.health if item.get("warning"))
    assert first_warning["sequence"] <= 3
    assert clock.now < 300

    await clock.advance(30)
    assert any(not item.get("warning") for item in sender.health)

    await clock.advance(7_080)
    engine.finish.set()
    await asyncio.wait_for(manager._managed_sessions[record.session_id].task, timeout=1)

    assert sender.terminal[-1]["caption_lines"] == 0
    assert sender.terminal[-1]["participants"] == []
