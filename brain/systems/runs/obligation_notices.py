"""Post-commit Slack delivery for obligation notices.

The obligation and its notice outbox row are written in the caller's
transaction. Delivery begins only after that transaction is committed and
visible from a fresh session. A committed ``posting`` claim precedes every
Slack request; stale claims are ambiguous and are disambiguated against the
Slack destination before any retry.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from brain.platform.db.models.open_ask import ObligationNotice, OpenAsk


logger = logging.getLogger(__name__)

NOTICE_SWEEP_LIMIT = 10
STALE_NOTICE_POSTING_GRACE = timedelta(minutes=10)
_DESTINATION_READ_LIMIT = 200
_DESTINATION_MAX_PAGES = 10
_VISIBILITY_POLL_ATTEMPTS = 5
_VISIBILITY_POLL_DELAY_SECONDS = 1.5
_INFO_QUEUE_KEY = "illo_obligation_notice_delivery_queue"
_INFO_ARMED_KEY = "illo_obligation_notice_delivery_listeners_armed"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def notice_idempotency_key(obligation_id: int, condition: str) -> str:
    return f"obligation-notice:{int(obligation_id)}:{str(condition)}"


async def _notice_for_key(
    session: Any,
    *,
    obligation_id: int,
    condition: str,
    for_update: bool = False,
) -> ObligationNotice | None:
    statement = (
        select(ObligationNotice)
        .where(
            ObligationNotice.obligation_id == int(obligation_id),
            ObligationNotice.condition == str(condition),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    return (await session.scalars(statement)).first()


async def record_obligation_notice(
    session: Any,
    *,
    obligation: OpenAsk,
    condition: str,
    notice_text: str,
    post_thread_ts: str | None,
) -> tuple[ObligationNotice, bool]:
    """Record one condition atomically with its answer obligation.

    The unique outbox key owns suppression. An existing pending row may take
    fresher committed delivery text/target; posting and delivered attempts are
    immutable because a send may already be in flight or visible.
    """

    normalized_condition = _clean(condition)
    if not normalized_condition:
        raise ValueError("obligation notices require a condition")
    if obligation.id is None:
        await session.flush()

    row = await _notice_for_key(
        session,
        obligation_id=int(obligation.id),
        condition=normalized_condition,
        for_update=True,
    )
    if row is not None:
        if row.state == "pending":
            row.notice_text = str(notice_text)
            row.channel_id = str(obligation.channel_id)
            row.thread_ts = str(obligation.thread_ts)
            row.post_thread_ts = _clean(post_thread_ts) or None
            row.bot_user_id = _clean(obligation.bot_user_id) or None
        return row, False

    row = ObligationNotice(
        obligation_id=int(obligation.id),
        org_id=str(obligation.org_id),
        condition=normalized_condition,
        idempotency_key=notice_idempotency_key(
            int(obligation.id),
            normalized_condition,
        ),
        state="pending",
        channel_id=str(obligation.channel_id),
        thread_ts=str(obligation.thread_ts),
        post_thread_ts=_clean(post_thread_ts) or None,
        bot_user_id=_clean(obligation.bot_user_id) or None,
        notice_text=str(notice_text),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        row = await _notice_for_key(
            session,
            obligation_id=int(obligation.id),
            condition=normalized_condition,
            for_update=True,
        )
        if row is None:
            raise
        return row, False
    return row, True


def _default_session_factory() -> Callable[[], Any] | None:
    try:
        from brain.platform.db import SessionFactory

        return SessionFactory
    except Exception as exc:  # noqa: BLE001
        logger.debug("obligation notice delivery has no session factory (%s)", exc)
        return None


async def _default_poster(
    *,
    channel: str,
    text: str,
    thread_ts: str | None,
    idempotency_key: str,
) -> dict[str, Any]:
    from brain.systems.slack.client import slack_web_client_from_runtime

    client = await slack_web_client_from_runtime(
        requested_by="obligation_notice_delivery",
        reason="Deliver a durable answer-obligation notice to its Slack destination.",
    )
    return await client.post_message(
        channel=channel,
        text=text,
        thread_ts=thread_ts,
        metadata={
            "event_type": "illo_obligation_notice",
            "event_payload": {"idempotency_key": idempotency_key},
        },
    )


async def _default_destination_reader(
    *,
    channel: str,
    thread_ts: str | None,
) -> dict[str, Any]:
    """Read the complete destination, or report that absence is undecidable."""

    from brain.systems.slack.client import slack_web_client_from_runtime

    client = await slack_web_client_from_runtime(
        requested_by="obligation_notice_delivery",
        reason="Disambiguate a crashed obligation-notice Slack delivery.",
    )
    if not thread_ts:
        payload = await client.conversation_history(
            channel=channel,
            limit=_DESTINATION_READ_LIMIT,
        )
        metadata = payload.get("response_metadata") or {}
        complete = not payload.get("has_more") and not metadata.get("next_cursor")
        return {
            "messages": [dict(message) for message in payload.get("messages") or []],
            "complete": complete,
        }

    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    complete = False
    for _page in range(_DESTINATION_MAX_PAGES):
        payload = await client.conversation_replies(
            channel=channel,
            thread_ts=thread_ts,
            limit=_DESTINATION_READ_LIMIT,
            cursor=cursor,
        )
        messages.extend(dict(message) for message in payload.get("messages") or [])
        cursor = _clean((payload.get("response_metadata") or {}).get("next_cursor"))
        if not cursor:
            complete = True
            break
    return {"messages": messages, "complete": complete}


def _snapshot(row: ObligationNotice) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "obligation_id": int(row.obligation_id),
        "org_id": str(row.org_id),
        "condition": str(row.condition),
        "idempotency_key": str(row.idempotency_key),
        "state": str(row.state),
        "channel_id": str(row.channel_id),
        "thread_ts": str(row.thread_ts),
        "post_thread_ts": _clean(row.post_thread_ts) or None,
        "bot_user_id": _clean(row.bot_user_id) or None,
        "notice_text": str(row.notice_text),
        "attempts": int(row.attempts or 0),
        "claimed_at": row.claimed_at,
    }


def _claimable_clause(cutoff: datetime):
    return or_(
        ObligationNotice.state == "pending",
        and_(
            ObligationNotice.state == "posting",
            ObligationNotice.claimed_at.is_not(None),
            ObligationNotice.claimed_at <= cutoff,
        ),
    )


async def _cas(
    factory: Callable[[], Any],
    notice_id: int,
    *,
    where: list[Any],
    values: dict[str, Any],
    skip_locked: bool = False,
) -> bool:
    async with factory() as session:
        conditions = [ObligationNotice.id == int(notice_id), *where]
        if skip_locked:
            locked_id = (
                select(ObligationNotice.id)
                .where(*conditions)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            statement = update(ObligationNotice).where(
                ObligationNotice.id.in_(select(locked_id))
            )
        else:
            statement = update(ObligationNotice).where(*conditions)
        result = await session.execute(
            statement.values(**values).execution_options(synchronize_session=False)
        )
        await session.commit()
        return int(result.rowcount or 0) == 1


async def _verified_reread(
    factory: Callable[[], Any],
    notice_id: int,
    *,
    claim_stamp: datetime,
) -> dict[str, Any] | None:
    async with factory() as session:
        result = (
            await session.execute(
                select(ObligationNotice, OpenAsk.status)
                .join(OpenAsk, OpenAsk.id == ObligationNotice.obligation_id)
                .where(ObligationNotice.id == int(notice_id))
                .execution_options(populate_existing=True)
            )
        ).first()
    if result is None:
        return None
    row, obligation_status = result
    if str(row.state) != "posting":
        return None
    if _utc(row.claimed_at) != _utc(claim_stamp):
        return None
    return {**_snapshot(row), "obligation_status": str(obligation_status)}


def _found_in_destination(
    messages: list[dict[str, Any]],
    idempotency_key: str,
    *,
    bot_user_id: str | None,
) -> str | None:
    for message in messages:
        if bot_user_id and _clean(message.get("user")) != bot_user_id:
            continue
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        payload = metadata.get("event_payload")
        payload = payload if isinstance(payload, dict) else {}
        if _clean(payload.get("idempotency_key")) == idempotency_key:
            return _clean(message.get("ts")) or "found"
    return None


async def _disambiguate(
    item: dict[str, Any],
    *,
    destination_reader: Callable[..., Awaitable[dict[str, Any]]],
) -> tuple[str, str | None]:
    try:
        read = await destination_reader(
            channel=item["channel_id"],
            thread_ts=item["post_thread_ts"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "obligation notice %s disambiguation failed (%s)",
            item["id"],
            exc,
        )
        return "unknown", None
    found_ts = _found_in_destination(
        list(read.get("messages") or []),
        item["idempotency_key"],
        bot_user_id=item.get("bot_user_id"),
    )
    if found_ts:
        return "delivered", found_ts
    if not read.get("complete", False):
        return "unknown", None
    return "absent", None


async def _deliver_one(
    item: dict[str, Any],
    *,
    factory: Callable[[], Any],
    poster: Callable[..., Awaitable[dict[str, Any]]],
    destination_reader: Callable[..., Awaitable[dict[str, Any]]],
    now: datetime,
    summary: dict[str, int],
) -> None:
    notice_id = int(item["id"])
    must_disambiguate = item["state"] == "posting"
    if must_disambiguate:
        claimed = await _cas(
            factory,
            notice_id,
            where=[
                ObligationNotice.state == "posting",
                ObligationNotice.claimed_at == item["claimed_at"],
            ],
            values={
                "claimed_at": now,
                "attempts": int(item["attempts"]) + 1,
            },
            skip_locked=True,
        )
    else:
        claimed = await _cas(
            factory,
            notice_id,
            where=[ObligationNotice.state == "pending"],
            values={
                "state": "posting",
                "claimed_at": now,
                "attempts": int(item["attempts"]) + 1,
            },
            skip_locked=True,
        )
    if not claimed:
        summary["lost_claim"] += 1
        return

    fence = [
        ObligationNotice.state == "posting",
        ObligationNotice.claimed_at == now,
    ]
    current = await _verified_reread(factory, notice_id, claim_stamp=now)
    if current is None:
        summary["lost_claim"] += 1
        return
    item = current
    if not must_disambiguate and item["obligation_status"] != "open":
        if await _cas(
            factory,
            notice_id,
            where=fence,
            values={
                "state": "superseded",
                "claimed_at": None,
                "last_error": None,
            },
        ):
            summary["superseded"] += 1
        return

    if must_disambiguate:
        outcome, found_ts = await _disambiguate(
            item,
            destination_reader=destination_reader,
        )
        if outcome == "unknown":
            summary["undecided"] += 1
            return
        if outcome == "delivered":
            if await _cas(
                factory,
                notice_id,
                where=fence,
                values={
                    "state": "delivered",
                    "delivered_at": now,
                    "delivered_message_ts": found_ts if found_ts != "found" else None,
                    "last_error": None,
                },
            ):
                summary["already_delivered"] += 1
            return
        if item["obligation_status"] != "open":
            if await _cas(
                factory,
                notice_id,
                where=fence,
                values={
                    "state": "superseded",
                    "claimed_at": None,
                    "last_error": None,
                },
            ):
                summary["superseded"] += 1
            return

    final_check = await _verified_reread(
        factory,
        notice_id,
        claim_stamp=now,
    )
    if final_check is None:
        summary["lost_claim"] += 1
        return
    if final_check["obligation_status"] != "open":
        if await _cas(
            factory,
            notice_id,
            where=fence,
            values={
                "state": "superseded",
                "claimed_at": None,
                "last_error": None,
            },
        ):
            summary["superseded"] += 1
        return
    # A destination read can outlive a lease; this final fence and
    # obligation-state check happens immediately before Slack.
    item = final_check

    from brain.systems.slack.client import (
        SlackApiError,
        SlackDeliveryError,
    )

    try:
        response = await poster(
            channel=item["channel_id"],
            text=item["notice_text"],
            thread_ts=item["post_thread_ts"],
            idempotency_key=item["idempotency_key"],
        )
    except SlackDeliveryError as exc:
        # Slack may have accepted one or more chunks before delivery
        # verification failed. The next sweep must read before retrying.
        await _cas(
            factory,
            notice_id,
            where=fence,
            values={"last_error": str(exc)},
        )
        summary["undecided"] += 1
        return
    except SlackApiError as exc:
        # Slack returned a definite rejection: no message was accepted.
        if await _cas(
            factory,
            notice_id,
            where=fence,
            values={
                "state": "pending",
                "claimed_at": None,
                "last_error": str(exc),
            },
        ):
            summary["requeued"] += 1
        return
    except Exception as exc:  # noqa: BLE001
        await _cas(
            factory,
            notice_id,
            where=fence,
            values={"last_error": str(exc)},
        )
        summary["undecided"] += 1
        return

    message_ts = _clean(
        response.get("ts")
        or (
            response.get("message", {}).get("ts")
            if isinstance(response.get("message"), dict)
            else None
        )
    )
    if not message_ts:
        summary["undecided"] += 1
        return
    marked = await _cas(
        factory,
        notice_id,
        where=fence,
        values={
            "state": "delivered",
            "delivered_at": now,
            "delivered_message_ts": message_ts,
            "last_error": None,
        },
    )
    if not marked:
        logger.warning(
            "obligation notice %s reached Slack but lost its delivery fence",
            notice_id,
        )
    summary["delivered"] += 1


async def deliver_pending_obligation_notices(
    *,
    org_id: str | None = None,
    notice_ids: list[int] | None = None,
    session_factory: Callable[[], Any] | None = None,
    poster: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    destination_reader: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    now: datetime | None = None,
    limit: int = NOTICE_SWEEP_LIMIT,
) -> dict[str, int]:
    """Deliver committed notices. Failures are contained for a later sweep."""

    summary = {
        "selected": 0,
        "delivered": 0,
        "already_delivered": 0,
        "requeued": 0,
        "undecided": 0,
        "lost_claim": 0,
        "superseded": 0,
    }
    try:
        factory = session_factory or _default_session_factory()
        if factory is None:
            return summary
        moment = now or datetime.now(timezone.utc)
        statement = (
            select(ObligationNotice)
            .where(
                _claimable_clause(moment - STALE_NOTICE_POSTING_GRACE)
            )
            .order_by(ObligationNotice.created_at.asc(), ObligationNotice.id.asc())
            .limit(max(1, int(limit)))
            .execution_options(populate_existing=True)
        )
        if org_id:
            statement = statement.where(ObligationNotice.org_id == str(org_id))
        if notice_ids:
            statement = statement.where(
                ObligationNotice.id.in_([int(value) for value in notice_ids])
            )
        async with factory() as session:
            candidates = [
                _snapshot(row)
                for row in (await session.execute(statement)).scalars().all()
            ]
        summary["selected"] = len(candidates)
        for item in candidates:
            try:
                await _deliver_one(
                    item,
                    factory=factory,
                    poster=poster or _default_poster,
                    destination_reader=destination_reader
                    or _default_destination_reader,
                    now=moment,
                    summary=summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "obligation notice %s failed safely: %s",
                    item.get("id"),
                    exc,
                )
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.warning("obligation notice delivery pass failed safely: %s", exc)
        return summary


_POST_COMMIT_TASKS: set[asyncio.Task] = set()


def _reap_delivery_task(task: asyncio.Task) -> None:
    _POST_COMMIT_TASKS.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("post-commit obligation notice task died: %s", exc)


async def _deliver_when_visible(org_id: str, notice_ids: list[int]) -> None:
    for attempt in range(_VISIBILITY_POLL_ATTEMPTS):
        summary = await deliver_pending_obligation_notices(
            org_id=org_id,
            notice_ids=notice_ids,
        )
        if summary["selected"]:
            return
        if attempt < _VISIBILITY_POLL_ATTEMPTS - 1:
            await asyncio.sleep(_VISIBILITY_POLL_DELAY_SECONDS)


def _spawn_delivery_task(org_id: str, notice_ids: list[int]) -> None:
    try:
        task = asyncio.ensure_future(_deliver_when_visible(org_id, notice_ids))
        _POST_COMMIT_TASKS.add(task)
        task.add_done_callback(_reap_delivery_task)
    except Exception:  # noqa: BLE001
        logger.warning(
            "post-commit obligation notice task failed to start",
            exc_info=True,
        )


def schedule_post_commit_notice_delivery(
    session: Any,
    *,
    org_id: str,
    notice_ids: list[int],
) -> bool:
    """Queue committed-visibility delivery; the periodic sweep is the fallback."""

    try:
        sync_session = getattr(session, "sync_session", None)
        if sync_session is None:
            return False
        loop = asyncio.get_running_loop()
        queue: list[tuple[str, int]] = sync_session.info.setdefault(
            _INFO_QUEUE_KEY,
            [],
        )
        queue.extend((str(org_id), int(notice_id)) for notice_id in notice_ids)
        if sync_session.info.get(_INFO_ARMED_KEY):
            return True

        def _drain_into_tasks(target_session: Any) -> None:
            drained = list(target_session.info.get(_INFO_QUEUE_KEY) or [])
            target_session.info[_INFO_QUEUE_KEY] = []
            by_org: dict[str, list[int]] = {}
            for queued_org, notice_id in drained:
                bucket = by_org.setdefault(queued_org, [])
                if notice_id not in bucket:
                    bucket.append(notice_id)
            for queued_org, ids in by_org.items():
                try:
                    loop.call_soon_threadsafe(
                        _spawn_delivery_task,
                        queued_org,
                        ids,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "post-commit obligation notice dispatch failed",
                        exc_info=True,
                    )

        def _on_rollback(target_session: Any, previous: Any) -> None:
            if getattr(previous, "nested", False):
                return
            target_session.info[_INFO_QUEUE_KEY] = []

        from sqlalchemy import event

        event.listen(sync_session, "after_commit", _drain_into_tasks)
        event.listen(sync_session, "after_soft_rollback", _on_rollback)
        sync_session.info[_INFO_ARMED_KEY] = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "post-commit obligation notice dispatch not armed (%s); "
            "the periodic sweep will deliver it",
            exc,
        )
        return False


__all__ = [
    "NOTICE_SWEEP_LIMIT",
    "STALE_NOTICE_POSTING_GRACE",
    "deliver_pending_obligation_notices",
    "notice_idempotency_key",
    "record_obligation_notice",
    "schedule_post_commit_notice_delivery",
]
