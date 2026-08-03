"""Slack-owned route resolution and trigger metadata for deferred delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.systems.slack.triggers import (
    SLACK_MESSAGE_ENVELOPE_KIND,
    SLACK_REPLY_TOOL,
    SLACK_SURFACE,
)


DEFAULT_DELIVERY_CHANNEL = "#alerts"


@dataclass(frozen=True, slots=True)
class SlackDeliveryRoute:
    """One available Slack connection and its resolved reply destination."""

    team_id: str
    bot_user_id: str | None
    channel: str
    thread_ts: str | None
    routing: str


@dataclass(frozen=True, slots=True)
class SlackDeliveryTrigger:
    """Slack-owned metadata and intake target for one deferred delivery."""

    metadata: dict[str, Any]
    target: dict[str, Any]


async def resolve_delivery_route(
    session: AsyncSession,
    *,
    org_id: str,
    channel: str | None,
    thread_ts: str | None,
) -> SlackDeliveryRoute | None:
    """Resolve the first active Slack connection and a safe destination."""

    stmt = (
        select(ExternalAgentConnectionRow)
        .where(
            ExternalAgentConnectionRow.org_id == str(org_id),
            func.lower(ExternalAgentConnectionRow.agent_kind) == "slack",
            ExternalAgentConnectionRow.disabled_at.is_(None),
            func.lower(ExternalAgentConnectionRow.status) != "disabled",
        )
        .order_by(
            ExternalAgentConnectionRow.created_at.asc(),
            ExternalAgentConnectionRow.id.asc(),
        )
        .limit(1)
    )
    connection = (await session.scalars(stmt)).first()
    if connection is None:
        return None

    slack_metadata = dict((connection.metadata_ or {}).get("slack") or {})
    team_id = str(
        slack_metadata.get("team_id") or connection.remote_agent_id or ""
    ).strip()
    if not team_id:
        return None

    resolved_channel = str(channel or "").strip()
    routing = "slack_origin"
    if not resolved_channel:
        resolved_channel = DEFAULT_DELIVERY_CHANNEL
        routing = "slack_alerts"
    resolved_thread_ts = str(thread_ts or "").strip() or None
    if resolved_channel.startswith("D"):
        resolved_thread_ts = None
    return SlackDeliveryRoute(
        team_id=team_id,
        bot_user_id=(
            str(slack_metadata.get("bot_user_id") or "").strip() or None
        ),
        channel=resolved_channel,
        thread_ts=resolved_thread_ts,
        routing=routing,
    )


def build_delivery_trigger(
    route: SlackDeliveryRoute,
    *,
    message_ts: str,
    slack_user_id: str | None,
    text: str,
    triggering_surface: str,
) -> SlackDeliveryTrigger:
    """Build the standard Slack trigger, response target, and intake target."""

    channel_type = "im" if route.channel.startswith("D") else "channel"
    surface = "slack_thread" if route.thread_ts else "slack_channel"
    slack_trigger = {
        "team_id": route.team_id,
        "channel_id": route.channel,
        "channel_type": channel_type,
        "message_ts": message_ts,
        "thread_ts": route.thread_ts,
        "slack_user_id": slack_user_id,
        "bot_user_id": route.bot_user_id,
        "text": text,
        "surface": surface,
        "response_target": {
            "channel_id": route.channel,
            "thread_ts": route.thread_ts,
            "visibility": "public",
        },
    }
    return SlackDeliveryTrigger(
        metadata={
            "originating_surface": SLACK_SURFACE,
            "triggering_surface": triggering_surface,
            "source_surface": SLACK_SURFACE,
            "required_response_tool": SLACK_REPLY_TOOL,
            "final_answer_target_surface": SLACK_SURFACE,
            "slack_trigger": slack_trigger,
        },
        target={
            "kind": SLACK_MESSAGE_ENVELOPE_KIND,
            "team_id": route.team_id,
            "channel_id": route.channel,
            "message_ts": message_ts,
            "thread_ts": route.thread_ts,
            "surface": surface,
        },
    )


__all__ = [
    "DEFAULT_DELIVERY_CHANNEL",
    "SlackDeliveryRoute",
    "SlackDeliveryTrigger",
    "build_delivery_trigger",
    "resolve_delivery_route",
]
