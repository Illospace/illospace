"""Completion webhook delivery with retry and durable dead letters."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable, Protocol

import httpx

from meetbot.config import MeetbotConfig
from meetbot.models import SessionRecord

logger = logging.getLogger(__name__)


class CompletionSender(Protocol):
    """Delivery interface used by the session manager."""

    async def send(self, record: SessionRecord) -> None: ...


class CompletionCallback:
    """POST terminal meeting records to Illospace's webhook ingress."""

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

    async def send(self, record: SessionRecord) -> None:
        """Try three deliveries, then save the envelope for manual replay."""

        key = f"meeting-{record.session_id}"
        envelope = {
            "origin": "meetbot",
            "kind": "meeting_transcript",
            "payload": record.completion_payload(),
            "idempotency_key": key,
        }
        headers = {
            "Authorization": f"Bearer {self._bridge_token}",
            "X-Illo-Idempotency-Key": key,
        }
        last_error = "unknown callback failure"
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in range(1, 4):
                try:
                    response = await client.post(self._url, json=envelope, headers=headers)
                    response.raise_for_status()
                    return
                except (httpx.HTTPError, OSError) as exc:
                    last_error = str(exc)
                    logger.warning(
                        "Meetbot completion callback attempt %d/3 failed for session %s: %s",
                        attempt,
                        record.session_id,
                        exc,
                    )
                    if attempt < 3:
                        await self._sleep(float(2 ** (attempt - 1)))

        self._write_dead_letter(record.session_id, envelope, last_error)

    def _write_dead_letter(
        self,
        session_id: str,
        envelope: dict[str, object],
        error: str,
    ) -> None:
        self._private_root.mkdir(parents=True, exist_ok=True)
        path = self._private_root / f"dead-letter-meeting-{session_id}.json"
        temporary = path.with_name(f".{path.name}.tmp")
        content = {
            "callback_url": self._url,
            "attempts": 3,
            "last_error": error,
            "envelope": envelope,
        }
        temporary.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        logger.error("Meetbot completion callback saved to dead letter %s", path)
