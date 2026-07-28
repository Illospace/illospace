"""Slack delivery driven entirely by caller-owned policy and presentation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


SlackClientProvider = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class FailureAlertSubject:
    """Presentation fields for one guarded subject's Slack card."""

    identity_label: str
    identity: str
    url_label: str
    url: str
    link_label: str


@dataclass(frozen=True)
class FailureAlertPresentation:
    """Fully composed caller-owned alert copy."""

    title: str
    summary: str


@dataclass(frozen=True)
class SlackFailureAlertPolicy:
    """Slack access, routing, and error fallback policy."""

    provide_client: SlackClientProvider
    requested_by: str
    reason: str
    channel: str
    unknown_error_text: str


async def _resolve_channel(client: Any, configured: str) -> str:
    channel = str(configured or "").strip()
    if not channel.startswith("#"):
        return channel

    target_name = channel.removeprefix("#")
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        response = await client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
            exclude_archived=True,
        )
        for candidate in response.get("channels") or []:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("name") or "") == target_name:
                return str(candidate.get("id") or channel)
        metadata = response.get("response_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            return channel
        seen_cursors.add(next_cursor)
        cursor = next_cursor


async def async_deliver_failure_alert(
    *,
    policy: SlackFailureAlertPolicy,
    subject: FailureAlertSubject,
    presentation: FailureAlertPresentation,
    error_text: str,
) -> None:
    """Post one fully composed failure alert."""
    client = await policy.provide_client(
        requested_by=policy.requested_by,
        reason=policy.reason,
    )
    channel = await _resolve_channel(client, policy.channel)
    first_error_line = next(
        (
            line.strip()
            for line in str(error_text or "").splitlines()
            if line.strip()
        ),
        policy.unknown_error_text,
    )
    await client.post_message(
        channel=channel,
        text=(
            f"{presentation.title}\n"
            f"{subject.identity_label}: {subject.identity}\n"
            f"{presentation.summary}\n"
            f"Error: {first_error_line}\n"
            f"{subject.url_label}: <{subject.url}|{subject.link_label}>"
        ),
    )
