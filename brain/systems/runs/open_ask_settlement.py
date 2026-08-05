"""Settlement transitions for delivered and inbound open-ask replies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from brain.contracts.statuses import (
    ACTIVE_OPEN_ASK_STATUS_VALUES,
    OpenAskStatus,
)
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.open_ask import (
    ObligationKind,
    ObligationNotice,
    OpenAsk,
)
from brain.systems.runs.obligation_specs import (
    InboundSlackReply,
    obligation_spec_from_metadata,
)


@dataclass(frozen=True, slots=True)
class DeliveredSlackAnswer:
    """Confirmed Slack answer delivery used to settle matching obligations."""

    org_id: str
    channel_id: str
    thread_ts: str
    answering_run_id: int | None
    slack_message_ts: str
    answer_text: str
    artifact_kind: str | None = None
    artifact_ref: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveredSlackRoute:
    """Confirmed Slack routing delivery for one originating human ask."""

    org_id: str
    channel_id: str
    thread_ts: str
    answering_run_id: int
    slack_message_ts: str
    routed_to_name: str
    routed_to_slack_id: str


@dataclass(frozen=True, slots=True)
class DeliveredSlackAnswerCounts:
    """Answered obligation counts grouped by kind."""

    by_kind: dict[ObligationKind, int]

    @classmethod
    def empty(cls) -> DeliveredSlackAnswerCounts:
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


def _clear_open_ask_resolution(row: OpenAsk) -> None:
    """Clear terminal transition fields before creation reopens an obligation."""

    row.answer_text = None
    row.answer_artifact_kind = None
    row.answer_artifact_ref = None
    row.answered_by_run_id = None
    row.answered_at = None
    row.delivered_message_ts = None
    row.routed_to_name = None
    row.routed_to_slack_id = None
    row.routed_at = None
    row.expired_at = None
    row.status_reason = None


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
            OpenAsk.status == OpenAskStatus.OPEN.value,
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
            OpenAsk.status == OpenAskStatus.OPEN.value,
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
    row.status = OpenAskStatus.ANSWERED.value
    row.answer_text = str(answer_text)
    row.answer_artifact_kind = _clean(artifact_kind) or None
    row.answer_artifact_ref = _clean(artifact_ref) or None
    row.answered_by_run_id = int(answered_by_run_id) if answered_by_run_id else None
    row.answered_at = _aware_utc(now)
    row.delivered_message_ts = message_ts
    row.expired_at = None
    row.status_reason = None
    return row


def mark_open_ask_routed(
    row: OpenAsk,
    *,
    routed_to_name: str,
    routed_to_slack_id: str,
    slack_response: Any = None,
    now: datetime | None = None,
) -> OpenAsk:
    """Transfer an ask after a confirmed Illo reply names its human answerer."""

    owner_name = _clean(routed_to_name)
    owner_slack_id = _clean(routed_to_slack_id)
    message_ts = delivered_message_ts(slack_response)
    if not owner_name or not owner_slack_id:
        raise ValueError("routed open asks require a named Slack answerer")
    if message_ts is None:
        raise ValueError("routed open asks require a confirmed Slack delivery timestamp")
    row.status = OpenAskStatus.ROUTED.value
    row.routed_to_name = owner_name
    row.routed_to_slack_id = owner_slack_id
    row.routed_at = _aware_utc(now)
    row.delivered_message_ts = message_ts
    row.expired_at = None
    row.status_reason = None
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


async def _delivered_slack_rows(
    session: Any,
    *,
    org_id: str,
    channel_id: str,
    thread_ts: str,
    conditions: tuple[Any, ...],
) -> list[OpenAsk]:
    return list(
        (
            await session.scalars(
                select(OpenAsk)
                .where(
                    OpenAsk.org_id == org_id,
                    OpenAsk.channel_id == channel_id,
                    OpenAsk.thread_ts == thread_ts,
                    *conditions,
                )
                .order_by(OpenAsk.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def record_delivered_slack_answer(
    session: Any,
    delivered: DeliveredSlackAnswer,
    *,
    now: datetime | None = None,
) -> DeliveredSlackAnswerCounts:
    """Settle matching obligations after a confirmed Slack answer delivery."""

    message_ts = _clean(delivered.slack_message_ts)
    if not message_ts:
        raise ValueError("delivered Slack answers require a confirmed message timestamp")
    org_id = _clean(delivered.org_id)
    channel_id = _clean(delivered.channel_id)
    thread_ts = _clean(delivered.thread_ts)
    if not all((org_id, channel_id, thread_ts)):
        raise ValueError("delivered Slack answers require org, channel, and thread")

    rows = await _delivered_slack_rows(
        session,
        org_id=org_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        conditions=(OpenAsk.status.in_(ACTIVE_OPEN_ASK_STATUS_VALUES),),
    )
    by_kind = dict(DeliveredSlackAnswerCounts.empty().by_kind)
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
    return DeliveredSlackAnswerCounts(by_kind=by_kind)


async def record_delivered_slack_route(
    session: Any,
    delivered: DeliveredSlackRoute,
    *,
    now: datetime | None = None,
) -> int:
    """Route one originating human ask after a confirmed Slack delivery."""

    message_ts = _clean(delivered.slack_message_ts)
    if not message_ts:
        raise ValueError("delivered Slack routes require a confirmed message timestamp")
    routed_to_name = _clean(delivered.routed_to_name)
    routed_to_slack_id = _clean(delivered.routed_to_slack_id)
    if not routed_to_name or not routed_to_slack_id:
        raise ValueError("delivered Slack routing requires a complete named answerer")
    org_id = _clean(delivered.org_id)
    channel_id = _clean(delivered.channel_id)
    thread_ts = _clean(delivered.thread_ts)
    if not all((org_id, channel_id, thread_ts)):
        raise ValueError("delivered Slack routing requires org, channel, and thread")
    if delivered.answering_run_id is None:
        raise ValueError("delivered Slack routing requires its originating run")

    rows = await _delivered_slack_rows(
        session,
        org_id=org_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        conditions=(
            OpenAsk.obligation_kind == ObligationKind.HUMAN_ASK,
            OpenAsk.origin_run_id == int(delivered.answering_run_id),
            OpenAsk.status == OpenAskStatus.OPEN.value,
        ),
    )
    for row in rows:
        mark_open_ask_routed(
            row,
            routed_to_name=routed_to_name,
            routed_to_slack_id=routed_to_slack_id,
            slack_response={"ts": message_ts},
            now=now,
        )
    if rows:
        await session.flush()
    return len(rows)


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
                    OpenAsk.status.in_(ACTIVE_OPEN_ASK_STATUS_VALUES),
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


__all__ = [
    "DeliveredSlackAnswer",
    "DeliveredSlackAnswerCounts",
    "DeliveredSlackRoute",
    "delivered_message_ts",
    "mark_open_ask_answered",
    "mark_open_ask_routed",
    "open_asks_for_origin_ref",
    "open_asks_for_origin_run",
    "record_delivered_slack_answer",
    "record_delivered_slack_route",
    "record_inbound_slack_obligation_answer",
]
