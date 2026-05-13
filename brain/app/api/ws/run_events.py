"""AgentRun event fanout for websocket clients."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.runs.ui_events import run_event_to_ui_message
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 0.5
DEFAULT_BATCH_SIZE = 100
DEFAULT_CONSUMER_NAME = "api.websocket_fanout"
_last_event_id = 0


def _event_message(event: AgentRunEventRow, run: AgentRunRow, org_id: str | None = None) -> dict[str, Any] | None:
    return run_event_to_ui_message(event, run=run, org_id=org_id)


def _backbone_status_payload(
    max_event_id: int,
    consumer_name: str = DEFAULT_CONSUMER_NAME,
    *,
    consumer_running: bool | None = None,
    stale_after_seconds: int = 120,
) -> dict[str, Any]:
    lag = max(0, max_event_id - int(_last_event_id or 0))
    health = "healthy"
    if consumer_running is False:
        health = "degraded"
    elif lag > 0:
        health = "lagging"
    return {
        "ok": True,
        "consumer_name": consumer_name,
        "consumer_running": consumer_running,
        "health": health,
        "last_event_id": _last_event_id,
        "max_event_id": max_event_id,
        "lag": lag,
        "caught_up": lag == 0,
        "replay_safe": health in {"healthy", "lagging"},
        "stale_after_seconds": stale_after_seconds,
    }


def run_event_backbone_status(
    session,
    consumer_name: str = DEFAULT_CONSUMER_NAME,
    *,
    consumer_running: bool | None = None,
    stale_after_seconds: int = 120,
) -> dict[str, Any]:
    max_event_id = int(session.scalar(select(func.coalesce(func.max(AgentRunEventRow.id), 0))) or 0)
    return _backbone_status_payload(
        max_event_id,
        consumer_name,
        consumer_running=consumer_running,
        stale_after_seconds=stale_after_seconds,
    )


async def async_run_event_backbone_status(
    session: AsyncSession,
    consumer_name: str = DEFAULT_CONSUMER_NAME,
    *,
    consumer_running: bool | None = None,
    stale_after_seconds: int = 120,
) -> dict[str, Any]:
    max_event_id = int(await session.scalar(select(func.coalesce(func.max(AgentRunEventRow.id), 0))) or 0)
    return _backbone_status_payload(
        max_event_id,
        consumer_name,
        consumer_running=consumer_running,
        stale_after_seconds=stale_after_seconds,
    )


async def fanout_run_events_once(
    ws_manager,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    logger: logging.Logger = logger,
    **_: Any,
) -> tuple[int, bool]:
    global _last_event_id
    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            select(AgentRunEventRow, AgentRunRow)
            .join(AgentRunRow, AgentRunRow.id == AgentRunEventRow.run_id)
            .where(AgentRunEventRow.id > int(_last_event_id or 0))
            .order_by(AgentRunEventRow.id.asc())
            .limit(batch_size)
        )
        rows = result.all()
    if not rows:
        return 0, False

    delivered = 0
    had_error = False
    for event, run in rows:
        org_id = str(run.org_id) if run.org_id else None
        if not org_id:
            logger.warning(
                "run_event_fanout_missing_org_id event_id=%s run_id=%s",
                event.id,
                run.id,
            )
            _last_event_id = int(event.id)
            delivered += 1
            continue
        message = _event_message(event, run, org_id)
        if message is None:
            _last_event_id = int(event.id)
            delivered += 1
            continue
        try:
            await ws_manager.broadcast_run_event(
                message["type"],
                {key: value for key, value in message.items() if key != "type"},
                org_id=org_id,
            )
        except Exception as exc:
            had_error = True
            logger.warning("run_event_fanout_failed event_id=%s error=%s", event.id, exc)
            break
        _last_event_id = int(event.id)
        delivered += 1
    return delivered, had_error


async def fanout_run_events(
    ws_manager,
    *,
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
    batch_size: int = DEFAULT_BATCH_SIZE,
    logger: logging.Logger = logger,
    **kwargs: Any,
) -> None:
    while True:
        try:
            delivered, had_error = await fanout_run_events_once(
                ws_manager,
                batch_size=batch_size,
                logger=logger,
                **kwargs,
            )
            if delivered == 0 or had_error:
                await asyncio.sleep(poll_interval_sec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("run_event_fanout_loop_error error=%s", exc)
            await asyncio.sleep(poll_interval_sec)


__all__ = [
    "DEFAULT_CONSUMER_NAME",
    "async_run_event_backbone_status",
    "fanout_run_events",
    "fanout_run_events_once",
    "run_event_backbone_status",
]
