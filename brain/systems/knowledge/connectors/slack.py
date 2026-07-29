"""Bounded Slack-thread backfill and event-driven incremental indexing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_CONNECTOR_BATCH_SIZE
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundEventRow
from brain.systems.knowledge.connectors.base import KnowledgeDraft
from brain.systems.slack.monitored_intakes import visible_slack_content
from brain.systems.slack.monitors import monitored_channels


_CURSOR_VERSION = 1
_SLACK_MESSAGE_KIND = "slack_message"
_THREAD_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True)
class _Channel:
    id: str
    name: str | None


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_cursor_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return _utc(parsed)


def _slack_datetime(value: Any) -> datetime | None:
    try:
        timestamp = Decimal(_clean(value))
    except InvalidOperation:
        return None
    seconds = int(timestamp)
    micros = int((timestamp - seconds) * 1_000_000)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
            microsecond=micros
        )
    except (OSError, OverflowError, ValueError):
        return None


def _slack_timestamp_key(message: Mapping[str, Any]) -> tuple[Decimal, str]:
    raw = _clean(message.get("ts"))
    try:
        return Decimal(raw), raw
    except InvalidOperation:
        return Decimal(0), raw


def _message_author(message: Mapping[str, Any]) -> str:
    return _clean(
        message.get("user")
        or message.get("bot_id")
        or message.get("username")
    ) or "unknown"


def _message_text(message: Mapping[str, Any]) -> str:
    # Knowledge needs the source text before the shared 20k row bound. The
    # monitor's default 4k prompt budget is intentionally disabled here.
    visible = visible_slack_content(message, limit=None)
    return visible.message_text or visible.block_text


def _thread_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in messages:
        text = _message_text(message)
        if not text:
            continue
        timestamp = _slack_datetime(message.get("ts"))
        rendered_timestamp = timestamp.isoformat() if timestamp else _clean(message.get("ts"))
        lines.append(f"[{rendered_timestamp}] {_message_author(message)}: {text}")
    return "\n".join(lines)


def _channel_set_digest(channels: list[_Channel]) -> str:
    """Bounded marker that restarts backfill when monitor configuration changes."""

    payload = "\n".join(channel.id for channel in channels)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _slack_metadata(connection: ExternalAgentConnectionRow) -> dict[str, Any]:
    metadata = dict(connection.metadata_ or {})
    slack = metadata.get("slack")
    return dict(slack) if isinstance(slack, Mapping) else {}


def _team_id(connection: ExternalAgentConnectionRow) -> str:
    return _clean(_slack_metadata(connection).get("team_id") or connection.remote_agent_id)


def _configured_channels(connection: ExternalAgentConnectionRow) -> list[_Channel]:
    channels = [
        _Channel(
            id=_clean(entry.get("channel_id")),
            name=_clean(entry.get("channel_name")) or None,
        )
        for entry in monitored_channels(connection)
        if entry.get("enabled", True) and _clean(entry.get("channel_id"))
    ]
    return sorted(channels, key=lambda channel: channel.id)


async def _monitored_connection(
    session: AsyncSession,
) -> tuple[ExternalAgentConnectionRow | None, list[_Channel]]:
    rows = list(
        (
            await session.scalars(
                select(ExternalAgentConnectionRow)
                .where(
                    ExternalAgentConnectionRow.agent_kind == "slack",
                    ExternalAgentConnectionRow.transport == "slack_socket_mode",
                    ExternalAgentConnectionRow.disabled_at.is_(None),
                )
                .order_by(
                    ExternalAgentConnectionRow.created_at.asc(),
                    ExternalAgentConnectionRow.id.asc(),
                )
            )
        ).all()
    )
    rows.sort(
        key=lambda connection: (
            _clean(connection.status) not in {"online", "connected"},
            connection.created_at,
            str(connection.id),
        )
    )
    for connection in rows:
        channels = _configured_channels(connection)
        if channels:
            return connection, channels
    return None, []


def _next_cursor(
    *,
    connection_id: str,
    channel_set_digest: str,
    phase: str,
    channel_id: str | None,
    history_cursor: str | None,
    event_created_at: str | None,
    event_id: str | None,
) -> dict[str, Any]:
    return {
        "version": _CURSOR_VERSION,
        "connection_id": connection_id,
        "channel_set_digest": channel_set_digest,
        "phase": phase,
        "channel_id": channel_id,
        "history_cursor": history_cursor,
        "event_created_at": event_created_at,
        "event_id": event_id,
    }


class SlackKnowledgeConnector:
    """Index monitored Slack channels as complete, replaceable threads.

    A cursor-paged history sweep owns the initial backfill. Once complete,
    durable Slack inbound events identify changed thread roots; each event
    causes the canonical parent and every reply to be fetched again. When that
    event queue is caught up, the same bounded history cursor continuously
    refreshes monitored channels so message edits and Illo-authored replies that
    intake intentionally ignores cannot leave the index stale forever.
    """

    source_key = "slack"

    def __init__(
        self,
        *,
        client: Any | None = None,
        max_items: int = KNOWLEDGE_CONNECTOR_BATCH_SIZE,
    ) -> None:
        self._client = client
        self.max_items = max(1, int(max_items))

    async def _resolve_client(self, connection: ExternalAgentConnectionRow) -> Any:
        if self._client is None:
            from brain.systems.slack.client import slack_web_client_from_runtime

            self._client = await slack_web_client_from_runtime(
                requested_by="knowledge_index_sync",
                reason="Backfill and incrementally refresh monitored Slack threads.",
                org_id=str(connection.org_id),
                owner_user_id=str(connection.owner_user_id),
            )
        return self._client

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> tuple[list[KnowledgeDraft], dict[str, Any]]:
        connection, channels = await _monitored_connection(session)
        if connection is None:
            return [], dict(cursor)

        connection_id = str(connection.id)
        if not _team_id(connection):
            raise RuntimeError("Monitored Slack connection has no stable team_id")
        current_channel_set_digest = _channel_set_digest(channels)
        cursor_is_current = (
            cursor.get("version") == _CURSOR_VERSION
            and _clean(cursor.get("connection_id")) == connection_id
            and _clean(cursor.get("channel_set_digest"))
            == current_channel_set_digest
        )
        state = dict(cursor) if cursor_is_current else _next_cursor(
            connection_id=connection_id,
            channel_set_digest=current_channel_set_digest,
            phase="backfill",
            channel_id=channels[0].id,
            history_cursor=None,
            event_created_at=None,
            event_id=None,
        )
        client = await self._resolve_client(connection)
        if state.get("phase") != "incremental":
            return await self._backfill(
                client=client,
                connection=connection,
                channels=channels,
                state=state,
            )
        return await self._incremental(
            session=session,
            client=client,
            connection=connection,
            channels=channels,
            state=state,
        )

    async def _backfill(
        self,
        *,
        client: Any,
        connection: ExternalAgentConnectionRow,
        channels: list[_Channel],
        state: dict[str, Any],
    ) -> tuple[list[KnowledgeDraft], dict[str, Any]]:
        channel_ids = [channel.id for channel in channels]
        active_channel = _clean(state.get("channel_id"))
        try:
            active_index = channel_ids.index(active_channel)
        except ValueError:
            active_index = 0
        drafts: list[KnowledgeDraft] = []
        history_cursor = _clean(state.get("history_cursor")) or None

        for channel_index in range(active_index, len(channels)):
            channel = channels[channel_index]
            remaining = self.max_items - len(drafts)
            if remaining <= 0:
                return drafts, {
                    **state,
                    "channel_id": channel.id,
                    "history_cursor": history_cursor,
                }
            payload = await client.conversation_history(
                channel=channel.id,
                limit=remaining,
                cursor=history_cursor,
            )
            roots = [
                dict(message)
                for message in payload.get("messages") or []
                if isinstance(message, Mapping) and _clean(message.get("ts"))
            ]
            for root in roots[:remaining]:
                drafts.append(
                    await self._draft_for_thread(
                        client=client,
                        connection=connection,
                        channel=channel,
                        thread_ts=_clean(root.get("thread_ts") or root.get("ts")),
                        root_fallback=root,
                    )
                )
            next_history_cursor = _clean(
                (payload.get("response_metadata") or {}).get("next_cursor")
            )
            if next_history_cursor:
                return drafts, {
                    **state,
                    "channel_id": channel.id,
                    "history_cursor": next_history_cursor,
                }
            history_cursor = None
            if len(drafts) >= self.max_items and channel_index + 1 < len(channels):
                return drafts, {
                    **state,
                    "channel_id": channels[channel_index + 1].id,
                    "history_cursor": None,
                }

        return drafts, _next_cursor(
            connection_id=str(connection.id),
            channel_set_digest=_channel_set_digest(channels),
            phase="incremental",
            channel_id=None,
            history_cursor=None,
            event_created_at=state.get("event_created_at"),
            event_id=state.get("event_id"),
        )

    async def _incremental(
        self,
        *,
        session: AsyncSession,
        client: Any,
        connection: ExternalAgentConnectionRow,
        channels: list[_Channel],
        state: dict[str, Any],
    ) -> tuple[list[KnowledgeDraft], dict[str, Any]]:
        stmt = (
            select(InboundEventRow)
            .where(
                InboundEventRow.connection_id == str(connection.id),
                InboundEventRow.kind == _SLACK_MESSAGE_KIND,
            )
            .order_by(InboundEventRow.created_at.asc(), InboundEventRow.id.asc())
            .limit(self.max_items)
        )
        event_created_at = _event_cursor_datetime(state.get("event_created_at"))
        event_id = _clean(state.get("event_id"))
        if event_created_at is not None:
            stmt = stmt.where(
                or_(
                    InboundEventRow.created_at > event_created_at,
                    and_(
                        InboundEventRow.created_at == event_created_at,
                        InboundEventRow.id > event_id,
                    ),
                )
            )
        events = list((await session.scalars(stmt)).all())
        if not events:
            refresh_state = {
                **state,
                "channel_id": _clean(state.get("channel_id")) or channels[0].id,
            }
            return await self._backfill(
                client=client,
                connection=connection,
                channels=channels,
                state=refresh_state,
            )

        channels_by_id = {channel.id: channel for channel in channels}
        seen_threads: set[tuple[str, str]] = set()
        drafts: list[KnowledgeDraft] = []
        for event in events:
            normalized = dict(event.normalized_payload or {})
            payload = normalized.get("payload")
            payload = dict(payload) if isinstance(payload, Mapping) else {}
            channel_id = _clean(payload.get("channel_id"))
            thread_ts = _clean(payload.get("thread_ts") or payload.get("message_ts"))
            thread_key = (channel_id, thread_ts)
            if (
                channel_id not in channels_by_id
                or not thread_ts
                or thread_key in seen_threads
            ):
                continue
            seen_threads.add(thread_key)
            drafts.append(
                await self._draft_for_thread(
                    client=client,
                    connection=connection,
                    channel=channels_by_id[channel_id],
                    thread_ts=thread_ts,
                )
            )

        last_event = events[-1]
        return drafts, {
            **state,
            "event_created_at": _utc(last_event.created_at).isoformat(),
            "event_id": str(last_event.id),
        }

    async def _draft_for_thread(
        self,
        *,
        client: Any,
        connection: ExternalAgentConnectionRow,
        channel: _Channel,
        thread_ts: str,
        root_fallback: Mapping[str, Any] | None = None,
    ) -> KnowledgeDraft:
        messages_by_ts: dict[str, dict[str, Any]] = {}
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            response = await client.conversation_replies(
                channel=channel.id,
                thread_ts=thread_ts,
                limit=_THREAD_PAGE_SIZE,
                cursor=cursor,
            )
            for message in response.get("messages") or []:
                if isinstance(message, Mapping) and _clean(message.get("ts")):
                    messages_by_ts[_clean(message.get("ts"))] = dict(message)
            next_cursor = _clean(
                (response.get("response_metadata") or {}).get("next_cursor")
            )
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("Slack returned a repeated conversations.replies cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if root_fallback is not None:
            root_ts = _clean(root_fallback.get("ts"))
            if root_ts:
                messages_by_ts.setdefault(root_ts, dict(root_fallback))
        messages = sorted(messages_by_ts.values(), key=_slack_timestamp_key)
        if not messages:
            raise RuntimeError(
                f"Slack returned no messages for {channel.id}:{thread_ts}"
            )

        root = messages[0]
        participants = sorted({_message_author(message) for message in messages})
        root_text = _message_text(root)
        title_lede = " ".join(root_text.split())[:160] or f"Thread {thread_ts}"
        channel_label = f"#{channel.name}" if channel.name else channel.id
        message_count = len(messages)
        summary = (
            f"Slack thread in {channel_label} with {message_count} message"
            f"{'s' if message_count != 1 else ''} from {len(participants)} participant"
            f"{'s' if len(participants) != 1 else ''}. Root: {title_lede}"
        )
        team_id = _team_id(connection)
        source_created_at = _slack_datetime(root.get("ts"))
        source_updated_at = _slack_datetime(messages[-1].get("ts"))
        return KnowledgeDraft(
            source=self.source_key,
            kind="slack_thread",
            source_ref=f"slack:{team_id}:{channel.id}:{thread_ts}",
            title=f"Slack {channel_label}: {title_lede}",
            summary=summary,
            entities=[channel.id, *([channel.name] if channel.name else []), *participants],
            raw_text=_thread_transcript(messages),
            extra={
                "org_id": str(connection.org_id),
                "actor_user_id": str(connection.owner_user_id),
                "connection_id": str(connection.id),
                "team_id": team_id,
                "channel_id": channel.id,
                "channel_name": channel.name,
                "thread_ts": thread_ts,
                "participants": participants,
                "message_count": message_count,
                "last_activity_ts": _clean(messages[-1].get("ts")),
            },
            source_created_at=source_created_at,
            source_updated_at=source_updated_at,
            distill=True,
        )


__all__ = ["SlackKnowledgeConnector"]
