"""Small Slack Web API client used by the self-hosted Slack connector/tools."""

from __future__ import annotations

import os
from typing import Any

from brain.platform.async_io import async_http_client


# Slack recommends at most 4,000 characters for a top-level ``text`` field.
# Split before the API boundary so a receiver-side limit cannot discard the lede.
SLACK_MESSAGE_TEXT_CHARS = 4000


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


class SlackDeliveryError(SlackApiError):
    """Raised when Slack cannot prove that submitted text was stored intact."""

    def __init__(
        self,
        error: str,
        *,
        submitted_text: str,
        posted_text: str | None,
        chunk_count: int,
        detail: str,
    ) -> None:
        self.submitted_chars = len(submitted_text)
        self.posted_chars = len(posted_text) if posted_text is not None else None
        self.submitted_bytes = len(submitted_text.encode("utf-8"))
        self.posted_bytes = len(posted_text.encode("utf-8")) if posted_text is not None else None
        self.chunk_count = chunk_count
        self.truncated = None if posted_text is None else posted_text != submitted_text
        self.detail = detail
        super().__init__(error)

    def to_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.error,
            "detail": self.detail,
            "submitted_chars": self.submitted_chars,
            "posted_chars": self.posted_chars,
            "submitted_bytes": self.submitted_bytes,
            "posted_bytes": self.posted_bytes,
            "chunk_count": self.chunk_count,
            "truncated": self.truncated,
        }


def _message_text_chunks(text: str) -> list[str]:
    if not text:
        return [text]
    return [
        text[start : start + SLACK_MESSAGE_TEXT_CHARS]
        for start in range(0, len(text), SLACK_MESSAGE_TEXT_CHARS)
    ]


def _stored_message_text(response: dict[str, Any]) -> str | None:
    message = response.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("text"), str):
        return None
    return message["text"]


def _posted_message_ts(response: dict[str, Any]) -> str | None:
    message = response.get("message") if isinstance(response.get("message"), dict) else {}
    return str(response.get("ts") or message.get("ts") or "").strip() or None


def _response_warns_of_truncation(response: dict[str, Any]) -> bool:
    metadata = response.get("response_metadata")
    warnings = metadata.get("warnings") if isinstance(metadata, dict) else []
    values = [response.get("warning"), *(warnings if isinstance(warnings, list) else [])]
    return any(str(value or "").strip() == "message_truncated" for value in values)


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

    async def _post_bytes(self, url: str, *, data: bytes, content_type: str) -> None:
        async with async_http_client(timeout=self.timeout) as client:
            response = await client.post(
                url,
                headers={"Content-Type": content_type or "application/octet-stream"},
                content=data,
            )
            response.raise_for_status()

    async def post_message(
        self,
        *,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        submitted_text = str(text)
        chunks = _message_text_chunks(submitted_text)
        responses: list[dict[str, Any]] = []
        stored_chunks: list[str] = []
        continuation_thread_ts = thread_ts

        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"channel": channel, "text": chunk}
            if continuation_thread_ts:
                payload["thread_ts"] = continuation_thread_ts
            try:
                response = await self._post("chat.postMessage", payload)
            except Exception as exc:
                if not responses:
                    raise
                raise SlackDeliveryError(
                    "slack_message_delivery_incomplete",
                    submitted_text=submitted_text,
                    posted_text="".join(stored_chunks),
                    chunk_count=len(responses),
                    detail=f"Slack failed while posting continuation {index + 1} of {len(chunks)}: {exc}",
                ) from exc

            responses.append(response)
            stored_text = _stored_message_text(response)
            if stored_text is None:
                raise SlackDeliveryError(
                    "slack_message_delivery_unverified",
                    submitted_text=submitted_text,
                    posted_text=None,
                    chunk_count=len(responses),
                    detail="Slack returned ok without message.text, so delivery integrity cannot be verified.",
                )
            stored_chunks.append(stored_text)
            if stored_text != chunk or _response_warns_of_truncation(response):
                raise SlackDeliveryError(
                    "slack_message_delivery_mismatch",
                    submitted_text=submitted_text,
                    posted_text="".join(stored_chunks),
                    chunk_count=len(responses),
                    detail=(
                        f"Slack stored {len(stored_text)} of {len(chunk)} characters "
                        f"for chunk {index + 1} of {len(chunks)}."
                    ),
                )

            if index == 0 and len(chunks) > 1 and not continuation_thread_ts:
                continuation_thread_ts = _posted_message_ts(response)
                if not continuation_thread_ts:
                    raise SlackDeliveryError(
                        "slack_message_delivery_unverified",
                        submitted_text=submitted_text,
                        posted_text="".join(stored_chunks),
                        chunk_count=len(responses),
                        detail="Slack returned ok without a message timestamp for threaded continuations.",
                    )

        posted_text = "".join(stored_chunks)
        result = dict(responses[0])
        result.update(
            {
                "submitted_chars": len(submitted_text),
                "posted_chars": len(posted_text),
                "submitted_bytes": len(submitted_text.encode("utf-8")),
                "posted_bytes": len(posted_text.encode("utf-8")),
                "chunk_count": len(responses),
                "truncated": posted_text != submitted_text,
            }
        )
        if len(responses) > 1:
            result["continuations"] = [
                {
                    "channel": response.get("channel"),
                    "ts": _posted_message_ts(response),
                    "posted_chars": len(stored_chunk),
                }
                for response, stored_chunk in zip(responses[1:], stored_chunks[1:])
            ]
        return result

    async def post_ephemeral(
        self,
        *,
        channel: str,
        user: str,
        text: str,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        submitted_text = str(text)
        if len(submitted_text) > SLACK_MESSAGE_TEXT_CHARS:
            raise SlackDeliveryError(
                "slack_ephemeral_message_too_long",
                submitted_text=submitted_text,
                posted_text="",
                chunk_count=0,
                detail=(
                    "Ephemeral Slack messages cannot be verified or continued safely; "
                    f"the {len(submitted_text)}-character message was not posted."
                ),
            )
        payload: dict[str, Any] = {
            "channel": channel,
            "user": user,
            "text": submitted_text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = await self._post("chat.postEphemeral", payload)
        if _response_warns_of_truncation(result):
            raise SlackDeliveryError(
                "slack_message_delivery_mismatch",
                submitted_text=submitted_text,
                posted_text=None,
                chunk_count=1,
                detail="Slack warned that the ephemeral message was truncated.",
            )
        return {
            **result,
            "submitted_chars": len(submitted_text),
            "posted_chars": len(submitted_text),
            "submitted_bytes": len(submitted_text.encode("utf-8")),
            "posted_bytes": len(submitted_text.encode("utf-8")),
            "chunk_count": 1,
            "truncated": False,
        }

    async def upload_file(
        self,
        *,
        channel: str,
        file_bytes: bytes,
        filename: str,
        title: str | None = None,
        initial_comment: str | None = None,
        thread_ts: str | None = None,
        alt_txt: str | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """Upload bytes through Slack's external upload flow and share the file."""

        if initial_comment and len(initial_comment) > SLACK_MESSAGE_TEXT_CHARS:
            raise SlackDeliveryError(
                "slack_file_comment_too_long",
                submitted_text=initial_comment,
                posted_text="",
                chunk_count=0,
                detail=(
                    "Slack file initial comments cannot be continued safely; "
                    f"the {len(initial_comment)}-character comment and file were not posted."
                ),
            )

        upload_request: dict[str, Any] = {
            "filename": filename,
            "length": len(file_bytes),
        }
        if alt_txt:
            upload_request["alt_txt"] = alt_txt[:1000]
        upload = await self._post("files.getUploadURLExternal", upload_request)
        upload_url = str(upload.get("upload_url") or "").strip()
        file_id = str(upload.get("file_id") or "").strip()
        if not upload_url or not file_id:
            raise SlackApiError("slack_upload_ticket_missing")

        await self._post_bytes(upload_url, data=file_bytes, content_type=content_type)

        complete_request: dict[str, Any] = {
            "files": [{"id": file_id, "title": title or filename}],
            "channel_id": channel,
        }
        if initial_comment:
            complete_request["initial_comment"] = initial_comment
        if thread_ts:
            complete_request["thread_ts"] = thread_ts
        return await self._post("files.completeUploadExternal", complete_request)

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

    async def add_reaction(
        self,
        *,
        channel: str,
        timestamp: str,
        name: str = "eyes",
    ) -> dict[str, Any]:
        """Add an emoji reaction to a Slack message.

        Slack returns ``already_reacted`` when the emoji is already present; the
        caller may treat that as success. Requires the ``reactions:write`` scope.
        """

        return await self._post(
            "reactions.add",
            {
                "channel": channel,
                "timestamp": timestamp,
                "name": str(name or "eyes").strip().strip(":"),
            },
        )

    async def open_conversation(self, *, users: str) -> dict[str, Any]:
        return await self._post("conversations.open", {"users": users})

    async def conversation_replies(
        self,
        *,
        channel: str,
        thread_ts: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "channel": channel,
            "ts": thread_ts,
            "limit": max(1, min(int(limit or 50), 200)),
        }
        if cursor:
            params["cursor"] = cursor
        return await self._get("conversations.replies", params)

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

    async def conversations_list(
        self,
        *,
        types: str = "public_channel,private_channel,mpim,im",
        limit: int = 200,
        cursor: str | None = None,
        exclude_archived: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "types": types,
            "limit": max(1, min(int(limit or 200), 1000)),
            "exclude_archived": bool(exclude_archived),
        }
        if cursor:
            payload["cursor"] = cursor
        return await self._get("conversations.list", payload)

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


async def slack_web_client_from_runtime(
    *,
    requested_by: str,
    reason: str,
) -> SlackWebClient:
    """Backend Slack client with Vault-first bot-token resolution.

    Deployments store SLACK_BOT_TOKEN in DB-backed runtime secrets, not in
    every service's env (the compose anchor passes it to the connector
    only), so env-only resolution silently strands backend posting/reading
    in the worker and API — the packet-mint E2E on illo-dev caught exactly
    that (2026-07-16). Resolution order mirrors the connector's
    ``SlackConnectorConfig.from_runtime``: env when set, else the runtime
    secret under the connector authority. One owner for the secret.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        from brain.systems.slack.connector import resolve_slack_connector_authority
        from brain.systems.vault.runtime_secrets import (
            RuntimeSecretContext,
            read_runtime_secret,
        )

        org_id = os.environ.get("ILLO_SLACK_ORG_ID") or os.environ.get("ILLO_ORG_ID")
        owner_user_id = (
            os.environ.get("ILLO_SLACK_OWNER_USER_ID")
            or os.environ.get("ILLO_OWNER_USER_ID")
        )
        if not org_id or not owner_user_id:
            org_id, owner_user_id = await resolve_slack_connector_authority(
                org_id=org_id, owner_user_id=owner_user_id
            )
        token = str(
            await read_runtime_secret(
                "SLACK_BOT_TOKEN",
                context=RuntimeSecretContext(actor_user_id=owner_user_id, org_id=org_id),
                reason=reason,
                requested_by=requested_by,
                access="service",
                allow_env_fallback=True,
            )
            or ""
        ).strip()
    if not token:
        raise SlackConfigurationError("SLACK_BOT_TOKEN is required (env or runtime secret)")
    return SlackWebClient(token)
