"""One-shot model-context notices for cumulative AgentRun token usage."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import logging
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel import config
from brain.platform.db.models.agent_run import AgentRunEventRow
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.runs.domain import EventVisibility
from brain.systems.runs.events import run_event
from brain.systems.runs.runtime_activity import load_run_activity
from brain.systems.runs.store import AsyncAgentRunStore

logger = logging.getLogger("agent")

SOFT_NOTICE_KEY = "soft"
CEILING_NOTICE_KEY = "ceiling"
BUDGET_NOTICE_SENT_EVENT = "run.budget_notice_sent"
_NOTICE_KEYS = frozenset({SOFT_NOTICE_KEY, CEILING_NOTICE_KEY})


@dataclass(frozen=True)
class RunBudgetNotice:
    """A newly due notice and its once-per-run identity."""

    key: str
    message: dict[str, str]


def _notice_kind(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    kind = str(payload.get("kind") or "")
    return kind if kind in _NOTICE_KEYS else None


async def _recorded_notice_keys(session: AsyncSession, run_id: int) -> set[str]:
    payloads = (
        await session.scalars(
            select(AgentRunEventRow.payload).where(
                AgentRunEventRow.run_id == run_id,
                AgentRunEventRow.event_type == BUDGET_NOTICE_SENT_EVENT,
            )
        )
    ).all()
    return {
        kind
        for payload in payloads
        if (kind := _notice_kind(payload)) is not None
    }


async def load_budget_notices_sent(run_id: int | None) -> set[str]:
    """Recover once-per-run notice state from the internal event ledger."""

    if run_id is None or config.AGENT_RUN_CUMULATIVE_TOKEN_BUDGET == 0:
        return set()
    resolved_run_id = int(run_id)
    try:
        async with UnitOfWork() as uow:
            return await _recorded_notice_keys(uow.session, resolved_run_id)
    except Exception:
        logger.warning(
            "Failed to load durable run budget notice state",
            extra={"run_id": resolved_run_id},
            exc_info=True,
        )
        # Fail closed: an unavailable ledger must never cause a duplicate or
        # potentially misleading safety notice.
        return set(_NOTICE_KEYS)


async def record_budget_notice_sent(
    *,
    run_id: int,
    notice: RunBudgetNotice,
) -> bool:
    """Persist a notice marker, returning true only for a newly recorded notice."""

    resolved_run_id = int(run_id)
    try:
        async with UnitOfWork() as uow:
            store = AsyncAgentRunStore(uow.session)
            run = await store.require_run(resolved_run_id)
            await store.lock_event_stream(resolved_run_id)
            if notice.key in await _recorded_notice_keys(
                uow.session,
                resolved_run_id,
            ):
                return False
            await store.append_event(
                run_event(
                    resolved_run_id,
                    BUDGET_NOTICE_SENT_EVENT,
                    {"kind": notice.key},
                    root_run_id=run.root_run_id or resolved_run_id,
                    visibility=EventVisibility.INTERNAL,
                )
            )
        return True
    except Exception:
        logger.warning(
            "Failed to persist durable run budget notice state",
            extra={"run_id": resolved_run_id, "notice_kind": notice.key},
            exc_info=True,
        )
        return False


def _notice(
    *,
    key: str,
    run_id: int,
    budget_tokens_used: int,
    ceiling_tokens: int,
) -> RunBudgetNotice:
    position = (
        f"Run {run_id} has used {budget_tokens_used} of {ceiling_tokens} cumulative "
        "budget tokens."
    )
    if key == SOFT_NOTICE_KEY:
        instruction = (
            "You are nearing this run's token budget. "
            "Wrap up now and persist durable progress before closing."
        )
    else:
        instruction = (
            "This run has reached its token budget ceiling. Stop new work: "
            "persist what you have and emit the closing output now."
        )
    return RunBudgetNotice(
        key=key,
        message={
            "role": "user",
            "content": f"{position} {instruction}",
        },
    )


async def load_due_budget_notices(
    *,
    run_id: int | None,
    sent: Collection[str],
) -> tuple[RunBudgetNotice, ...]:
    """Load ledger-backed usage and return each newly crossed notice once."""

    if run_id is None or _NOTICE_KEYS.issubset(sent):
        return ()
    ceiling_tokens = config.AGENT_RUN_CUMULATIVE_TOKEN_BUDGET
    if ceiling_tokens == 0:
        return ()

    resolved_run_id = int(run_id)
    try:
        activity = await load_run_activity(resolved_run_id)
    except Exception:
        logger.warning(
            "Failed to load durable run activity for budget notice",
            extra={"run_id": resolved_run_id},
            exc_info=True,
        )
        return ()

    budget_tokens_used = max(
        0,
        int(activity.get("run_budget_tokens_used") or 0),
    )
    soft_threshold = math.ceil(
        ceiling_tokens * config.AGENT_RUN_BUDGET_NOTICE_FRACTION
    )
    notices: list[RunBudgetNotice] = []
    if budget_tokens_used >= soft_threshold and SOFT_NOTICE_KEY not in sent:
        notices.append(
            _notice(
                key=SOFT_NOTICE_KEY,
                run_id=resolved_run_id,
                budget_tokens_used=budget_tokens_used,
                ceiling_tokens=ceiling_tokens,
            )
        )
    if budget_tokens_used >= ceiling_tokens and CEILING_NOTICE_KEY not in sent:
        notices.append(
            _notice(
                key=CEILING_NOTICE_KEY,
                run_id=resolved_run_id,
                budget_tokens_used=budget_tokens_used,
                ceiling_tokens=ceiling_tokens,
            )
        )
    return tuple(notices)


__all__ = [
    "BUDGET_NOTICE_SENT_EVENT",
    "CEILING_NOTICE_KEY",
    "RunBudgetNotice",
    "SOFT_NOTICE_KEY",
    "load_budget_notices_sent",
    "load_due_budget_notices",
    "record_budget_notice_sent",
]
