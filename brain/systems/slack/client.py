"""Small Slack Web API client used by the self-hosted Slack connector/tools."""

from __future__ import annotations

import os
from typing import Any

import httpx


class SlackConfigurationError(RuntimeError):
    """Raised when the self-hosted Slack connector is not configured."""


class SlackApiError(RuntimeError):
    """Raised when Slack Web API returns an error."""


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

    async def api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
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
        if not data.get("ok"):
            raise SlackApiError(str(data.get("error") or "slack_api_error"))
        return dict(data)

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
        return await self.api_call("chat.postMessage", payload)

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
        return await self.api_call("chat.postEphemeral", payload)

    async def conversation_replies(
        self,
        *,
        channel: str,
        thread_ts: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await self.api_call(
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
        return await self.api_call("conversations.history", payload)

    async def auth_test(self) -> dict[str, Any]:
        return await self.api_call("auth.test", {})


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
