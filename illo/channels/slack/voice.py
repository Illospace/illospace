"""Bound the opt-in audio path before any request leaves Slack."""

import asyncio
import json
import math
import os
import tempfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx
from psycopg.types.json import Jsonb

from illo.channels.slack.clips import pending, transcript
from illo.channels.slack.files import download, slack_call
from illo.channels.slack.limits import lock
from illo.ledger import audit, kv
from illo.workspace import Person

if TYPE_CHECKING:
    from illo.app import AppContext
    from illo.gateway import InboundEvent
    from illo.gateway.principal import Authority


class OpenAITranscriber:
    def __init__(
        self,
        ctx: "AppContext",
        event: "InboundEvent",
        authority: "Authority",
        bot_id: str,
        working_ts: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        slack_timeout: float = 15,
        slack_poll: float = 1,
        team_id: str | None = None,
    ):
        self.ctx, self.event, self.authority = ctx, event, authority
        self.bot_id, self.working_ts = bot_id, working_ts
        self.transport = transport
        self.config = ctx.workspace.config.voice
        self.slack_timeout, self.slack_poll = slack_timeout, slack_poll
        self.team_id = team_id

    async def trusted_person(self) -> Person | None:
        """Require a mapped human and check thread authors before any audio leaves Slack."""
        payload = self.event.payload
        if self.authority.tainted or self.authority.profile not in {"person_dm", "team_channel"}:
            return None
        if any(
            payload.get(key)
            for key in (
                "bot",
                "bot_id",
                "webhook",
                "form",
                "forwarded",
                "from_automation",
                "outside_workspace",
                "contains_outside_text",
            )
        ):
            return None
        person = next(
            (p for p in self.ctx.workspace.people if p.active and p.slack_id == self.event.sender),
            None,
        )
        if not person:
            return None
        if payload.get("thread_ts"):
            async with self.ctx.pool.connection() as conn:
                if await kv.get(
                    conn, f"slack:thread-tainted:{payload['channel_id']}:{payload['thread_ts']}"
                ):
                    return None
            known = {p.slack_id for p in self.ctx.workspace.people if p.active and p.slack_id}
            cursor = None
            while True:
                response = await self.ctx.slack_client.conversations_replies(
                    channel=payload["channel_id"],
                    ts=payload["thread_ts"],
                    limit=100,
                    cursor=cursor,
                    latest=payload["message_ts"],
                    inclusive=False,
                )
                for message in response.get("messages", []):
                    if message.get("ts") in {payload["message_ts"], self.working_ts}:
                        continue
                    user = message.get("user")
                    if user == self.bot_id:
                        continue
                    outside_workspace = False
                    if message.get("user_team"):
                        if not self.team_id:
                            self.team_id = (await self.ctx.slack_client.auth_test())["team_id"]
                        outside_workspace = message["user_team"] != self.team_id
                    if (
                        user not in known
                        or message.get("bot_id")
                        or message.get("subtype")
                        or outside_workspace
                    ):
                        payload["contains_outside_text"] = True
                        return None
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        return person

    async def admit(self, person_key: str) -> bool:
        """Persist attempts before download. Restarts and concurrent requests keep the cap."""
        key = f"slack:voice:{person_key}"
        async with self.ctx.pool.connection() as conn:
            await lock(conn, key)
            timestamp = datetime.now(UTC).timestamp()
            recent = [at for at in await kv.get(conn, key, []) if at > timestamp - 3600]
            if len(recent) >= self.config.max_per_person_per_hour:
                return False
            await kv.set(conn, key, [*recent, timestamp])
            await audit.write(
                conn,
                principal=f"person:{person_key}",
                tool="slack_voice_transcribe",
                args={},
                decision="allow",
                reason="Transcription attempt",
            )
        return True

    async def __call__(self, file_id: str) -> str | None:
        if self.config.transcription != "openai":
            return None
        try:
            async with asyncio.timeout(self.config.timeout_s):
                return await self.transcribe(file_id)
        except Exception:
            # Neither exceptions nor response bodies are safe to log here.
            return None

    async def transcribe(self, file_id: str) -> str | None:
        person = await self.trusted_person()
        if not person:
            return None
        # Never use a URL from the event. Fetch this file's metadata from Slack.
        response = await slack_call(self.ctx.slack_client.files_info, file=file_id)
        file = response["file"]
        if file.get("id") != file_id:
            return None
        text = transcript(file)
        if not text and pending(file):
            async with asyncio.timeout(self.slack_timeout):
                while not text and pending(file):
                    await asyncio.sleep(self.slack_poll)
                    response = await slack_call(self.ctx.slack_client.files_info, file=file_id)
                    file = response["file"]
                    if file.get("id") != file_id:
                        return None
                    text = transcript(file)
        if text:
            return text
        if file.get("user") != person.slack_id:
            return None
        if not (file.get("mimetype", "").startswith("audio/") or file.get("is_audio")):
            return None
        # Fixed names omit the private Slack filename and URL.
        extension = {
            "m4a": "m4a",
            "mp3": "mp3",
            "wav": "wav",
            "webm": "webm",
            "mp4": "mp4",
            "mpeg": "mpeg",
            "mpga": "mpga",
        }.get(file.get("filetype"))
        if not extension:
            return None
        duration = file.get("duration_ms")
        size = file.get("size")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (float, int))
            or not math.isfinite(duration)
            or not 0 < duration <= self.config.max_duration_s * 1000
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= self.config.max_bytes
        ):
            return None
        url = file.get("url_private_download", "")
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        bot_token = self.ctx.config.slack_bot_token
        base = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
        endpoint = urlsplit(base)
        if (
            not api_key
            or not bot_token
            or endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            return None
        if not await self.admit(person.key):
            return None
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.config.timeout_s,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            # TemporaryFile has no persistent name and closes on every exit path.
            with tempfile.TemporaryFile() as audio:
                body = await download(client, url, bot_token, self.config.max_bytes)
                if body is None:
                    return None
                audio.write(body)
                audio.seek(0)
                async with client.stream(
                    "POST",
                    base.rstrip("/") + "/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": self.config.model, "response_format": "json"},
                    files={"file": (f"clip.{extension}", audio, "application/octet-stream")},
                ) as result:
                    if result.status_code != 200:
                        return None
                    body = bytearray()
                    async for chunk in result.aiter_bytes(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > 262144:
                            return None
                text = json.loads(body).get("text")
                if not isinstance(text, str) or not text.strip():
                    return None
        metadata = {
            "clip_transcribed_by": "openai",
            "clip_duration_ms": self.event.payload.get("clip_duration_ms", 0) + duration,
            "clip_transcription_count": self.event.payload.get("clip_transcription_count", 0) + 1,
        }
        # Keep only service and duration in this record. The gateway treats speech like typed text.
        async with self.ctx.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE events SET payload=jsonb_set(payload,'{payload}',
                    COALESCE(payload->'payload','{}'::jsonb) || %s)
                WHERE channel='slack' AND external_id=%s
            """,
                (Jsonb(metadata), self.event.external_id),
            )
        self.event.payload.update(metadata)
        return text.strip()
