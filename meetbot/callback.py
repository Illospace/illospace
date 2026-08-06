"""Meeting webhook delivery with retry and durable dead letters."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, Protocol

import httpx

from meetbot.config import MeetbotConfig
from meetbot.models import SessionHealthSnapshot, SessionRecord

logger = logging.getLogger(__name__)


class MeetingWebhookSender(Protocol):
    """Meeting webhook delivery interface used by the session manager."""

    async def send_transcript(self, record: SessionRecord) -> None: ...

    async def send_health(
        self,
        snapshot: SessionHealthSnapshot,
        *,
        sequence: int,
        warning: str | None = None,
    ) -> None: ...


class MeetingWebhookCallback:
    """POST terminal transcripts and live health to Illospace webhook ingress."""

    def __init__(
        self,
        config: MeetbotConfig,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = f"{config.callback_url.rstrip('/')}/webhooks"
        self._bridge_token = config.bridge_token or ""
        self._private_root = config.private_root
        self._sleep = sleep

    async def send_transcript(self, record: SessionRecord) -> None:
        """Try three deliveries, then save the envelope for manual replay."""

        key = f"meeting-{record.session_id}"
        envelope = {
            "origin": "meetbot",
            "kind": "meeting_transcript",
            "payload": record.completion_payload(),
            "idempotency_key": key,
        }
        await self._deliver(
            session_id=record.session_id,
            key=key,
            envelope=envelope,
            dead_letter_name=f"dead-letter-meeting-{record.session_id}.json",
            failure_log=(
                "Meetbot transcript webhook attempt %d/3 failed for session %s: %s"
            ),
            dead_letter_log="Meetbot transcript webhook saved to dead letter %s",
        )

    async def send_health(
        self,
        snapshot: SessionHealthSnapshot,
        *,
        sequence: int,
        warning: str | None = None,
    ) -> None:
        """Deliver one active-session observation with the shared retry policy."""

        key = f"meeting-health-{snapshot.session_id}-{sequence}"
        envelope = {
            "origin": "meetbot",
            "kind": "meeting_session_health",
            "payload": snapshot.webhook_payload(warning=warning),
            "idempotency_key": key,
        }
        await self._deliver(
            session_id=snapshot.session_id,
            key=key,
            envelope=envelope,
            dead_letter_name=(
                f"dead-letter-meeting-health-{snapshot.session_id}-{sequence}.json"
            ),
            failure_log=(
                "Meetbot health webhook attempt %d/3 failed for session %s: %s"
            ),
            dead_letter_log="Meetbot health webhook saved to dead letter %s",
        )

    async def _deliver(
        self,
        *,
        session_id: str,
        key: str,
        envelope: dict[str, object],
        dead_letter_name: str,
        failure_log: str,
        dead_letter_log: str,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {self._bridge_token}",
            "X-Illo-Idempotency-Key": key,
        }
        last_error = "unknown webhook failure"
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(1, 4):
                try:
                    response = await client.post(self._url, json=envelope, headers=headers)
                    response.raise_for_status()
                    return
                except (httpx.HTTPError, OSError) as exc:
                    last_error = str(exc)
                    logger.warning(failure_log, attempt, session_id, exc)
                    if attempt < 3:
                        await self._sleep(float(2 ** (attempt - 1)))

        self._write_dead_letter(
            envelope,
            last_error,
            name=dead_letter_name,
            log_message=dead_letter_log,
        )

    def _write_dead_letter(
        self,
        envelope: dict[str, object],
        error: str,
        *,
        name: str,
        log_message: str,
    ) -> None:
        self._private_root.mkdir(parents=True, exist_ok=True)
        path = self._private_root / name
        temporary = path.with_name(f".{path.name}.tmp")
        content = {
            "callback_url": self._url,
            "attempts": 3,
            "last_error": error,
            "envelope": envelope,
        }
        temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        logger.error(log_message, path)
