"""Async client for the compose-internal meetbot HTTP service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from brain.platform.async_io import async_http_client


MEETBOT_URL_ENV = "ILLO_MEETBOT_URL"
MEETBOT_TOKEN_ENV = "ILLO_MEETBOT_TOKEN"
JOIN_POLL_TIMEOUT_SECONDS = 8.0
JOIN_POLL_INTERVAL_SECONDS = 1.0
_TERMINAL_JOIN_STATES = frozenset({"captions_flowing", "ended", "failed"})


class MeetbotConfigurationError(RuntimeError):
    """The brain runtime cannot call meetbot because configuration is missing."""


class MeetbotServiceError(RuntimeError):
    """A meetbot request failed or returned an invalid response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = dict(payload or {})


@dataclass(frozen=True, slots=True)
class MeetbotSettings:
    base_url: str
    token: str

    @classmethod
    def from_env(cls) -> "MeetbotSettings":
        base_url = str(os.getenv(MEETBOT_URL_ENV) or "").strip().rstrip("/")
        if not base_url:
            raise MeetbotConfigurationError(
                "meetbot service is not configured; set ILLO_MEETBOT_URL on the api and worker"
            )
        token = str(os.getenv(MEETBOT_TOKEN_ENV) or "").strip()
        if not token:
            raise MeetbotConfigurationError(
                "meetbot service token is not configured; set ILLO_MEETBOT_TOKEN on the api and worker"
            )
        return cls(base_url=base_url, token=token)


class MeetbotClient:
    """Narrow typed client for the meetbot v1 HTTP contract."""

    def __init__(self, settings: MeetbotSettings | None = None) -> None:
        self.settings = settings or MeetbotSettings.from_env()

    async def join(
        self,
        *,
        meeting_url: str,
        display_name: str | None = None,
        origin: Mapping[str, str] | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "meeting_url": str(meeting_url or "").strip(),
            "origin": dict(origin or {}),
        }
        if display_name is not None and str(display_name).strip():
            body["display_name"] = str(display_name).strip()
        if requested_by is not None and str(requested_by).strip():
            body["requested_by"] = str(requested_by).strip()
        return await self._request("POST", "/join", json_body=body)

    async def status(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/sessions/{_session_id(session_id)}")

    async def leave(self, session_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{_session_id(session_id)}/leave",
        )

    async def chat(self, session_id: str, *, text: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{_session_id(session_id)}/chat",
            json_body={"text": str(text or "")},
        )

    async def poll_join_status(
        self,
        session_id: str,
        *,
        initial: Mapping[str, Any] | None = None,
        timeout_seconds: float = JOIN_POLL_TIMEOUT_SECONDS,
        interval_seconds: float = JOIN_POLL_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        """Poll briefly without turning lobby or admission into fake success."""

        current = dict(initial or {})
        if _status(current) in _TERMINAL_JOIN_STATES:
            return current

        async def poll() -> dict[str, Any]:
            nonlocal current
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(0.0, float(timeout_seconds))
            first_attempt = True
            while first_attempt or loop.time() < deadline:
                first_attempt = False
                try:
                    current = await self.status(session_id)
                except MeetbotServiceError as exc:
                    current["poll_warning"] = str(exc)
                    return current
                if _status(current) in _TERMINAL_JOIN_STATES:
                    return current
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return current
                await asyncio.sleep(min(max(0.0, interval_seconds), remaining))
            return current

        try:
            async with asyncio.timeout(max(0.01, float(timeout_seconds))):
                return await poll()
        except TimeoutError:
            return current

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"X-Meetbot-Token": self.settings.token}
        try:
            async with async_http_client(
                timeout=httpx.Timeout(5.0, connect=2.0),
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.settings.base_url}{path}",
                    headers=headers,
                    json=dict(json_body) if json_body is not None else None,
                )
        except httpx.HTTPError as exc:
            raise MeetbotServiceError(
                "meetbot service could not be reached; check ILLO_MEETBOT_URL and the meetbot container"
            ) from exc

        payload = _response_payload(response)
        if not response.is_success:
            detail = _error_detail(payload)
            if response.status_code == 409 and payload.get("active_session_id"):
                detail = (
                    f"meetbot is already in session {payload['active_session_id']}; "
                    "leave that session before joining another meeting"
                )
            raise MeetbotServiceError(
                detail or f"meetbot returned HTTP {response.status_code}",
                status_code=response.status_code,
                payload=payload,
            )
        return payload


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError as exc:
        raise MeetbotServiceError("meetbot returned a response that was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise MeetbotServiceError("meetbot returned a JSON response that was not an object")
    return dict(payload)


def _error_detail(payload: Mapping[str, Any]) -> str:
    detail = payload.get("detail") or payload.get("error") or payload.get("message")
    if isinstance(detail, list):
        return "; ".join(str(item) for item in detail)
    return str(detail or "").strip()


def _session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise ValueError("session_id is required")
    return quote(session_id, safe="")


def _status(payload: Mapping[str, Any]) -> str:
    return str(payload.get("status") or "").strip().lower()


__all__ = [
    "JOIN_POLL_INTERVAL_SECONDS",
    "JOIN_POLL_TIMEOUT_SECONDS",
    "MEETBOT_TOKEN_ENV",
    "MEETBOT_URL_ENV",
    "MeetbotClient",
    "MeetbotConfigurationError",
    "MeetbotServiceError",
    "MeetbotSettings",
]
