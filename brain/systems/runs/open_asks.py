"""Persistence service for human Slack asks that still need an answer."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from brain.platform.db.models.open_ask import OpenAsk
from brain.platform.db.models.org import User
from brain.systems.runs.domain import AgentRunRequest


OPEN_ASK_STATUS = "open"
ANSWERED_ASK_STATUS = "answered"
OPEN_ASK_STRAGGLER_AFTER = timedelta(hours=1)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _request_maps(request: AgentRunRequest) -> tuple[dict[str, Any], dict[str, Any]]:
    target_ref = dict(request.target_ref or {})
    metadata = dict(request.metadata or {})
    return target_ref, metadata


def _slack_trigger_for_request(request: AgentRunRequest) -> dict[str, Any]:
    target_ref, metadata = _request_maps(request)
    for container in (metadata, target_ref):
        trigger = container.get("slack_trigger")
        if isinstance(trigger, dict):
            return dict(trigger)
    return {}


def slack_origin_ref(trigger: dict[str, Any]) -> str | None:
    team_id = _clean(trigger.get("team_id"))
    channel_id = _clean(trigger.get("channel_id"))
    thread_ts = _clean(trigger.get("thread_ts") or trigger.get("message_ts"))
    if not team_id or not channel_id or not thread_ts:
        return None
    return f"slack:{team_id}:{channel_id}:{thread_ts}"


def slack_thread_permalink(trigger: dict[str, Any]) -> str | None:
    permalink = _clean(trigger.get("permalink"))
    if permalink:
        return permalink
    team_id = _clean(trigger.get("team_id"))
    channel_id = _clean(trigger.get("channel_id"))
    thread_ts = _clean(trigger.get("thread_ts") or trigger.get("message_ts"))
    if not team_id or not channel_id or not thread_ts:
        return None
    return (
        f"https://app.slack.com/client/{team_id}/{channel_id}/thread/"
        f"{channel_id}-{thread_ts}"
    )


def open_ask_context_for_request(request: AgentRunRequest) -> dict[str, Any] | None:
    target_ref, metadata = _request_maps(request)
    trigger = _slack_trigger_for_request(request)
    if not trigger or metadata.get("slack_monitor") or target_ref.get("headless"):
        return None
    channel_id = _clean(trigger.get("channel_id"))
    thread_ts = _clean(trigger.get("thread_ts") or trigger.get("message_ts"))
    requester_slack_id = _clean(trigger.get("slack_user_id"))
    ask_text = str(trigger.get("text") or "")
    origin_ref = slack_origin_ref(trigger)
    permalink = slack_thread_permalink(trigger)
    if not all(
        (
            request.org_id,
            channel_id,
            thread_ts,
            requester_slack_id,
            ask_text.strip(),
            origin_ref,
            permalink,
        )
    ):
        return None
    return {
        "org_id": str(request.org_id),
        "channel_id": channel_id,
        "channel_type": _clean(trigger.get("channel_type")) or None,
        "team_id": _clean(trigger.get("team_id")) or None,
        "thread_ts": thread_ts,
        "thread_permalink": permalink,
        "requester_slack_id": requester_slack_id,
        "requester_user_id": _clean(request.user_id) or None,
        "bot_user_id": _clean(trigger.get("bot_user_id")) or None,
        "ask_text": ask_text,
        "origin_ref": origin_ref,
    }


def annotate_request_with_open_ask(
    request: AgentRunRequest,
) -> tuple[AgentRunRequest, dict[str, Any] | None]:
    context = open_ask_context_for_request(request)
    if context is None:
        return request, None
    target_ref, metadata = _request_maps(request)
    origin_ref = context["origin_ref"]
    target_ref["origin_ref"] = origin_ref
    metadata["origin_ref"] = origin_ref
    metadata["open_ask"] = {
        "origin_ref": origin_ref,
        "channel_id": context["channel_id"],
        "thread_ts": context["thread_ts"],
        "requester_slack_id": context["requester_slack_id"],
    }
    return (
        replace(request, target_ref=target_ref, metadata=metadata),
        context,
    )


async def _requester_name(
    session: Any,
    requester_user_id: str | None,
    requester_slack_id: str,
) -> str:
    if requester_user_id and hasattr(session, "get"):
        try:
            user = await session.get(User, requester_user_id)
        except Exception:
            user = None
        name = _clean(getattr(user, "name", None))
        if name:
            return name
    return f"<@{requester_slack_id}>"


async def _open_ask_for_key(
    session: Any,
    *,
    org_id: str,
    channel_id: str,
    thread_ts: str,
    requester_slack_id: str,
    for_update: bool = False,
) -> OpenAsk | None:
    statement = select(OpenAsk).where(
        OpenAsk.org_id == org_id,
        OpenAsk.channel_id == channel_id,
        OpenAsk.thread_ts == thread_ts,
        OpenAsk.requester_slack_id == requester_slack_id,
    )
    if for_update:
        statement = statement.with_for_update()
    return (await session.scalars(statement)).first()


def _reopen_ask(
    row: OpenAsk,
    *,
    context: dict[str, Any],
    run_id: int,
    requester_name: str,
    opened_at: datetime,
) -> OpenAsk:
    row.channel_type = context["channel_type"]
    row.team_id = context["team_id"]
    row.thread_permalink = context["thread_permalink"]
    row.requester_user_id = context["requester_user_id"]
    row.requester_name = requester_name
    row.bot_user_id = context["bot_user_id"]
    row.ask_text = context["ask_text"]
    row.origin_ref = context["origin_ref"]
    row.origin_run_id = int(run_id)
    row.status = OPEN_ASK_STATUS
    row.opened_at = opened_at
    row.answer_text = None
    row.answer_artifact_kind = None
    row.answer_artifact_ref = None
    row.answered_by_run_id = None
    row.answered_at = None
    row.delivered_message_ts = None
    return row


async def record_open_ask(
    session: Any,
    *,
    context: dict[str, Any] | None,
    run_id: int,
    now: datetime | None = None,
) -> OpenAsk | None:
    """Persist or reopen the obligation created by one admitted Slack ask."""

    if context is None:
        return None
    opened_at = _aware_utc(now)
    requester_name = await _requester_name(
        session,
        context["requester_user_id"],
        context["requester_slack_id"],
    )
    row = await _open_ask_for_key(
        session,
        org_id=context["org_id"],
        channel_id=context["channel_id"],
        thread_ts=context["thread_ts"],
        requester_slack_id=context["requester_slack_id"],
        for_update=True,
    )
    if row is not None:
        if int(row.origin_run_id or 0) == int(run_id):
            return row
        return _reopen_ask(
            row,
            context=context,
            run_id=run_id,
            requester_name=requester_name,
            opened_at=opened_at,
        )

    row = OpenAsk(
        **context,
        requester_name=requester_name,
        origin_run_id=int(run_id),
        status=OPEN_ASK_STATUS,
        opened_at=opened_at,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        row = await _open_ask_for_key(
            session,
            org_id=context["org_id"],
            channel_id=context["channel_id"],
            thread_ts=context["thread_ts"],
            requester_slack_id=context["requester_slack_id"],
            for_update=True,
        )
        if row is None:
            raise
        if int(row.origin_run_id or 0) != int(run_id):
            _reopen_ask(
                row,
                context=context,
                run_id=run_id,
                requester_name=requester_name,
                opened_at=opened_at,
            )
    return row


async def open_asks_for_origin_ref(
    session: Any,
    origin_ref: str,
    *,
    for_update: bool = False,
) -> list[OpenAsk]:
    normalized = _clean(origin_ref)
    if not normalized:
        return []
    statement = (
        select(OpenAsk)
        .where(
            OpenAsk.origin_ref == normalized,
            OpenAsk.status == OPEN_ASK_STATUS,
        )
        .order_by(OpenAsk.opened_at.asc(), OpenAsk.id.asc())
    )
    if for_update:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


async def open_asks_for_origin_run(
    session: Any,
    origin_run_id: int,
    *,
    for_update: bool = False,
) -> list[OpenAsk]:
    statement = (
        select(OpenAsk)
        .where(
            OpenAsk.origin_run_id == int(origin_run_id),
            OpenAsk.status == OPEN_ASK_STATUS,
        )
        .order_by(OpenAsk.id.asc())
    )
    if for_update:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


def delivered_message_ts(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None
    message = response.get("message")
    message = message if isinstance(message, dict) else {}
    return _clean(response.get("ts") or message.get("ts")) or None


def mark_open_ask_answered(
    row: OpenAsk,
    *,
    answer_text: str,
    answered_by_run_id: int | None,
    artifact_kind: str | None = None,
    artifact_ref: str | None = None,
    slack_response: Any = None,
    now: datetime | None = None,
) -> OpenAsk:
    """Close an ask only after the caller has confirmed Slack delivery."""

    message_ts = delivered_message_ts(slack_response)
    if message_ts is None:
        raise ValueError("open ask answers require a confirmed Slack delivery timestamp")
    row.status = ANSWERED_ASK_STATUS
    row.answer_text = str(answer_text)
    row.answer_artifact_kind = _clean(artifact_kind) or None
    row.answer_artifact_ref = _clean(artifact_ref) or None
    row.answered_by_run_id = int(answered_by_run_id) if answered_by_run_id else None
    row.answered_at = _aware_utc(now)
    row.delivered_message_ts = message_ts
    return row


async def mark_origin_run_answer_delivered(
    session: Any,
    *,
    origin_run_id: int,
    answer_text: str,
    slack_response: Any,
    now: datetime | None = None,
) -> list[OpenAsk]:
    rows = await open_asks_for_origin_run(
        session,
        origin_run_id,
        for_update=True,
    )
    for row in rows:
        mark_open_ask_answered(
            row,
            answer_text=answer_text,
            answered_by_run_id=origin_run_id,
            slack_response=slack_response,
            now=now,
        )
    return rows


def _age_label(age: timedelta) -> str:
    total_minutes = max(0, int(age.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


async def list_open_ask_stragglers(
    session: Any,
    *,
    org_id: str,
    now: datetime | None = None,
    older_than: timedelta = OPEN_ASK_STRAGGLER_AFTER,
) -> list[dict[str, Any]]:
    current = _aware_utc(now)
    cutoff = current - older_than
    rows = list(
        (
            await session.scalars(
                select(OpenAsk)
                .where(
                    OpenAsk.org_id == str(org_id),
                    OpenAsk.status == OPEN_ASK_STATUS,
                    OpenAsk.opened_at < cutoff,
                )
                .order_by(
                    OpenAsk.requester_name.asc(),
                    OpenAsk.opened_at.asc(),
                    OpenAsk.id.asc(),
                )
            )
        ).all()
    )
    return [
        {
            "id": row.id,
            "requester_name": row.requester_name or f"<@{row.requester_slack_id}>",
            "requester_slack_id": row.requester_slack_id,
            "ask_text": row.ask_text,
            "origin_ref": row.origin_ref,
            "age": _age_label(current - _aware_utc(row.opened_at)),
            "age_seconds": max(
                0,
                int((current - _aware_utc(row.opened_at)).total_seconds()),
            ),
            "thread_permalink": row.thread_permalink,
        }
        for row in rows
    ]


__all__ = [
    "ANSWERED_ASK_STATUS",
    "OPEN_ASK_STATUS",
    "OPEN_ASK_STRAGGLER_AFTER",
    "annotate_request_with_open_ask",
    "delivered_message_ts",
    "list_open_ask_stragglers",
    "mark_open_ask_answered",
    "mark_origin_run_answer_delivered",
    "open_ask_context_for_request",
    "open_asks_for_origin_ref",
    "open_asks_for_origin_run",
    "record_open_ask",
    "slack_origin_ref",
    "slack_thread_permalink",
]
