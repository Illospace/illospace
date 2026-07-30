"""Persistence service for Slack-thread obligations that still need an answer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.open_ask import (
    ObligationKind,
    ObligationNotice,
    OpenAsk,
)
from brain.platform.db.models.org import User
from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.obligation_specs import (
    InboundSlackReply,
    ObligationSpec,
    obligation_spec_from_metadata,
)


OPEN_ASK_STATUS = "open"
ANSWERED_ASK_STATUS = "answered"
OPEN_ASK_STRAGGLER_AFTER = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class DeliveredSlackReply:
    """Confirmed Slack delivery facts used to settle matching obligations."""

    org_id: str
    channel_id: str
    thread_ts: str
    answering_run_id: int | None
    slack_message_ts: str
    answer_text: str
    is_answer: bool
    artifact_kind: str | None = None
    artifact_ref: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveredSlackReplyCounts:
    """Settlement counts grouped by the ledger's canonical obligation kind."""

    by_kind: dict[ObligationKind, int]

    @classmethod
    def empty(cls) -> DeliveredSlackReplyCounts:
        return cls(by_kind={kind: 0 for kind in ObligationKind})

    @property
    def answered_open_asks(self) -> int:
        return int(self.by_kind.get(ObligationKind.HUMAN_ASK, 0))

    @property
    def total(self) -> int:
        return sum(int(value) for value in self.by_kind.values())


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
    obligation_spec = obligation_spec_from_metadata(
        metadata.get("obligation_spec")
    )
    if not trigger or (
        obligation_spec is None
        and (metadata.get("slack_monitor") or target_ref.get("headless"))
    ):
        return None
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
    persistence_context = dict(context)
    notice_config = persistence_context.pop("notice", None)
    explicit_requester_name = persistence_context.pop(
        "requester_name",
        None,
    )
    opened_at = _aware_utc(now)
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
            OpenAsk.obligation_kind == ObligationKind.HUMAN_ASK,
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
            OpenAsk.obligation_kind == ObligationKind.HUMAN_ASK,
            OpenAsk.status == OPEN_ASK_STATUS,
        )
        .order_by(OpenAsk.id.asc())
    )
    if for_update:
        statement = statement.with_for_update()
    return list((await session.scalars(statement)).all())


def _run_context_maps(run: Any):
    target_ref = getattr(run, "target_ref", None)
    metadata = getattr(run, "metadata_", None)
    if not isinstance(metadata, dict):
        metadata = getattr(run, "metadata", None)
    for container in (target_ref, metadata):
        if isinstance(container, dict):
            yield container


def _run_origin_ref(
    run: Any,
    *,
    trigger: dict[str, Any],
    channel_id: str,
    thread_ts: str,
) -> str:
    for container in _run_context_maps(run):
        candidate = _clean(container.get("origin_ref"))
        if candidate:
            return candidate
    candidate = slack_origin_ref(trigger)
    if candidate:
        return candidate
    thread_id = _clean(getattr(run, "thread_id", None))
    if thread_id.startswith("slack:"):
        return thread_id
    return f"slack-run:{int(run.id)}:{channel_id}:{thread_ts}"


def _run_deferral_key(
    *,
    run: Any,
    channel_id: str,
    thread_ts: str,
) -> tuple[str, str, str, int] | None:
    org_id = _clean(getattr(run, "org_id", None))
    normalized_channel = _clean(channel_id)
    normalized_thread = _clean(thread_ts)
    if not org_id or not normalized_channel or not normalized_thread:
        return None
    return org_id, normalized_channel, normalized_thread, int(run.id)


async def _run_deferral_for_key(
    session: Any,
    *,
    org_id: str,
    channel_id: str,
    thread_ts: str,
    run_id: int,
    for_update: bool = False,
) -> OpenAsk | None:
    statement = select(OpenAsk).where(
        OpenAsk.obligation_kind == ObligationKind.RUN_DEFERRAL,
        OpenAsk.org_id == org_id,
        OpenAsk.channel_id == channel_id,
        OpenAsk.thread_ts == thread_ts,
        OpenAsk.origin_run_id == int(run_id),
    )
    if for_update:
        statement = statement.with_for_update()
    return (await session.scalars(statement)).first()


async def record_run_deferral(
    session: Any,
    *,
    run: Any,
    channel_id: str,
    thread_ts: str,
    trigger: dict[str, Any],
    deferral_text: str,
    notice_condition: str,
    post_thread_ts: str | None,
    now: datetime | None = None,
) -> tuple[OpenAsk, Any | None, bool]:
    """Persist one run-owned promise and its condition-specific outbox row."""

    key = _run_deferral_key(
        run=run,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )
    condition = _clean(notice_condition)
    if key is None:
        raise ValueError("run deferrals require an org, channel, and thread")
    if not condition:
        raise ValueError("run deferrals require a notice condition")
    org_id, normalized_channel, normalized_thread, run_id = key
    row = await _run_deferral_for_key(
        session,
        org_id=org_id,
        channel_id=normalized_channel,
        thread_ts=normalized_thread,
        run_id=run_id,
        for_update=True,
    )
    if row is None:
        permalink = slack_thread_permalink(
            {
                **trigger,
                "channel_id": normalized_channel,
                "thread_ts": normalized_thread,
            }
        )
        row = OpenAsk(
            obligation_kind=ObligationKind.RUN_DEFERRAL,
            org_id=org_id,
            channel_id=normalized_channel,
            channel_type=_clean(trigger.get("channel_type")) or None,
            team_id=_clean(trigger.get("team_id")) or None,
            thread_ts=normalized_thread,
            thread_permalink=permalink
            or f"https://app.slack.com/client/unknown/{normalized_channel}",
            requester_slack_id=None,
            requester_user_id=None,
            requester_name=None,
            bot_user_id=_clean(trigger.get("bot_user_id")) or None,
            ask_text=str(
                trigger.get("text")
                or getattr(run, "input_message", None)
                or deferral_text
            ),
            origin_ref=_run_origin_ref(
                run,
                trigger=trigger,
                channel_id=normalized_channel,
                thread_ts=normalized_thread,
            ),
            origin_run_id=run_id,
            status=OPEN_ASK_STATUS,
            opened_at=_aware_utc(now),
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = await _run_deferral_for_key(
                session,
                org_id=org_id,
                channel_id=normalized_channel,
                thread_ts=normalized_thread,
                run_id=run_id,
                for_update=True,
            )
            if row is None:
                raise
    # Answered obligations are terminal. A later failure condition on the same
    # run cannot resurrect the promise or emit a contradictory new notice.
    if row.status == ANSWERED_ASK_STATUS:
        return row, None, False

    from brain.systems.runs.obligation_notices import record_obligation_notice

    notice, created = await record_obligation_notice(
        session,
        obligation=row,
        condition=condition,
        notice_text=deferral_text,
        post_thread_ts=post_thread_ts,
    )
    return row, notice, created


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


async def _supersede_pending_notices(
    session: Any,
    obligation_ids: list[int],
) -> None:
    if not obligation_ids:
        return
    pending_notices = list(
        (
            await session.scalars(
                select(ObligationNotice)
                .where(
                    ObligationNotice.obligation_id.in_(obligation_ids),
                    ObligationNotice.state == "pending",
                )
                .with_for_update()
            )
        ).all()
    )
    for notice in pending_notices:
        notice.state = "superseded"
        notice.claimed_at = None
        notice.last_error = None
    if pending_notices:
        await session.flush()


async def record_delivered_slack_answer(
    session: Any,
    delivered: DeliveredSlackReply,
    *,
    now: datetime | None = None,
) -> DeliveredSlackReplyCounts:
    """Match and close every obligation answered by one confirmed Slack reply."""

    counts = DeliveredSlackReplyCounts.empty()
    message_ts = _clean(delivered.slack_message_ts)
    if not message_ts:
        raise ValueError("delivered Slack answers require a confirmed message timestamp")
    if not delivered.is_answer:
        return counts
    org_id = _clean(delivered.org_id)
    channel_id = _clean(delivered.channel_id)
    thread_ts = _clean(delivered.thread_ts)
    if not all((org_id, channel_id, thread_ts)):
        raise ValueError("delivered Slack answers require org, channel, and thread")

    rows = list(
        (
            await session.scalars(
                select(OpenAsk)
                .where(
                    OpenAsk.org_id == org_id,
                    OpenAsk.channel_id == channel_id,
                    OpenAsk.thread_ts == thread_ts,
                    OpenAsk.status == OPEN_ASK_STATUS,
                )
                .order_by(OpenAsk.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    by_kind = dict(counts.by_kind)
    for row in rows:
        mark_open_ask_answered(
            row,
            answer_text=delivered.answer_text,
            answered_by_run_id=delivered.answering_run_id,
            artifact_kind=delivered.artifact_kind,
            artifact_ref=delivered.artifact_ref,
            slack_response={"ts": message_ts},
            now=now,
        )
        try:
            kind = ObligationKind(str(row.obligation_kind))
        except ValueError:
            continue
        by_kind[kind] = int(by_kind.get(kind, 0)) + 1
    await _supersede_pending_notices(
        session,
        [int(row.id) for row in rows],
    )
    return DeliveredSlackReplyCounts(by_kind=by_kind)


async def record_inbound_slack_obligation_answer(
    session: Any,
    *,
    org_id: str,
    channel_id: str,
    thread_ts: str,
    slack_user_id: str,
    message_ts: str,
    answer_text: str,
    now: datetime | None = None,
) -> int:
    """Close typed obligations whose settlement policies accept this reply."""

    normalized = {
        "org_id": _clean(org_id),
        "channel_id": _clean(channel_id),
        "thread_ts": _clean(thread_ts),
        "slack_user_id": _clean(slack_user_id),
        "message_ts": _clean(message_ts),
    }
    if not all(normalized.values()):
        return 0
    results = list(
        (
            await session.execute(
                select(
                    OpenAsk,
                    ObligationNotice.condition,
                    AgentRunRow.metadata_,
                )
                .join(
                    ObligationNotice,
                    ObligationNotice.obligation_id == OpenAsk.id,
                )
                .join(
                    AgentRunRow,
                    AgentRunRow.id == OpenAsk.origin_run_id,
                )
                .where(
                    OpenAsk.org_id == normalized["org_id"],
                    OpenAsk.channel_id == normalized["channel_id"],
                    OpenAsk.thread_ts == normalized["thread_ts"],
                    OpenAsk.status == OPEN_ASK_STATUS,
                )
                .order_by(OpenAsk.id.asc())
                .with_for_update(of=[OpenAsk, ObligationNotice])
            )
        ).all()
    )
    reply = InboundSlackReply(
        slack_user_id=normalized["slack_user_id"],
        message_ts=normalized["message_ts"],
        text=str(answer_text),
    )
    rows_by_id: dict[int, OpenAsk] = {}
    for row, notice_condition, run_metadata in results:
        metadata = run_metadata if isinstance(run_metadata, dict) else {}
        spec = obligation_spec_from_metadata(
            metadata.get("obligation_spec")
        )
        if (
            spec is None
            or spec.condition != _clean(notice_condition)
            or not spec.settles(reply)
        ):
            continue
        rows_by_id[int(row.id)] = row
    for row in rows_by_id.values():
        mark_open_ask_answered(
            row,
            answer_text=str(answer_text),
            answered_by_run_id=None,
            slack_response={"ts": normalized["message_ts"]},
            now=now,
        )
    await _supersede_pending_notices(
        session,
        list(rows_by_id),
    )
    return len(rows_by_id)


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
                    OpenAsk.obligation_kind.asc(),
                    OpenAsk.opened_at.asc(),
                    OpenAsk.id.asc(),
                )
            )
        ).all()
    )
    return [
        {
            "id": row.id,
            "obligation_kind": row.obligation_kind,
            "owner_label": row.owner_label,
            "requester_name": (
                row.owner_label
                if row.obligation_kind == ObligationKind.HUMAN_ASK
                else None
            ),
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
    "DeliveredSlackReply",
    "DeliveredSlackReplyCounts",
    "OPEN_ASK_STATUS",
    "OPEN_ASK_STRAGGLER_AFTER",
    "annotate_request_with_open_ask",
    "delivered_message_ts",
    "list_open_ask_stragglers",
    "mark_open_ask_answered",
    "open_ask_context_for_request",
    "open_asks_for_origin_ref",
    "open_asks_for_origin_run",
    "record_delivered_slack_answer",
    "record_inbound_slack_obligation_answer",
    "record_open_ask",
    "record_run_deferral",
    "slack_origin_ref",
    "slack_thread_permalink",
]
