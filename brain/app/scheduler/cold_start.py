"""One scheduler-owned cold-start gap reconciliation pass."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.platform.db.models.scheduler import (
    SchedulerColdStartReconciliation,
    SchedulerLivenessCheckpoint,
)
from brain.systems.cycles.service import async_advance_cycle_schedule_past_gap
from brain.systems.production_gate_notifier import (
    SOFTWARE_CHANNEL,
    resolve_slack_channel,
)
from brain.systems.slack.connector import backfill_monitored_slack_history
from brain.systems.tracker_maintenance import run_cold_start_tracker_maintenance


logger = logging.getLogger(__name__)

DEFAULT_COLD_START_GAP_THRESHOLD = timedelta(minutes=60)
ACTIVE_CLAIM_WINDOW = timedelta(minutes=30)
CLAIM_HEARTBEAT_INTERVAL = timedelta(minutes=5)
LIVENESS_CHECKPOINT_KEY = "scheduler_connector"
NOTICE_TEXT_LIMIT = 3900
NOTICE_HISTORY_MAX_PAGES = 20


class ClaimSuperseded(RuntimeError):
    """The receipt claim generation no longer authorizes work."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc) if value is not None else None


def _window_identity(gap_start: datetime, gap_end: datetime) -> str:
    return f"{gap_start.isoformat()}->{gap_end.isoformat()}"


def _notice_marker(gap_start: datetime, gap_end: datetime) -> str:
    digest = hashlib.sha256(
        _window_identity(gap_start, gap_end).encode("utf-8")
    ).hexdigest()[:16]
    return f"illo-cold-start:{digest}"


def _notice_client_msg_id(gap_start: datetime, gap_end: datetime) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"illospace:cold-start:{_window_identity(gap_start, gap_end)}",
        )
    )


async def scheduler_liveness_checkpoint(session) -> datetime | None:
    checkpoint = await session.get(
        SchedulerLivenessCheckpoint,
        LIVENESS_CHECKPOINT_KEY,
    )
    return _utc(checkpoint.last_heartbeat_at) if checkpoint is not None else None


async def record_scheduler_liveness_checkpoint(
    session,
    *,
    now: datetime | None = None,
) -> datetime:
    """Advance the scheduler heartbeat monotonically."""

    heartbeat_at = _utc(now or _utc_now())
    if heartbeat_at is None:
        raise ValueError("now is required")
    checkpoint = await session.get(
        SchedulerLivenessCheckpoint,
        LIVENESS_CHECKPOINT_KEY,
    )
    if checkpoint is None:
        checkpoint = SchedulerLivenessCheckpoint(
            checkpoint_key=LIVENESS_CHECKPOINT_KEY,
            last_heartbeat_at=heartbeat_at,
        )
        try:
            async with session.begin_nested():
                session.add(checkpoint)
                await session.flush()
        except IntegrityError:
            checkpoint = await session.get(
                SchedulerLivenessCheckpoint,
                LIVENESS_CHECKPOINT_KEY,
                populate_existing=True,
            )
            if checkpoint is None:
                raise
    current = _utc(checkpoint.last_heartbeat_at)
    if current is None or current < heartbeat_at:
        checkpoint.last_heartbeat_at = heartbeat_at
    await session.commit()
    return _utc(checkpoint.last_heartbeat_at) or heartbeat_at


async def _receipt_for_window(
    session,
    *,
    gap_start: datetime,
    gap_end: datetime,
) -> SchedulerColdStartReconciliation | None:
    return await session.scalar(
        select(SchedulerColdStartReconciliation)
        .where(
            SchedulerColdStartReconciliation.gap_started_at == gap_start,
            SchedulerColdStartReconciliation.reconciled_through == gap_end,
        )
        .limit(1)
    )


async def _unfinished_receipt(
    session,
) -> SchedulerColdStartReconciliation | None:
    return await session.scalar(
        select(SchedulerColdStartReconciliation)
        .where(
            SchedulerColdStartReconciliation.status.in_({"running", "degraded"})
        )
        .order_by(
            SchedulerColdStartReconciliation.gap_started_at.asc(),
            SchedulerColdStartReconciliation.id.asc(),
        )
        .limit(1)
    )


async def _refresh_receipt(
    session,
    receipt_id: int,
) -> SchedulerColdStartReconciliation:
    receipt = await session.get(
        SchedulerColdStartReconciliation,
        receipt_id,
        populate_existing=True,
    )
    if receipt is None:
        raise RuntimeError("cold-start reconciliation receipt disappeared")
    return receipt


async def _claim_receipt(
    session,
    *,
    gap_start: datetime,
    gap_end: datetime,
    claimed_at: datetime,
) -> tuple[SchedulerColdStartReconciliation, int | None]:
    receipt = await _receipt_for_window(
        session,
        gap_start=gap_start,
        gap_end=gap_end,
    )
    if receipt is None:
        receipt = SchedulerColdStartReconciliation(
            gap_started_at=gap_start,
            reconciled_through=gap_end,
            status="running",
            lane_results={},
            notice_state="pending",
            notice_marker=_notice_marker(gap_start, gap_end),
            notice_client_msg_id=_notice_client_msg_id(gap_start, gap_end),
            claimed_at=claimed_at,
            claim_generation=1,
        )
        try:
            async with session.begin_nested():
                session.add(receipt)
                await session.flush()
        except IntegrityError:
            receipt = await _receipt_for_window(
                session,
                gap_start=gap_start,
                gap_end=gap_end,
            )
            if receipt is None:
                raise
            return receipt, None
        await session.commit()
        return receipt, 1

    observed_generation = int(receipt.claim_generation)
    claim = await session.execute(
        update(SchedulerColdStartReconciliation)
        .where(
            SchedulerColdStartReconciliation.id == receipt.id,
            SchedulerColdStartReconciliation.claim_generation == observed_generation,
            or_(
                SchedulerColdStartReconciliation.status != "running",
                SchedulerColdStartReconciliation.claimed_at
                <= claimed_at - ACTIVE_CLAIM_WINDOW,
            ),
        )
        .values(
            claimed_at=claimed_at,
            status="running",
            claim_generation=observed_generation + 1,
            completed_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    acquired = claim.rowcount == 1
    await session.commit()
    if acquired:
        receipt = await _refresh_receipt(session, receipt.id)
        return receipt, observed_generation + 1
    return await _refresh_receipt(session, receipt.id), None


async def _fenced_receipt_update(
    session,
    *,
    receipt_id: int,
    claim_generation: int,
    values: Mapping[str, Any],
) -> SchedulerColdStartReconciliation:
    result = await session.execute(
        update(SchedulerColdStartReconciliation)
        .where(
            SchedulerColdStartReconciliation.id == receipt_id,
            SchedulerColdStartReconciliation.status == "running",
            SchedulerColdStartReconciliation.claim_generation == claim_generation,
        )
        .values(**dict(values))
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await session.rollback()
        raise ClaimSuperseded(
            f"cold-start receipt {receipt_id} claim {claim_generation} was superseded"
        )
    await session.commit()
    return await _refresh_receipt(session, receipt_id)


async def _heartbeat_claim(
    session,
    *,
    receipt_id: int,
    claim_generation: int,
    now: datetime | None = None,
) -> bool:
    heartbeat_at = _utc(now or _utc_now())
    if heartbeat_at is None:
        raise ValueError("now is required")
    result = await session.execute(
        update(SchedulerColdStartReconciliation)
        .where(
            SchedulerColdStartReconciliation.id == receipt_id,
            SchedulerColdStartReconciliation.status == "running",
            SchedulerColdStartReconciliation.claim_generation == claim_generation,
        )
        .values(claimed_at=heartbeat_at)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


class _ClaimHeartbeat:
    def __init__(
        self,
        session,
        *,
        receipt_id: int,
        claim_generation: int,
    ) -> None:
        self._session_factory = (
            async_sessionmaker(
                bind=session.bind,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            if session.bind is not None
            else None
        )
        self._receipt_id = receipt_id
        self._claim_generation = claim_generation
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _ClaimHeartbeat:
        if self._session_factory is not None:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(CLAIM_HEARTBEAT_INTERVAL.total_seconds())
            if self._session_factory is None:
                return
            try:
                async with self._session_factory() as heartbeat_session:
                    alive = await _heartbeat_claim(
                        heartbeat_session,
                        receipt_id=self._receipt_id,
                        claim_generation=self._claim_generation,
                    )
            except Exception:  # noqa: BLE001 - retry transient heartbeat failures
                logger.exception(
                    "cold-start receipt %s claim heartbeat failed safely",
                    self._receipt_id,
                )
                continue
            if not alive:
                return


def _has_errors(value: Any) -> bool:
    if isinstance(value, Mapping):
        errors = value.get("errors")
        if isinstance(errors, list) and errors:
            return True
        return any(_has_errors(item) for key, item in value.items() if key != "errors")
    if isinstance(value, list):
        return any(_has_errors(item) for item in value)
    return False


async def _run_lane(
    session,
    receipt: SchedulerColdStartReconciliation,
    *,
    claim_generation: int,
    name: str,
    run,
) -> dict[str, Any]:
    receipt = await _refresh_receipt(session, receipt.id)
    existing = dict(receipt.lane_results or {}).get(name)
    if isinstance(existing, Mapping) and existing.get("status") == "succeeded":
        return dict(existing)
    try:
        async with session.begin_nested():
            result = await run()
        lane = {
            "status": "failed" if _has_errors(result) else "succeeded",
            "result": result,
        }
    except Exception as exc:  # noqa: BLE001 - every startup lane is fail-open
        logger.exception("cold-start reconciliation lane %s failed safely", name)
        lane = {
            "status": "failed",
            "error": str(exc),
        }
    await _fenced_receipt_update(
        session,
        receipt_id=receipt.id,
        claim_generation=claim_generation,
        values={
            "claimed_at": _utc_now(),
            "lane_results": {
                **dict(receipt.lane_results or {}),
                name: lane,
            },
        },
    )
    return lane


def _duration_label(duration: timedelta) -> str:
    total_minutes = max(0, int(duration.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _lane_result(
    lane_results: Mapping[str, Any],
    name: str,
) -> tuple[str, Mapping[str, Any]]:
    lane = lane_results.get(name)
    lane = lane if isinstance(lane, Mapping) else {}
    result = lane.get("result")
    return str(lane.get("status") or "failed"), (
        result if isinstance(result, Mapping) else {}
    )


def _slot_label(slot: Mapping[str, Any]) -> str:
    scheduled_for = datetime.fromisoformat(str(slot["scheduled_for"]))
    timezone_name = str(slot.get("timezone") or "UTC")
    try:
        local = scheduled_for.astimezone(ZoneInfo(timezone_name))
    except Exception:
        local = scheduled_for.astimezone(timezone.utc)
        timezone_name = "UTC"
    return (
        f"{str(slot.get('cycle_name') or 'Cycle')} — "
        f"{local.strftime('%Y-%m-%d %H:%M')} {local.tzname() or timezone_name}"
    )


def _sum_nested(value: Any, key: str) -> int:
    if isinstance(value, Mapping):
        own = value.get(key)
        total = int(own or 0) if isinstance(own, (int, float)) else 0
        return total + sum(
            _sum_nested(item, key)
            for nested_key, item in value.items()
            if nested_key != key
        )
    if isinstance(value, list):
        return sum(_sum_nested(item, key) for item in value)
    return 0


def render_cold_start_notice(
    *,
    gap_start: datetime,
    now: datetime,
    lane_results: Mapping[str, Any],
    marker: str,
) -> str:
    """Render one bounded Slack message for the complete catch-up pass."""

    slack_status, slack = _lane_result(lane_results, "slack")
    cycles_status, cycles = _lane_result(lane_results, "cycles")
    tracker_status, tracker = _lane_result(lane_results, "tracker")
    slots = cycles.get("missed_slots")
    slots = [slot for slot in slots or [] if isinstance(slot, Mapping)]

    lines = [
        f"Cold-start catch-up after *{_duration_label(now - gap_start)}* offline.",
        (
            f"Window: {gap_start.isoformat()} → {now.isoformat()}."
        ),
        "*Missed scheduled slots (not replayed):*",
    ]
    if cycles_status == "failed":
        lines.append("• Cycle slot reconciliation failed; normal startup is continuing.")
    elif not slots:
        lines.append("• None.")
    else:
        remaining = len(slots)
        for slot in slots:
            candidate = f"• {_slot_label(slot)}"
            reserved = len(marker) + 500
            if len("\n".join([*lines, candidate])) + reserved > NOTICE_TEXT_LIMIT:
                break
            lines.append(candidate)
            remaining -= 1
        if remaining:
            lines.append(f"• …and {remaining} additional missed slot(s).")

    if slack_status == "succeeded":
        lines.append(
            "*Slack backfill:* "
            f"{int(slack.get('ingested') or 0)} ingested, "
            f"{int(slack.get('deduplicated') or 0)} already present, "
            f"{int(slack.get('acked') or 0)} acknowledged."
        )
    else:
        lines.append(
            "*Slack backfill: FAILED/DEGRADED.* "
            f"{len(slack.get('errors') or [])} recorded error(s); startup continues."
        )

    if tracker_status == "succeeded":
        lines.append(
            "*Tracker reconciliation:* "
            f"{_sum_nested(tracker, 'updated')} record update(s), "
            f"{_sum_nested(tracker, 'flagged')} production-gate finding(s)."
        )
    else:
        lines.append(
            "*Tracker reconciliation: FAILED/DEGRADED.* "
            "The normal cadence will continue."
        )
    lines.append(f"Reconciliation receipt: `{marker}`")
    text = "\n".join(lines)
    if len(text) > NOTICE_TEXT_LIMIT:
        text = (
            text[: NOTICE_TEXT_LIMIT - len(marker) - 50].rstrip()
            + f"\n…\nReconciliation receipt: `{marker}`"
        )
    return text


async def _notice_present_in_history(
    client: Any,
    *,
    channel_id: str,
    marker: str,
    gap_start: datetime,
    now: datetime,
) -> tuple[bool, str | None]:
    cursor: str | None = None
    for _page in range(NOTICE_HISTORY_MAX_PAGES):
        payload = await client.conversation_history(
            channel=channel_id,
            limit=200,
            oldest=f"{gap_start.timestamp():.6f}",
            latest=f"{now.timestamp():.6f}",
            cursor=cursor,
        )
        for message in payload.get("messages") or []:
            if isinstance(message, Mapping) and marker in str(message.get("text") or ""):
                return True, str(message.get("ts") or "") or None
        metadata = payload.get("response_metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        cursor = str(metadata.get("next_cursor") or "").strip() or None
        if cursor is None:
            return False, None
    raise RuntimeError("catch-up notice history exceeded the disambiguation page limit")


async def _deliver_notice(
    session,
    receipt: SchedulerColdStartReconciliation,
    *,
    claim_generation: int,
    now: datetime,
    client: Any | None,
) -> dict[str, Any]:
    receipt = await _refresh_receipt(session, receipt.id)
    if receipt.notice_state == "posted":
        return {"status": "posted", "already_posted": True}
    try:
        if client is None:
            from brain.systems.slack.client import slack_web_client_from_runtime

            client = await slack_web_client_from_runtime(
                requested_by="scheduler_cold_start_reconciliation",
                reason="Post the one scheduler cold-start catch-up notice.",
            )
        channel_id = await resolve_slack_channel(client, SOFTWARE_CHANNEL)
    except Exception as exc:
        await _fenced_receipt_update(
            session,
            receipt_id=receipt.id,
            claim_generation=claim_generation,
            values={
                "claimed_at": _utc_now(),
                "notice_state": "failed",
                "last_error": str(exc),
            },
        )
        raise
    if receipt.notice_state == "posting":
        found, message_ts = await _notice_present_in_history(
            client,
            channel_id=channel_id,
            marker=receipt.notice_marker,
            gap_start=_utc(receipt.gap_started_at) or now,
            now=now,
        )
        if found:
            await _fenced_receipt_update(
                session,
                receipt_id=receipt.id,
                claim_generation=claim_generation,
                values={
                    "claimed_at": _utc_now(),
                    "notice_state": "posted",
                    "notice_posted_at": now,
                    "notice_message_ts": message_ts,
                    "last_error": None,
                },
            )
            return {"status": "posted", "already_posted": True}

    receipt = await _fenced_receipt_update(
        session,
        receipt_id=receipt.id,
        claim_generation=claim_generation,
        values={
            "claimed_at": _utc_now(),
            "notice_state": "posting",
            "last_error": None,
        },
    )
    text = render_cold_start_notice(
        gap_start=_utc(receipt.gap_started_at) or now,
        now=_utc(receipt.reconciled_through) or now,
        lane_results=dict(receipt.lane_results or {}),
        marker=receipt.notice_marker,
    )
    try:
        response = await client.post_message(
            channel=channel_id,
            text=text,
            client_msg_id=receipt.notice_client_msg_id,
        )
    except Exception as exc:
        # A retry uses the same Slack client_msg_id. History is useful evidence,
        # but Slack's idempotency key closes the accepted-send/history-lag gap.
        await _fenced_receipt_update(
            session,
            receipt_id=receipt.id,
            claim_generation=claim_generation,
            values={
                "claimed_at": _utc_now(),
                "last_error": str(exc),
            },
        )
        raise
    await _fenced_receipt_update(
        session,
        receipt_id=receipt.id,
        claim_generation=claim_generation,
        values={
            "claimed_at": _utc_now(),
            "notice_state": "posted",
            "notice_posted_at": now,
            "notice_message_ts": str(response.get("ts") or "") or None,
            "last_error": None,
        },
    )
    return {
        "status": "posted",
        "already_posted": False,
        "channel": channel_id,
    }


def _receipt_result(
    receipt: SchedulerColdStartReconciliation,
    *,
    gap_start: datetime,
    now: datetime,
    idempotent_replay: bool,
) -> dict[str, Any]:
    return {
        "triggered": True,
        "idempotent_replay": idempotent_replay,
        "receipt_id": receipt.id,
        "status": receipt.status,
        "claim_generation": receipt.claim_generation,
        "gap_start": gap_start.isoformat(),
        "gap_end": (_utc(receipt.reconciled_through) or now).isoformat(),
        "gap_seconds": int(
            ((_utc(receipt.reconciled_through) or now) - gap_start).total_seconds()
        ),
        "lane_results": dict(receipt.lane_results or {}),
        "notice": {
            "state": receipt.notice_state,
            "posted_at": (
                _utc(receipt.notice_posted_at).isoformat()
                if receipt.notice_posted_at
                else None
            ),
            "message_ts": receipt.notice_message_ts,
            "last_error": receipt.last_error,
        },
    }


async def _execute_claimed_receipt(
    session,
    *,
    receipt: SchedulerColdStartReconciliation,
    claim_generation: int,
    now: datetime,
    notice_client: Any | None = None,
) -> SchedulerColdStartReconciliation:
    gap_start = _utc(receipt.gap_started_at) or now
    gap_end = _utc(receipt.reconciled_through) or now
    async with _ClaimHeartbeat(
        session,
        receipt_id=receipt.id,
        claim_generation=claim_generation,
    ):
        await _run_lane(
            session,
            receipt,
            claim_generation=claim_generation,
            name="slack",
            run=lambda: backfill_monitored_slack_history(
                session,
                gap_start=gap_start,
                now=gap_end,
            ),
        )
        await _run_lane(
            session,
            receipt,
            claim_generation=claim_generation,
            name="cycles",
            run=lambda: async_advance_cycle_schedule_past_gap(
                session,
                gap_start=gap_start,
                now=gap_end,
            ),
        )
        await _run_lane(
            session,
            receipt,
            claim_generation=claim_generation,
            name="tracker",
            run=lambda: run_cold_start_tracker_maintenance(session),
        )

        try:
            await _deliver_notice(
                session,
                receipt,
                claim_generation=claim_generation,
                now=now,
                client=notice_client,
            )
        except ClaimSuperseded:
            raise
        except Exception as exc:  # noqa: BLE001 - notice failure never blocks startup
            logger.exception("cold-start catch-up notice failed safely")
            await _fenced_receipt_update(
                session,
                receipt_id=receipt.id,
                claim_generation=claim_generation,
                values={
                    "claimed_at": _utc_now(),
                    "last_error": str(exc),
                },
            )

        receipt = await _refresh_receipt(session, receipt.id)
        lane_failed = any(
            isinstance(lane, Mapping) and lane.get("status") == "failed"
            for lane in dict(receipt.lane_results or {}).values()
        )
        receipt = await _fenced_receipt_update(
            session=session,
            receipt_id=receipt.id,
            claim_generation=claim_generation,
            values={
                "completed_at": now,
                "status": (
                    "completed"
                    if not lane_failed and receipt.notice_state == "posted"
                    else "degraded"
                ),
            },
        )
    return receipt


async def reconcile_cold_start_gap(
    session,
    *,
    now: datetime | None = None,
    threshold: timedelta = DEFAULT_COLD_START_GAP_THRESHOLD,
    notice_client: Any | None = None,
) -> dict[str, Any]:
    """Reconcile one liveness-checkpoint window before cadence resumes."""

    now = _utc(now or _utc_now())
    if now is None:
        raise ValueError("now is required")
    if threshold.total_seconds() < 0:
        raise ValueError("threshold must not be negative")

    unfinished = await _unfinished_receipt(session)
    if unfinished is not None:
        gap_start = _utc(unfinished.gap_started_at) or now
        gap_end = _utc(unfinished.reconciled_through) or now
        receipt, claim_generation = await _claim_receipt(
            session,
            gap_start=gap_start,
            gap_end=gap_end,
            claimed_at=now,
        )
        if claim_generation is None:
            return _receipt_result(
                receipt,
                gap_start=gap_start,
                now=now,
                idempotent_replay=True,
            )
        try:
            receipt = await _execute_claimed_receipt(
                session,
                receipt=receipt,
                claim_generation=claim_generation,
                now=now,
                notice_client=notice_client,
            )
        except ClaimSuperseded:
            receipt = await _refresh_receipt(session, receipt.id)
            return _receipt_result(
                receipt,
                gap_start=gap_start,
                now=now,
                idempotent_replay=True,
            )
        await record_scheduler_liveness_checkpoint(session, now=now)
        return _receipt_result(
            receipt,
            gap_start=gap_start,
            now=now,
            idempotent_replay=False,
        )

    gap_start = await scheduler_liveness_checkpoint(session)
    if gap_start is None:
        await record_scheduler_liveness_checkpoint(session, now=now)
        return {
            "triggered": False,
            "reason": "no_liveness_checkpoint",
            "checkpoint_at": now.isoformat(),
            "threshold_seconds": int(threshold.total_seconds()),
        }

    gap = now - gap_start
    if gap <= threshold:
        await record_scheduler_liveness_checkpoint(session, now=now)
        return {
            "triggered": False,
            "reason": "below_threshold",
            "gap_start": gap_start.isoformat(),
            "gap_end": now.isoformat(),
            "gap_seconds": int(gap.total_seconds()),
            "threshold_seconds": int(threshold.total_seconds()),
        }

    existing = await _receipt_for_window(
        session,
        gap_start=gap_start,
        gap_end=now,
    )
    if existing is not None and existing.status == "completed":
        await record_scheduler_liveness_checkpoint(session, now=now)
        return _receipt_result(
            existing,
            gap_start=gap_start,
            now=now,
            idempotent_replay=True,
        )

    receipt, claim_generation = await _claim_receipt(
        session,
        gap_start=gap_start,
        gap_end=now,
        claimed_at=now,
    )
    if claim_generation is None:
        return _receipt_result(
            receipt,
            gap_start=gap_start,
            now=now,
            idempotent_replay=True,
        )
    try:
        receipt = await _execute_claimed_receipt(
            session,
            receipt=receipt,
            claim_generation=claim_generation,
            now=now,
            notice_client=notice_client,
        )
    except ClaimSuperseded:
        receipt = await _refresh_receipt(session, receipt.id)
        return _receipt_result(
            receipt,
            gap_start=gap_start,
            now=now,
            idempotent_replay=True,
        )
    await record_scheduler_liveness_checkpoint(session, now=now)
    return _receipt_result(
        receipt,
        gap_start=gap_start,
        now=now,
        idempotent_replay=False,
    )


__all__ = [
    "DEFAULT_COLD_START_GAP_THRESHOLD",
    "record_scheduler_liveness_checkpoint",
    "reconcile_cold_start_gap",
    "render_cold_start_notice",
    "scheduler_liveness_checkpoint",
]
