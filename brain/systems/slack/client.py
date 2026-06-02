"""Small Slack Web API client used by the self-hosted Slack connector/tools."""

from __future__ import annotations

import os
from typing import Any

from brain.platform.async_io import async_http_client


class SlackConfigurationError(RuntimeError):
    """Raised when the self-hosted Slack connector is not configured."""


class SlackApiError(RuntimeError):
    """Raised when Slack Web API returns an error."""

    def __init__(self, error: str, *, response_metadata: dict[str, Any] | None = None) -> None:
        self.error = error
        self.response_metadata = dict(response_metadata or {})
        messages = self.response_metadata.get("messages")
        details = (
            "; ".join(str(message) for message in messages if message)
            if isinstance(messages, list)
            else ""
        )
        super().__init__(f"{error}: {details}" if details else error)


class SlackWebClient:
    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = "https://slack.com/api",
        timeout: float = 10.0,
    ) -> None:
        token = str(bot_token or "").strip()
        if not token:
            raise SlackConfigurationError("SLACK_BOT_TOKEN is required")
        self.bot_token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _coerce_response(self, data: dict[str, Any]) -> dict[str, Any]:
        if not data.get("ok"):
            metadata = data.get("response_metadata")
            raise SlackApiError(
                str(data.get("error") or "slack_api_error"),
                response_metadata=metadata if isinstance(metadata, dict) else None,
            )
        return dict(data)

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with async_http_client(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/{method}",
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        return self._coerce_response(dict(data))

    async def _get(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with async_http_client(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.base_url}/{method}",
                headers={"Authorization": f"Bearer {self.bot_token}"},
                params=params,
            )
            response.raise_for_status()
            data = response.json()
        return self._coerce_response(dict(data))

    async def post_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel,
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._post("chat.postMessage", payload)

    async def post_ephemeral(
        self,
        *,
        channel: str,
        user: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel,
            "user": user,
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return await self._post("chat.postEphemeral", payload)

    async def set_assistant_status(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        status: str,
        loading_messages: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "status": status,
        }
        if loading_messages:
            payload["loading_messages"] = loading_messages[:10]
        return await self._post("assistant.threads.setStatus", payload)

    async def open_conversation(self, *, users: str) -> dict[str, Any]:
        return await self._post("conversations.open", {"users": users})

    async def conversation_replies(
        self,
        *,
        channel: str,
        thread_ts: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await self._get(
            "conversations.replies",
            {
                "channel": channel,
                "ts": thread_ts,
                "limit": max(1, min(int(limit or 50), 200)),
            },
        )

    async def conversation_history(
        self,
        *,
        channel: str,
        limit: int = 50,
        latest: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": channel,
            "limit": max(1, min(int(limit or 50), 200)),
        }
        if latest:
            payload["latest"] = latest
            payload["inclusive"] = True
        return await self._post("conversations.history", payload)

    async def auth_test(self) -> dict[str, Any]:
        return await self._post("auth.test", {})


def slack_bot_token_from_env() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise SlackConfigurationError("SLACK_BOT_TOKEN is required")
    return token


def slack_app_token_from_env() -> str:
    token = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if not token:
        raise SlackConfigurationError("SLACK_APP_TOKEN is required")
    return token


def slack_web_client_from_env() -> SlackWebClient:
    return SlackWebClient(slack_bot_token_from_env())
