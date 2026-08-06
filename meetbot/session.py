"""Single-active-session lifecycle orchestration for meetbot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Awaitable, Callable
from uuid import uuid4

from meetbot.callback import CompletionSender
from meetbot.captions import CaptionLine, RollingCaptionBuffer
from meetbot.config import MeetbotConfig
from meetbot.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    MeetEngine,
    Origin,
    SessionEvents,
    SessionRecord,
    SessionStatus,
    isoformat_utc,
)
from meetbot.transcript import TranscriptWriter

logger = logging.getLogger(__name__)

NO_CAPTIONS_WARNING = "No caption mutations were observed within 90 seconds after admission."
HARD_CAP_WARNING = "The meeting ended at the configured maximum session duration."

_ALLOWED_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    "starting": frozenset({"lobby", "ended", "failed"}),
    "lobby": frozenset({"admitted", "ended", "failed"}),
    "admitted": frozenset({"captions_flowing", "ended", "failed"}),
    "captions_flowing": frozenset({"ended", "failed"}),
    "ended": frozenset(),
    "failed": frozenset(),
}


class SessionNotFoundError(LookupError):
    """Raised when an API request names an unknown session."""


class SessionNotActiveError(RuntimeError):
    """Raised when an action requires an active browser session."""


class ActiveSessionError(RuntimeError):
    """Raised when a second meeting tries to use the single bot."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Meetbot is already in session {session_id}")
        self.session_id = session_id


@dataclass(slots=True)
class _ManagedSession:
    """All mutable runtime state for one retained session record."""

    record: SessionRecord
    writer: TranscriptWriter | None
    buffer: RollingCaptionBuffer | None
    lines: list[CaptionLine] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    caption_warning_task: asyncio.Task[None] | None = None
    started_at_monotonic: float = 0.0
    admitted_at_monotonic: float | None = None
    health_sequence: int = 0
    caption_warning_sent: bool = False
    stale_warning_checked: bool = False
    health_wakeup: asyncio.Event = field(default_factory=asyncio.Event)


class SessionManager:
    """Own the browser engine, transcript state, and completion callback."""

    def __init__(
        self,
        config: MeetbotConfig,
        engine: MeetEngine,
        completion_sender: CompletionSender,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._engine = engine
        self._completion_sender = completion_sender
        self._sleep = sleep
        self._monotonic = monotonic
        self._managed_sessions: dict[str, _ManagedSession] = {}
        self._active_session_id: str | None = None
        self._lock = asyncio.Lock()

    async def join(
        self,
        *,
        meeting_url: str,
        display_name: str | None,
        origin: Origin,
        requested_by: str | None,
    ) -> SessionRecord:
        """Reserve the bot and start a browser task without blocking the API."""

        async with self._lock:
            if self._active_session_id:
                active = self._managed_sessions.get(self._active_session_id)
                if active and active.record.status in ACTIVE_STATUSES:
                    raise ActiveSessionError(active.record.session_id)
                self._active_session_id = None

            session_id = str(uuid4())
            transcript_path, transcript_md_path = TranscriptWriter.public_paths(session_id)
            record = SessionRecord(
                session_id=session_id,
                meeting_url=meeting_url,
                display_name=(display_name or self._config.display_name).strip()
                or self._config.display_name,
                origin=origin,
                requested_by=requested_by,
                transcript_path=transcript_path,
                transcript_md_path=transcript_md_path,
            )
            record.status_history.append({"status": "starting", "ts": record.started_at})
            writer = TranscriptWriter(self._config.uploads_root, session_id)
            writer.start(record)
            managed = _ManagedSession(
                record=record,
                writer=writer,
                buffer=RollingCaptionBuffer(),
                started_at_monotonic=self._monotonic(),
            )
            self._managed_sessions[session_id] = managed
            self._active_session_id = session_id
            managed.task = asyncio.create_task(
                self._run_session(record),
                name=f"meetbot-session-{session_id}",
            )
            managed.caption_warning_task = asyncio.create_task(
                self._warn_if_no_captions(record.session_id),
                name=f"meetbot-session-health-{record.session_id}",
            )
            return record

    def get(self, session_id: str) -> SessionRecord:
        managed = self._managed_sessions.get(session_id)
        if managed is None:
            raise SessionNotFoundError(session_id)
        return managed.record

    async def leave(self, session_id: str) -> SessionRecord:
        record = self.get(session_id)
        if record.status in ACTIVE_STATUSES:
            await self._engine.request_leave(session_id)
        return record

    async def chat(self, session_id: str, text: str) -> SessionRecord:
        record = self.get(session_id)
        if record.status not in {"admitted", "captions_flowing"}:
            raise SessionNotActiveError("Meeting chat is available only after admission.")
        await self._engine.send_chat(session_id, text)
        return record

    async def shutdown(self) -> None:
        """Ask the active browser to leave and bound application shutdown."""

        active_id = self._active_session_id
        if active_id:
            await self._engine.request_leave(active_id)
        tasks = [
            managed.task
            for managed in self._managed_sessions.values()
            if managed.task is not None and not managed.task.done()
        ]
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=5.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.exception()

    async def _run_session(self, record: SessionRecord) -> None:
        events: SessionEvents = _ManagerSessionEvents(self, record.session_id)
        try:
            result = await asyncio.wait_for(
                self._engine.run(
                    session_id=record.session_id,
                    meeting_url=record.meeting_url,
                    display_name=record.display_name,
                    events=events,
                ),
                timeout=float(self._config.max_session_seconds),
            )
            record.end_reason = result.reason
            if result.warning:
                record.add_warning(result.warning)
            if result.error:
                record.error = result.error
            await self._finish(record, result.terminal_status)
        except asyncio.TimeoutError:
            record.add_warning(HARD_CAP_WARNING)
            record.end_reason = "hard_cap"
            try:
                await self._engine.request_leave(record.session_id)
            except Exception:
                logger.exception("Meetbot could not click leave at the hard session cap")
            await self._finish(record, "ended")
        except asyncio.CancelledError:
            if record.status not in TERMINAL_STATUSES:
                record.error = "Meetbot stopped while the meeting session was active."
                record.end_reason = "service_shutdown"
                await self._finish(record, "failed")
            raise
        except Exception as exc:
            logger.exception("Meetbot session %s failed", record.session_id)
            record.error = str(exc) or exc.__class__.__name__
            record.end_reason = "error"
            await self._finish(record, "failed")
        finally:
            async with self._lock:
                if self._active_session_id == record.session_id:
                    self._active_session_id = None
            managed = self._managed_sessions.get(record.session_id)
            if managed is not None:
                managed.task = None
                managed.writer = None
                managed.buffer = None
                managed.lines.clear()

    async def _finish(self, record: SessionRecord, status: SessionStatus) -> None:
        if record.status in TERMINAL_STATUSES:
            return
        managed = self._active_managed_session(record.session_id)
        await self._transition(record, status)
        await self._wait_for_health_monitor(record.session_id)
        final_lines = managed.buffer.flush()
        self._commit_lines(record, final_lines)
        record.caption_lines = len(managed.lines)
        managed.writer.finalize(record, managed.lines)
        try:
            await self._completion_sender.send(record)
        except Exception:
            logger.exception("Meetbot could not deliver or dead-letter session %s", record.session_id)

    async def _transition(self, record: SessionRecord, status: SessionStatus) -> None:
        if status == record.status:
            return
        if status not in _ALLOWED_TRANSITIONS[record.status]:
            raise RuntimeError(f"Invalid meetbot transition: {record.status} -> {status}")
        record.status = status
        timestamp = isoformat_utc()
        record.status_history.append({"status": status, "ts": timestamp})
        if status == "admitted" and not record.joined_at:
            record.joined_at = timestamp
            managed = self._active_managed_session(record.session_id)
            managed.admitted_at_monotonic = self._monotonic()
            managed.health_wakeup.set()
        if status in TERMINAL_STATUSES:
            record.ended_at = timestamp
            self._wake_health_monitor(record.session_id)
        self._active_managed_session(record.session_id).writer.write_session(record)

    async def _on_status(self, session_id: str, status: SessionStatus) -> None:
        record = self.get(session_id)
        if status == "captions_flowing":
            raise RuntimeError("captions_flowing requires an observed caption mutation")
        await self._transition(record, status)

    async def _on_caption(
        self,
        session_id: str,
        speaker: str,
        text: str,
        line_id: str | None,
    ) -> None:
        record = self.get(session_id)
        if record.status == "admitted":
            await self._transition(record, "captions_flowing")
        if record.status != "captions_flowing":
            return
        managed = self._active_managed_session(session_id)
        managed.caption_warning_sent = True
        managed.health_wakeup.set()
        self._add_participants(record, [speaker])
        committed = managed.buffer.observe(speaker, text, line_id=line_id)
        self._commit_lines(record, committed)
        managed.writer.write_session(record)

    async def _on_participants(self, session_id: str, names: list[str]) -> None:
        record = self.get(session_id)
        if self._add_participants(record, names):
            self._active_managed_session(session_id).writer.write_session(record)

    async def _on_warning(self, session_id: str, message: str) -> None:
        record = self.get(session_id)
        if record.add_warning(message):
            self._active_managed_session(session_id).writer.write_session(record)
            await self._emit_health(session_id, warning=message)

    def _commit_lines(self, record: SessionRecord, lines: list[CaptionLine]) -> None:
        managed = self._active_managed_session(record.session_id)
        for line in lines:
            managed.lines.append(line)
            managed.writer.append(line)
        record.caption_lines = len(managed.lines)

    @staticmethod
    def _add_participants(record: SessionRecord, names: list[str]) -> bool:
        changed = False
        known = {name.casefold() for name in record.participants}
        for raw_name in names:
            name = " ".join(str(raw_name or "").split())
            if not name or name.casefold() in known:
                continue
            record.participants.append(name)
            known.add(name.casefold())
            changed = True
        return changed

    async def _warn_if_no_captions(self, session_id: str) -> None:
        """Extend the existing caption warning task into the session health loop."""

        managed = self._managed_sessions.get(session_id)
        if managed is None:
            return
        heartbeat_due = (
            managed.started_at_monotonic + float(self._config.health_interval_seconds)
        )
        stale_due = (
            managed.started_at_monotonic + float(self._config.stale_session_seconds)
        )
        try:
            while managed.record.status in ACTIVE_STATUSES:
                caption_due = None
                if (
                    managed.admitted_at_monotonic is not None
                    and not managed.caption_warning_sent
                    and managed.record.status == "admitted"
                ):
                    caption_due = managed.admitted_at_monotonic + float(
                        self._config.caption_warning_seconds
                    )
                deadlines = [heartbeat_due]
                if not managed.stale_warning_checked:
                    deadlines.append(stale_due)
                if caption_due is not None:
                    deadlines.append(caption_due)
                delay = max(0.0, min(deadlines) - self._monotonic())
                await self._wait_for_health_deadline(managed, delay)
                if managed.record.status in TERMINAL_STATUSES:
                    return

                now = self._monotonic()
                warning_emitted = False
                if caption_due is not None and now >= caption_due:
                    managed.caption_warning_sent = True
                    await self._on_warning(
                        session_id,
                        _no_captions_warning(self._config.caption_warning_seconds),
                    )
                    warning_emitted = True

                if not managed.stale_warning_checked and now >= stale_due:
                    managed.stale_warning_checked = True
                    zero_participants = not managed.record.participants
                    zero_captions = not _has_observed_captions(managed.record)
                    if zero_participants or zero_captions:
                        if zero_captions:
                            managed.caption_warning_sent = True
                        await self._on_warning(
                            session_id,
                            _stale_session_warning(
                                managed.record,
                                seconds=self._config.stale_session_seconds,
                            ),
                        )
                        warning_emitted = True

                if now >= heartbeat_due:
                    while heartbeat_due <= now:
                        heartbeat_due += float(self._config.health_interval_seconds)
                    if not warning_emitted:
                        await self._emit_health(session_id)
        except asyncio.CancelledError:
            return
        finally:
            if managed.caption_warning_task is asyncio.current_task():
                managed.caption_warning_task = None

    async def _wait_for_health_deadline(
        self,
        managed: _ManagedSession,
        delay: float,
    ) -> None:
        sleep_task = asyncio.create_task(self._sleep(delay))
        wake_task = asyncio.create_task(managed.health_wakeup.wait())
        _, pending = await asyncio.wait(
            {sleep_task, wake_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        managed.health_wakeup.clear()

    async def _emit_health(
        self,
        session_id: str,
        *,
        warning: str | None = None,
    ) -> None:
        managed = self._managed_sessions.get(session_id)
        if managed is None or managed.record.status in TERMINAL_STATUSES:
            return
        if managed.buffer is None:
            return
        managed.record.caption_lines = len(managed.lines) + managed.buffer.pending_count
        managed.health_sequence += 1
        try:
            await self._completion_sender.send_health(
                managed.record,
                sequence=managed.health_sequence,
                warning=warning,
            )
        except Exception:
            logger.exception(
                "Meetbot could not deliver or dead-letter health for session %s",
                session_id,
            )

    def _wake_health_monitor(self, session_id: str) -> None:
        managed = self._managed_sessions.get(session_id)
        if managed is not None:
            managed.health_wakeup.set()

    async def _wait_for_health_monitor(self, session_id: str) -> None:
        managed = self._managed_sessions.get(session_id)
        task = managed.caption_warning_task if managed is not None else None
        if task is None or task is asyncio.current_task():
            return
        await asyncio.gather(task, return_exceptions=True)

    def _active_managed_session(self, session_id: str) -> _ManagedSession:
        managed = self._managed_sessions.get(session_id)
        if managed is None:
            raise SessionNotFoundError(session_id)
        if managed.writer is None or managed.buffer is None:
            raise SessionNotActiveError(
                f"Meetbot session {session_id} no longer has active runtime state."
            )
        return managed


def _no_captions_warning(seconds: int) -> str:
    if int(seconds) == 90:
        return NO_CAPTIONS_WARNING
    return (
        f"No caption mutations were observed within {int(seconds)} seconds "
        "after admission."
    )


def _has_observed_captions(record: SessionRecord) -> bool:
    return record.status == "captions_flowing" or record.caption_lines > 0


def _stale_session_warning(record: SessionRecord, *, seconds: int) -> str:
    minutes = max(1, int(seconds) // 60)
    return (
        f"Meetbot session health is stale after {minutes} minute(s) for "
        f"{record.meeting_url}: observed {len(record.participants)} participants and "
        f"{record.caption_lines} caption lines. Likely causes are the wrong meeting, "
        "the bot was never admitted, or captions off."
    )


class _ManagerSessionEvents:
    def __init__(self, manager: SessionManager, session_id: str) -> None:
        self._manager = manager
        self._session_id = session_id

    async def status(self, status: SessionStatus) -> None:
        await self._manager._on_status(self._session_id, status)

    async def caption(self, speaker: str, text: str, line_id: str | None = None) -> None:
        await self._manager._on_caption(self._session_id, speaker, text, line_id)

    async def participants(self, names: list[str]) -> None:
        await self._manager._on_participants(self._session_id, names)

    async def warning(self, message: str) -> None:
        await self._manager._on_warning(self._session_id, message)
