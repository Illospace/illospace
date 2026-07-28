"""One scheduler-owned cold-start gap reconciliation pass."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.cycle import CycleRun
from brain.platform.db.models.scheduler import SchedulerColdStartReconciliation
from brain.systems.cycles.service import async_catch_up_missed_cycle_slots
from brain.systems.production_gate_notifier import (
    SOFTWARE_CHANNEL,
    resolve_slack_channel,
)
from brain.systems.slack.connector import backfill_monitored_slack_history
from brain.systems.tracker_maintenance import run_cold_start_tracker_maintenance


logger = logging.getLogger(__name__)

DEFAULT_COLD_START_GAP_THRESHOLD = timedelta(minutes=60)
ACTIVE_CLAIM_WINDOW = timedelta(minutes=30)
NOTICE_TEXT_LIMIT = 3900
NOTICE_HISTORY_MAX_PAGES = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc) if value is not None else None


async def _last_successful_completion(session) -> datetime | None:
    cycle_completion = await session.scalar(
        select(func.max(CycleRun.completed_at)).where(
            CycleRun.status == "completed",
            CycleRun.completed_at.is_not(None),
        )
    )
    agent_completion = await session.scalar(
        select(func.max(AgentRunRow.completed_at)).where(
            AgentRunRow.status == "completed",
            AgentRunRow.completed_at.is_not(None),
        )
    )
    completions = [
        completion
        for completion in (_utc(cycle_completion), _utc(agent_completion))
        if completion is not None
    ]
    return max(completions, default=None)


def _notice_marker(gap_start: datetime) -> str:
    digest = hashlib.sha256(gap_start.isoformat().encode("utf-8")).hexdigest()[:16]
    return f"illo-cold-start:{digest}"


async def _receipt_for_gap(
    session,
    *,
    gap_start: datetime,
) -> SchedulerColdStartReconciliation | None:
    return await session.scalar(
        select(SchedulerColdStartReconciliation)
        .where(SchedulerColdStartReconciliation.gap_started_at == gap_start)
        .limit(1)
    )


async def _claim_receipt(
    session,
    *,
    gap_start: datetime,
    now: datetime,
) -> tuple[SchedulerColdStartReconciliation, bool]:
    receipt = await _receipt_for_gap(session, gap_start=gap_start)
    if receipt is None:
        receipt = SchedulerColdStartReconciliation(
            gap_started_at=gap_start,
            reconciled_through=now,
            status="running",
            lane_results={},
            notice_state="pending",
            notice_marker=_notice_marker(gap_start),
            claimed_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(receipt)
                await session.flush()
        except IntegrityError:
            receipt = await _receipt_for_gap(session, gap_start=gap_start)
            if receipt is None:
                raise
            return receipt, False
        await session.commit()
        return receipt, True

    observed_claimed_at = receipt.claimed_at
    observed_status = receipt.status
    claim = await session.execute(
        update(SchedulerColdStartReconciliation)
        .where(
            SchedulerColdStartReconciliation.id == receipt.id,
            SchedulerColdStartReconciliation.claimed_at == observed_claimed_at,
            SchedulerColdStartReconciliation.status == observed_status,
        )
        .values(
            claimed_at=now,
            status="running",
        )
        .execution_options(synchronize_session=False)
    )
    acquired = claim.rowcount == 1
    await session.commit()
    if acquired:
        receipt.claimed_at = now
        receipt.status = "running"
        return receipt, True
    current = await _receipt_for_gap(session, gap_start=gap_start)
    if current is None:
        raise RuntimeError("cold-start reconciliation receipt disappeared")
    return current, False


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
    name: str,
    run,
) -> dict[str, Any]:
    existing = dict(receipt.lane_results or {}).get(name)
    if isinstance(existing, Mapping):
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
    receipt.lane_results = {
        **dict(receipt.lane_results or {}),
        name: lane,
    }
    await session.commit()
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
    now: datetime,
    client: Any | None,
) -> dict[str, Any]:
    if receipt.notice_state == "posted":
        return {"status": "posted", "already_posted": True}
    if client is None:
        from brain.systems.slack.client import slack_web_client_from_runtime

        client = await slack_web_client_from_runtime(
            requested_by="scheduler_cold_start_reconciliation",
            reason="Post the one scheduler cold-start catch-up notice.",
        )
    channel_id = await resolve_slack_channel(client, SOFTWARE_CHANNEL)
    if receipt.notice_state == "posting":
        found, message_ts = await _notice_present_in_history(
            client,
            channel_id=channel_id,
            marker=receipt.notice_marker,
            gap_start=_utc(receipt.reconciled_through) or now,
            now=now,
        )
        if found:
            receipt.notice_state = "posted"
            receipt.notice_posted_at = now
            receipt.notice_message_ts = message_ts
            receipt.last_error = None
            await session.commit()
            return {"status": "posted", "already_posted": True}

    receipt.notice_state = "posting"
    receipt.last_error = None
    await session.commit()
    text = render_cold_start_notice(
        gap_start=_utc(receipt.gap_started_at) or now,
        now=_utc(receipt.reconciled_through) or now,
        lane_results=dict(receipt.lane_results or {}),
        marker=receipt.notice_marker,
    )
    try:
        response = await client.post_message(channel=channel_id, text=text)
    except Exception as exc:
        # An interrupted HTTP exchange is ambiguous. Keep ``posting`` so the
        # next startup must prove absence from history before any re-send.
        receipt.last_error = str(exc)
        await session.commit()
        raise
    receipt.notice_state = "posted"
    receipt.notice_posted_at = now
    receipt.notice_message_ts = str(response.get("ts") or "") or None
    receipt.last_error = None
    await session.commit()
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
        },
    }


async def reconcile_cold_start_gap(
    session,
    *,
    now: datetime | None = None,
    threshold: timedelta = DEFAULT_COLD_START_GAP_THRESHOLD,
    notice_client: Any | None = None,
) -> dict[str, Any]:
    """Reconcile one detected startup gap before scheduler cadence resumes."""

    now = _utc(now or _utc_now())
    if now is None:
        raise ValueError("now is required")
    gap_start = await _last_successful_completion(session)
    if gap_start is None:
        return {
            "triggered": False,
            "reason": "no_successful_run",
            "threshold_seconds": int(threshold.total_seconds()),
        }
    gap = now - gap_start
    if gap <= threshold:
        return {
            "triggered": False,
            "reason": "below_threshold",
            "gap_start": gap_start.isoformat(),
            "gap_end": now.isoformat(),
            "gap_seconds": int(gap.total_seconds()),
            "threshold_seconds": int(threshold.total_seconds()),
        }

    existing = await _receipt_for_gap(session, gap_start=gap_start)
    if (
        existing is not None
        and existing.notice_state == "posted"
        and existing.status in {"completed", "degraded"}
    ):
        return _receipt_result(
            existing,
            gap_start=gap_start,
            now=now,
            idempotent_replay=True,
        )
    if (
        existing is not None
        and existing.status == "running"
        and now - (_utc(existing.claimed_at) or now) < ACTIVE_CLAIM_WINDOW
    ):
        return _receipt_result(
            existing,
            gap_start=gap_start,
            now=now,
            idempotent_replay=True,
        )

    receipt, acquired = await _claim_receipt(
        session,
        gap_start=gap_start,
        now=now,
    )
    if not acquired:
        return _receipt_result(
            receipt,
            gap_start=gap_start,
            now=now,
            idempotent_replay=True,
        )
    await _run_lane(
        session,
        receipt,
        name="slack",
        run=lambda: backfill_monitored_slack_history(
            session,
            gap_start=gap_start,
            now=now,
        ),
    )
    await _run_lane(
        session,
        receipt,
        name="cycles",
        run=lambda: async_catch_up_missed_cycle_slots(
            session,
            gap_start=gap_start,
            now=now,
        ),
    )
    await _run_lane(
        session,
        receipt,
        name="tracker",
        run=lambda: run_cold_start_tracker_maintenance(session),
    )

    try:
        await _deliver_notice(
            session,
            receipt,
            now=now,
            client=notice_client,
        )
    except Exception as exc:  # noqa: BLE001 - notice failure never blocks startup
        logger.exception("cold-start catch-up notice failed safely")
        receipt.last_error = str(exc)
        await session.commit()

    lane_failed = any(
        isinstance(lane, Mapping) and lane.get("status") == "failed"
        for lane in dict(receipt.lane_results or {}).values()
    )
    receipt.status = (
        "completed"
        if not lane_failed and receipt.notice_state == "posted"
        else "degraded"
    )
    receipt.completed_at = now
    await session.commit()
    return _receipt_result(
        receipt,
        gap_start=gap_start,
        now=now,
        idempotent_replay=False,
    )


__all__ = [
    "DEFAULT_COLD_START_GAP_THRESHOLD",
    "reconcile_cold_start_gap",
    "render_cold_start_notice",
]
