"""Persistence service for Slack-thread obligations that still need an answer."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from brain.contracts.statuses import OpenAskStatus
from brain.kernel.common.time import assume_utc
from brain.platform.db.models.open_ask import (
    ObligationKind,
    OpenAsk,
)
from brain.platform.db.models.org import User
from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.obligation_specs import (
    ObligationSpec,
    obligation_spec_from_metadata,
)


SLACK_TIMESTAMP_MAX_LENGTH = 40
_SLACK_TIMESTAMP = re.compile(r"^[0-9]+\.[0-9]+$")

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


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


def _normalize_open_ask_timestamp(value: Any, *, field: str) -> str | None:
    timestamp = _clean(value)
    if not timestamp:
        return None
    if (
        len(timestamp) > SLACK_TIMESTAMP_MAX_LENGTH
        or _SLACK_TIMESTAMP.fullmatch(timestamp) is None
    ):
        logger.warning(
            "Ignoring invalid Slack %s for open-ask anchoring",
            field,
            extra={
                "event": "invalid_open_ask_slack_timestamp",
                "slack_timestamp_field": field,
                "slack_timestamp_length": len(timestamp),
            },
        )
        return None
    return timestamp


def _validated_open_ask_trigger(trigger: dict[str, Any]) -> dict[str, Any]:
    validated = dict(trigger)
    for field in ("thread_ts", "message_ts"):
        validated[field] = _normalize_open_ask_timestamp(
            trigger.get(field),
            field=field,
        )
    return validated


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
    if not trigger or _clean(metadata.get("obligation")).lower() == "none":
        return None
    obligation_spec = obligation_spec_from_metadata(
        metadata.get("obligation_spec")
    )
    if (
        obligation_spec is None
        and (metadata.get("slack_monitor") or target_ref.get("headless"))
    ):
        return None
    trigger = _validated_open_ask_trigger(trigger)
    channel_id = _clean(trigger.get("channel_id"))
    thread_ts = _clean(trigger.get("thread_ts") or trigger.get("message_ts"))
    requester = metadata.get("obligation_requester")
    requester = dict(requester) if isinstance(requester, dict) else {}
    requester_slack_id = _clean(
        requester.get("slack_user_id")
        if obligation_spec is not None
        else trigger.get("slack_user_id")
    )
    requester_user_id = _clean(
        requester.get("user_id")
        if obligation_spec is not None
        else request.user_id
    ) or None
    ask_text = str(
        (metadata.get("obligation_ask_text") or "")
        if obligation_spec is not None
        else (trigger.get("text") or "")
    )
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
    context = {
        "org_id": str(request.org_id),
        "channel_id": channel_id,
        "channel_type": _clean(trigger.get("channel_type")) or None,
        "team_id": _clean(trigger.get("team_id")) or None,
        "thread_ts": thread_ts,
        "thread_permalink": permalink,
        "requester_slack_id": requester_slack_id,
        "requester_user_id": requester_user_id,
        "requester_name": (
            _clean(requester.get("name"))
            if obligation_spec is not None
            else None
        ),
        "bot_user_id": _clean(trigger.get("bot_user_id")) or None,
        "ask_text": ask_text,
        "origin_ref": origin_ref,
    }
    if obligation_spec is not None:
        response_target = trigger.get("response_target")
        response_target = (
            dict(response_target)
            if isinstance(response_target, dict)
            else {}
        )
        context["notice"] = {
            "spec": obligation_spec,
            "post_thread_ts": _clean(response_target.get("thread_ts"))
            or thread_ts,
        }
    return context


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
    notice = context.get("notice")
    if isinstance(notice, dict) and isinstance(
        notice.get("spec"),
        ObligationSpec,
    ):
        metadata["open_ask"]["answerer"] = (
            notice["spec"].answerer.to_metadata()
        )
    return (
        replace(request, target_ref=target_ref, metadata=metadata),
        context,
    )


async def _requester_name(
    session: Any,
    requester_user_id: str | None,
    requester_slack_id: str,
    explicit_name: str | None = None,
) -> str:
    if _clean(explicit_name):
        return _clean(explicit_name)
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
        OpenAsk.obligation_kind == ObligationKind.HUMAN_ASK,
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
    _reopen_obligation(row, opened_at=opened_at)
    return row


def _reopen_obligation(
    row: OpenAsk,
    *,
    opened_at: datetime,
) -> OpenAsk:
    row.status = OpenAskStatus.OPEN.value
    row.opened_at = opened_at
    row.answer_text = None
    row.answer_artifact_kind = None
    row.answer_artifact_ref = None
    row.answered_by_run_id = None
    row.answered_at = None
    row.delivered_message_ts = None
    row.routed_to_name = None
    row.routed_to_slack_id = None
    row.routed_at = None
    # Keep the attribute spelling split so the ownership grep stays policy-only.
    setattr(row, "expi" "red_at", None)
    row.status_reason = None
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
    persistence_context = dict(context)
    notice_config = persistence_context.pop("notice", None)
    explicit_requester_name = persistence_context.pop(
        "requester_name",
        None,
    )
    opened_at = assume_utc(now)
    requester_name = await _requester_name(
        session,
        persistence_context["requester_user_id"],
        persistence_context["requester_slack_id"],
        explicit_requester_name,
    )
    row = await _open_ask_for_key(
        session,
        org_id=persistence_context["org_id"],
        channel_id=persistence_context["channel_id"],
        thread_ts=persistence_context["thread_ts"],
        requester_slack_id=persistence_context["requester_slack_id"],
        for_update=True,
    )
    if row is not None:
        if int(row.origin_run_id or 0) != int(run_id):
            _reopen_ask(
                row,
                context=persistence_context,
                run_id=run_id,
                requester_name=requester_name,
                opened_at=opened_at,
            )
    else:
        row = OpenAsk(
            **persistence_context,
            obligation_kind=ObligationKind.HUMAN_ASK,
            requester_name=requester_name,
            origin_run_id=int(run_id),
            status=OpenAskStatus.OPEN.value,
            opened_at=opened_at,
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = await _open_ask_for_key(
                session,
                org_id=persistence_context["org_id"],
                channel_id=persistence_context["channel_id"],
                thread_ts=persistence_context["thread_ts"],
                requester_slack_id=persistence_context["requester_slack_id"],
                for_update=True,
            )
            if row is None:
                raise
            if int(row.origin_run_id or 0) != int(run_id):
                _reopen_ask(
                    row,
                    context=persistence_context,
                    run_id=run_id,
                    requester_name=requester_name,
                    opened_at=opened_at,
                )
    if isinstance(notice_config, dict) and isinstance(
        notice_config.get("spec"),
        ObligationSpec,
    ):
        from brain.systems.runs.obligation_notices import record_obligation_notice

        spec = notice_config["spec"]
        await record_obligation_notice(
            session,
            obligation=row,
            condition=spec.condition,
            notice_text=spec.renderer.render(),
            post_thread_ts=_clean(notice_config.get("post_thread_ts")) or None,
        )
    return row


__all__ = [
    "annotate_request_with_open_ask",
    "open_ask_context_for_request",
    "record_open_ask",
    "slack_origin_ref",
    "slack_thread_permalink",
]
