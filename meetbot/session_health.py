"""Active meeting session health scheduling and webhook delivery."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Literal

from meetbot.callback import MeetingWebhookSender
from meetbot.config import MeetbotConfig
from meetbot.models import ACTIVE_STATUSES, SessionHealthSnapshot, SessionRecord

logger = logging.getLogger(__name__)

NO_CAPTIONS_WARNING = (
    "No caption mutations were observed within 90 seconds after admission."
)

HealthEvent = Literal["caption_warning", "stale_warning", "heartbeat"]
WarningRecorder = Callable[[str], bool]


class SessionHealthMonitor:
    """Own health deadlines, warning policy, sequencing, and delivery."""

    def __init__(
        self,
        config: MeetbotConfig,
        record: SessionRecord,
        webhook_sender: MeetingWebhookSender,
        *,
        record_warning: WarningRecorder,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._record = record
        self._webhook_sender = webhook_sender
        self._observed_caption_count = 0
        self._record_warning = record_warning
        self._sleep = sleep
        self._monotonic = monotonic
        started_at = monotonic()
        self._heartbeat_interval = float(config.health_interval_seconds)
        self._caption_warning_seconds = config.caption_warning_seconds
        self._stale_session_seconds = config.stale_session_seconds
        self._heartbeat_deadline = started_at + self._heartbeat_interval
        self._stale_deadline = started_at + float(config.stale_session_seconds)
        self._caption_deadline: float | None = None
        self._caption_deadline_resolved = False
        self._stale_warning_checked = False
        self._wake_event = asyncio.Event()
        self._delivery_lock = asyncio.Lock()
        self._sequence = 0
        self._stopping = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start monitoring this session once."""

        if self._task is not None:
            raise RuntimeError("Session health monitor is already running")
        self._task = asyncio.create_task(
            self._run(),
            name=f"meetbot-session-health-{self._record.session_id}",
        )

    def on_admitted(self) -> None:
        """Start the no-caption deadline from verified admission."""

        if self._caption_deadline is None:
            self._caption_deadline = (
                self._monotonic() + float(self._caption_warning_seconds)
            )
        self._wake_event.set()

    def on_caption_observed(self, observed_caption_count: int) -> None:
        """Resolve the no-caption deadline after the first observed mutation."""

        self._observed_caption_count = observed_caption_count
        self._caption_deadline_resolved = True
        self._wake_event.set()

    async def on_warning(self, warning: str) -> None:
        """Record and deliver a warning reported outside the monitor policy."""

        await self._emit_warning(warning)

    async def stop(self) -> None:
        """Wake the monitor and wait for its task to finish."""

        self._stopping = True
        self._wake_event.set()
        task = self._task
        if task is None or task is asyncio.current_task():
            return
        await asyncio.gather(task, return_exceptions=True)

    def next_deadline(self) -> float:
        """Return the next unresolved health deadline."""

        deadlines = [self._heartbeat_deadline]
        if not self._stale_warning_checked:
            deadlines.append(self._stale_deadline)
        if (
            self._caption_deadline is not None
            and not self._caption_deadline_resolved
            and self._record.status == "admitted"
        ):
            deadlines.append(self._caption_deadline)
        return min(deadlines)

    def due_events(self, now: float) -> tuple[HealthEvent, ...]:
        """Resolve due policy events and catch the heartbeat up to ``now``."""

        events: list[HealthEvent] = []
        if (
            self._caption_deadline is not None
            and not self._caption_deadline_resolved
            and self._record.status == "admitted"
            and now >= self._caption_deadline
        ):
            self._caption_deadline_resolved = True
            events.append("caption_warning")

        if not self._stale_warning_checked and now >= self._stale_deadline:
            self._stale_warning_checked = True
            zero_captions = self._observed_caption_count == 0
            if not self._record.participants or zero_captions:
                if zero_captions:
                    self._caption_deadline_resolved = True
                events.append("stale_warning")

        if now >= self._heartbeat_deadline:
            while self._heartbeat_deadline <= now:
                self._heartbeat_deadline += self._heartbeat_interval
            if not events:
                events.append("heartbeat")
        return tuple(events)

    async def _run(self) -> None:
        try:
            while not self._stopping and self._record.status in ACTIVE_STATUSES:
                delay = max(0.0, self.next_deadline() - self._monotonic())
                await self._wait_for_deadline(delay)
                if self._stopping or self._record.status not in ACTIVE_STATUSES:
                    return
                for event in self.due_events(self._monotonic()):
                    if event == "caption_warning":
                        await self._emit_warning(
                            _no_captions_warning(self._caption_warning_seconds)
                        )
                    elif event == "stale_warning":
                        await self._emit_warning(
                            _stale_session_warning(
                                self._record,
                                observed_caption_count=self._observed_caption_count,
                                seconds=self._stale_session_seconds,
                            )
                        )
                    else:
                        await self._emit_health()
        except asyncio.CancelledError:
            return
        finally:
            self._task = None

    async def _wait_for_deadline(self, delay: float) -> None:
        sleep_task = asyncio.create_task(self._sleep(delay))
        wake_task = asyncio.create_task(self._wake_event.wait())
        _, pending = await asyncio.wait(
            {sleep_task, wake_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._wake_event.clear()

    async def _emit_warning(self, warning: str) -> None:
        if self._record_warning(warning):
            await self._emit_health(warning=warning)

    async def _emit_health(self, *, warning: str | None = None) -> None:
        async with self._delivery_lock:
            if self._stopping or self._record.status not in ACTIVE_STATUSES:
                return
            self._sequence += 1
            snapshot = SessionHealthSnapshot.capture(
                self._record,
                observed_caption_count=self._observed_caption_count,
            )
            try:
                await self._webhook_sender.send_health(
                    snapshot,
                    sequence=self._sequence,
                    warning=warning,
                )
            except Exception:
                logger.exception(
                    "Meetbot could not deliver or dead-letter health for session %s",
                    self._record.session_id,
                )


def _no_captions_warning(seconds: int) -> str:
    if int(seconds) == 90:
        return NO_CAPTIONS_WARNING
    return (
        f"No caption mutations were observed within {int(seconds)} seconds "
        "after admission."
    )


def _stale_session_warning(
    record: SessionRecord,
    *,
    observed_caption_count: int,
    seconds: int,
) -> str:
    minutes = max(1, int(seconds) // 60)
    return (
        f"Meetbot session health is stale after {minutes} minute(s) for "
        f"{record.meeting_url}: observed {len(record.participants)} participants and "
        f"{observed_caption_count} caption lines. Likely causes are the wrong meeting, "
        "the bot was never admitted, or captions off."
    )
