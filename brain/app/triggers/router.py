"""Router for Illo-native triggers."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.cortex import RunAdmissionRequest, async_admit_run
from brain.systems.runs.work_intake import (
    build_chat_agent_run_request,
    build_cortex_run_admission_kwargs,
)
from brain.systems.runs.store import AsyncAgentRunStore
from brain.app.triggers.contracts import IlloTrigger, TriggerRouteResult

_CORTEX_TRIGGER_EVENTS = {"cortex.idea_created", "cortex.thread_reply"}
_CHAT_TRIGGER_EVENTS = {"chat.room_message_mention", "chat.room_thread_mention"}


async def _async_route_chat_trigger(trigger: IlloTrigger, *, session: Any | None = None) -> TriggerRouteResult:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async def _admit(active_session: Any) -> TriggerRouteResult:
        request = build_chat_agent_run_request(trigger.to_payload())
        run = await AsyncAgentRunStore(active_session).create_run(request)
        return TriggerRouteResult(ok=True, route="run", run_id=run.id)

    if session is not None:
        return await _admit(session)
    async with UnitOfWork() as uow:
        return await _admit(uow.session)


async def async_route_trigger(trigger: IlloTrigger, *, session: Any | None = None) -> TriggerRouteResult:
    """Async trigger routing for request handlers that already own an AsyncSession."""
    if trigger.event_type in _CHAT_TRIGGER_EVENTS:
        return await _async_route_chat_trigger(trigger, session=session)
    if trigger.event_type not in _CORTEX_TRIGGER_EVENTS:
        return TriggerRouteResult(
            ok=False,
            route="unsupported",
            skipped_reason=f"No router registered for {trigger.event_type}",
        )

    request_kwargs = build_cortex_run_admission_kwargs(trigger.to_payload())
    result = await async_admit_run(
        RunAdmissionRequest(**request_kwargs),
        session=session,
    )
    if not result.ok:
        return TriggerRouteResult(
            ok=False,
            route="run",
            skipped_reason=result.skipped_reason or "run_admission_failed",
        )
    return TriggerRouteResult(
        ok=True,
        route="run",
        run_id=result.run_id,
    )
