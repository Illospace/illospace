"""Shared data contracts for meetbot sessions and browser events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

SessionStatus = Literal[
    "starting",
    "lobby",
    "admitted",
    "captions_flowing",
    "ended",
    "failed",
]

ACTIVE_STATUSES = frozenset({"starting", "lobby", "admitted", "captions_flowing"})
TERMINAL_STATUSES = frozenset({"ended", "failed"})


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime | None = None) -> str:
    """Render a timestamp with a stable UTC suffix."""

    timestamp = value or utc_now()
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Origin:
    """Slack routing context retained through meeting completion."""

    channel: str
    thread_ts: str

    def as_dict(self) -> dict[str, str]:
        return {"channel": self.channel, "thread_ts": self.thread_ts}


@dataclass(slots=True)
class SessionRecord:
    """Mutable in-memory state for one Meet session."""

    session_id: str
    meeting_url: str
    display_name: str
    origin: Origin
    requested_by: str | None
    transcript_path: str
    transcript_md_path: str
    status: SessionStatus = "starting"
    started_at: str = field(default_factory=isoformat_utc)
    joined_at: str | None = None
    ended_at: str | None = None
    caption_lines: int = 0
    participants: list[str] = field(default_factory=list)
    warning: str | None = None
    error: str | None = None
    end_reason: str | None = None
    status_history: list[dict[str, str]] = field(default_factory=list)

    def status_response(self) -> dict[str, object]:
        """Return the public GET /sessions response."""

        return {
            "session_id": self.session_id,
            "status": self.status,
            "meeting_url": self.meeting_url,
            "joined_at": self.joined_at,
            "caption_lines": self.caption_lines,
            "transcript_path": self.transcript_path,
            "error": self.error,
            "warning": self.warning,
            "end_reason": self.end_reason,
        }

    def session_document(self) -> dict[str, object]:
        """Return the durable session.json record."""

        return {
            "session_id": self.session_id,
            "meeting_url": self.meeting_url,
            "display_name": self.display_name,
            "status": self.status,
            "started_at": self.started_at,
            "joined_at": self.joined_at,
            "ended_at": self.ended_at,
            "caption_lines": self.caption_lines,
            "transcript_path": self.transcript_path,
            "transcript_md_path": self.transcript_md_path,
            "participants": list(self.participants),
            "origin": self.origin.as_dict(),
            "requested_by": self.requested_by,
            "warning": self.warning,
            "error": self.error,
            "end_reason": self.end_reason,
            "status_history": list(self.status_history),
        }

    def completion_payload(self) -> dict[str, object]:
        """Return the specified meeting_transcript webhook payload."""

        payload: dict[str, object] = {
            "session_id": self.session_id,
            "meeting_url": self.meeting_url,
            "status": self.status,
            "transcript_path": self.transcript_path,
            "transcript_md_path": self.transcript_md_path,
            "started_at": self.started_at,
            "joined_at": self.joined_at,
            "ended_at": self.ended_at,
            "caption_lines": self.caption_lines,
            "participants": list(self.participants),
            "origin": self.origin.as_dict(),
            "requested_by": self.requested_by,
        }
        if self.warning:
            payload["warning"] = self.warning
        if self.error:
            payload["error"] = self.error
        if self.end_reason:
            payload["end_reason"] = self.end_reason
        return payload

    def add_warning(self, message: str) -> bool:
        """Append one unique warning without hiding an earlier session risk."""

        warning = " ".join(str(message or "").split())
        if not warning:
            return False
        existing = self.warning.splitlines() if self.warning else []
        if warning in existing:
            return False
        self.warning = "\n".join([*existing, warning])
        return True


@dataclass(frozen=True, slots=True)
class SessionHealthSnapshot:
    """Immutable active-session state captured for one health observation."""

    session_id: str
    meeting_url: str
    status: SessionStatus
    started_at: str
    joined_at: str | None
    observed_at: str
    observed_caption_count: int
    participant_count: int
    origin: Origin
    requested_by: str | None

    @classmethod
    def capture(
        cls,
        record: SessionRecord,
        *,
        observed_caption_count: int,
    ) -> SessionHealthSnapshot:
        """Copy the health fields without changing the durable session record."""

        return cls(
            session_id=record.session_id,
            meeting_url=record.meeting_url,
            status=record.status,
            started_at=record.started_at,
            joined_at=record.joined_at,
            observed_at=isoformat_utc(),
            observed_caption_count=observed_caption_count,
            participant_count=len(record.participants),
            origin=record.origin,
            requested_by=record.requested_by,
        )

    def webhook_payload(self, *, warning: str | None = None) -> dict[str, object]:
        """Return one non-terminal meeting_session_health webhook payload."""

        payload: dict[str, object] = {
            "session_id": self.session_id,
            "meeting_url": self.meeting_url,
            "status": self.status,
            "started_at": self.started_at,
            "joined_at": self.joined_at,
            "observed_at": self.observed_at,
            "caption_lines": self.observed_caption_count,
            "participant_count": self.participant_count,
            "origin": self.origin.as_dict(),
            "requested_by": self.requested_by,
        }
        normalized_warning = " ".join(str(warning or "").split())
        if normalized_warning:
            payload["warning"] = normalized_warning
        return payload


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Expected terminal result returned by a Meet browser engine."""

    reason: str
    terminal_status: Literal["ended", "failed"] = "ended"
    warning: str | None = None
    error: str | None = None


class JoinRefusedError(RuntimeError):
    """Google Meet displayed a refusal instead of admitting the bot."""


class SessionEvents(Protocol):
    """Callbacks a browser engine uses to report verified session events."""

    async def status(self, status: SessionStatus) -> None: ...

    async def caption(self, speaker: str, text: str, line_id: str | None = None) -> None: ...

    async def participants(self, names: list[str]) -> None: ...

    async def warning(self, message: str) -> None: ...


class MeetEngine(Protocol):
    """Browser engine interface used by the session manager and test fakes."""

    async def run(
        self,
        *,
        session_id: str,
        meeting_url: str,
        display_name: str,
        events: SessionEvents,
    ) -> EngineResult: ...

    async def request_leave(self, session_id: str) -> None: ...

    async def send_chat(self, session_id: str, text: str) -> None: ...
