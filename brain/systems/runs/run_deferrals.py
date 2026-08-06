"""Creation, notice, and expiry lifecycle for run-owned deferrals."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from brain.contracts.statuses import (
    AGENT_RUN_DB_STATUS_VALUES,
    TERMINAL_OPEN_ASK_STATUS_VALUES,
    TERMINAL_RUN_STATUS_VALUES,
    OpenAskStatus,
    project_run_status_value,
)
from brain.kernel.common.time import assume_utc
from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.open_ask import ObligationKind, OpenAsk
from brain.systems.runs.obligation_notices import (
    record_obligation_notice,
    supersede_pending_obligation_notices,
)
from brain.systems.runs.open_asks import slack_origin_ref, slack_thread_permalink


# Three days is well below the observed 146-176h failures while allowing recovery.
RUN_DEFERRAL_EXPIRY_AFTER = timedelta(hours=72)
_TERMINAL_ORIGIN_RUN_STATUS_VALUES = tuple(
    status
    for status in AGENT_RUN_DB_STATUS_VALUES
    if project_run_status_value(status) in TERMINAL_RUN_STATUS_VALUES
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


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
            status=OpenAskStatus.OPEN.value,
            opened_at=assume_utc(now),
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
    # Settled obligations are terminal. A later failure condition on the same
    # run cannot resurrect the promise or emit a contradictory new notice.
    if row.status in TERMINAL_OPEN_ASK_STATUS_VALUES:
        return row, None, False

    notice, created = await record_obligation_notice(
        session,
        obligation=row,
        condition=condition,
        notice_text=deferral_text,
        post_thread_ts=post_thread_ts,
    )
    return row, notice, created


async def expire_stale_run_deferrals(
    session: Any,
    *,
    org_id: str,
    now: datetime | None = None,
    expiry_after: timedelta = RUN_DEFERRAL_EXPIRY_AFTER,
) -> int:
    """Expire old run promises only after their originating run is terminal."""

    current = assume_utc(now)
    cutoff = current - expiry_after
    results = list(
        (
            await session.execute(
                select(OpenAsk, AgentRunRow.status)
                .join(AgentRunRow, AgentRunRow.id == OpenAsk.origin_run_id)
                .where(
                    OpenAsk.org_id == str(org_id),
                    OpenAsk.obligation_kind == ObligationKind.RUN_DEFERRAL,
                    OpenAsk.status == OpenAskStatus.OPEN.value,
                    OpenAsk.opened_at < cutoff,
                    AgentRunRow.status.in_(_TERMINAL_ORIGIN_RUN_STATUS_VALUES),
                )
                .order_by(OpenAsk.id.asc())
                .with_for_update(of=OpenAsk)
            )
        ).all()
    )
    expiry_hours = int(expiry_after.total_seconds() // 3600)
    for row, run_status in results:
        row.status = OpenAskStatus.EXPIRED.value
        row.expired_at = current
        row.status_reason = (
            f"Origin run {int(row.origin_run_id)} is terminal ({str(run_status)}); "
            f"run deferral expired after {expiry_hours}h."
        )
    rows = [row for row, _run_status in results]
    await supersede_pending_obligation_notices(
        session,
        [int(row.id) for row in rows],
    )
    if rows:
        await session.flush()
    return len(rows)


__all__ = [
    "RUN_DEFERRAL_EXPIRY_AFTER",
    "expire_stale_run_deferrals",
    "record_run_deferral",
]
