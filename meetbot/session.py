"""Single-active-session lifecycle orchestration for meetbot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Awaitable, Callable

from meetbot.callback import MeetingWebhookSender
from meetbot.captions import CaptionLine, RollingCaptionBuffer
from meetbot.config import MeetbotConfig
from meetbot.models import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    MeetEngine,
    MeetbotSessionOutcome,
    Origin,
    SessionEvents,
    SessionHealthSnapshot,
    SessionRecord,
    SessionStatus,
    isoformat_utc,
)
from meetbot.session_health import SessionHealthMonitor
from meetbot.transcript import TranscriptWriter

logger = logging.getLogger(__name__)

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
    health_monitor: SessionHealthMonitor
    lines: list[CaptionLine] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class SessionManager:
    """Own the browser engine, transcript state, and meeting webhook sender."""

    def __init__(
        self,
        config: MeetbotConfig,
        engine: MeetEngine,
        webhook_sender: MeetingWebhookSender,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._engine = engine
        self._webhook_sender = webhook_sender
        self._sleep = sleep
        self._monotonic = monotonic
        self._managed_sessions: dict[str, _ManagedSession] = {}
        self._active_session_id: str | None = None
        self._status_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    async def join(
        self,
        *,
        session_id: str,
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
            health_monitor = SessionHealthMonitor(
                self._config,
                record,
                self._webhook_sender,
                record_warning=lambda warning: self._record_health_warning(
                    record.session_id,
                    warning,
                ),
                sleep=self._sleep,
                monotonic=self._monotonic,
            )
            managed = _ManagedSession(
                record=record,
                writer=writer,
                buffer=RollingCaptionBuffer(),
                health_monitor=health_monitor,
            )
            self._managed_sessions[session_id] = managed
            self._active_session_id = session_id
            managed.task = asyncio.create_task(
                self._run_session(record),
                name=f"meetbot-session-{session_id}",
            )
            health_monitor.start()
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
        await self._finish_background_tasks(tasks)
        await self._finish_background_tasks(list(self._status_tasks))

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
            record.outcome = MeetbotSessionOutcome.from_engine_end_reason(
                result.reason,
                was_admitted=record.joined_at is not None,
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
            record.outcome = MeetbotSessionOutcome.from_engine_end_reason(
                record.end_reason,
                was_admitted=record.joined_at is not None,
            )
            try:
                await self._engine.request_leave(record.session_id)
            except Exception:
                logger.exception("Meetbot could not click leave at the hard session cap")
            await self._finish(record, "ended")
        except asyncio.CancelledError:
            if record.status not in TERMINAL_STATUSES:
                record.error = "Meetbot stopped while the meeting session was active."
                record.end_reason = "service_shutdown"
                record.outcome = MeetbotSessionOutcome.from_engine_end_reason(
                    record.end_reason,
                    was_admitted=record.joined_at is not None,
                )
                await self._finish(record, "failed")
            raise
        except Exception as exc:
            logger.exception("Meetbot session %s failed", record.session_id)
            record.error = str(exc) or exc.__class__.__name__
            record.end_reason = "error"
            record.outcome = MeetbotSessionOutcome.from_engine_end_reason(
                record.end_reason,
                was_admitted=record.joined_at is not None,
            )
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
        await managed.health_monitor.stop()
        final_lines = managed.buffer.flush()
        self._commit_lines(record, final_lines)
        record.caption_lines = len(managed.lines)
        await self._transition(record, status)
        managed.writer.finalize(record, managed.lines)
        try:
            await self._webhook_sender.send_transcript(record)
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
            record.outcome = MeetbotSessionOutcome.ADMITTED
            managed = self._active_managed_session(record.session_id)
            managed.health_monitor.on_admitted()
        if status in TERMINAL_STATUSES:
            record.ended_at = timestamp
        self._active_managed_session(record.session_id).writer.write_session(record)
        if status in ACTIVE_STATUSES:
            snapshot = SessionHealthSnapshot.capture(
                record,
                observed_caption_count=record.caption_lines,
            )
            task = asyncio.create_task(
                self._deliver_status(snapshot),
                name=f"meetbot-status-{record.session_id}-{status}",
            )
            self._status_tasks.add(task)
            task.add_done_callback(self._status_tasks.discard)

    async def _deliver_status(self, snapshot: SessionHealthSnapshot) -> None:
        try:
            await self._webhook_sender.send_status(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Meetbot could not deliver or dead-letter status %s for session %s",
                snapshot.status,
                snapshot.session_id,
            )

    @staticmethod
    async def _finish_background_tasks(tasks: list[asyncio.Task[None]]) -> None:
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=5.0)
        for task in pending:
            logger.warning(
                "Cancelling background task %s after the shutdown timeout",
                task.get_name(),
            )
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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
        self._add_participants(record, [speaker])
        committed = managed.buffer.observe(speaker, text, line_id=line_id)
        self._commit_lines(record, committed)
        managed.health_monitor.on_caption_observed(
            len(managed.lines) + managed.buffer.pending_count
        )
        managed.writer.write_session(record)

    async def _on_participants(self, session_id: str, names: list[str]) -> None:
        record = self.get(session_id)
        if self._add_participants(record, names):
            self._active_managed_session(session_id).writer.write_session(record)

    async def _on_warning(self, session_id: str, message: str) -> None:
        managed = self._active_managed_session(session_id)
        await managed.health_monitor.on_warning(message)

    def _record_health_warning(self, session_id: str, message: str) -> bool:
        managed = self._active_managed_session(session_id)
        if not managed.record.add_warning(message):
            return False
        managed.writer.write_session(managed.record)
        return True

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

    def _active_managed_session(self, session_id: str) -> _ManagedSession:
        managed = self._managed_sessions.get(session_id)
        if managed is None:
            raise SessionNotFoundError(session_id)
        if managed.writer is None or managed.buffer is None:
            raise SessionNotActiveError(
                f"Meetbot session {session_id} no longer has active runtime state."
            )
        return managed


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
