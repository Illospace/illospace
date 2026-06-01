"""Router for Illo-native triggers."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work
from brain.app.triggers.contracts import IlloTrigger, TriggerRouteResult

_CORTEX_TRIGGER_EVENTS = {
    "cortex.idea_created",
    "cortex.thread_reply",
    "cortex.thread_discussion_mention",
}
_CHAT_TRIGGER_EVENTS = {"chat.room_message_mention", "chat.room_thread_mention"}
_SLACK_TRIGGER_EVENTS = {"slack.app_mention", "slack.direct_message"}
_RUN_TRIGGER_EVENTS = _CORTEX_TRIGGER_EVENTS | _CHAT_TRIGGER_EVENTS | _SLACK_TRIGGER_EVENTS


async def _async_route_run_trigger(trigger: IlloTrigger, *, session: Any | None = None) -> TriggerRouteResult:
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async def _admit(active_session: Any) -> TriggerRouteResult:
        result = await admit_work(
            active_session,
            WorkIntakeEvent.from_trigger_payload(trigger.to_payload()),
        )
        if not result.ok:
            return TriggerRouteResult(
                ok=False,
                route="run",
                skipped_reason=result.skipped_reason or "run_admission_failed",
            )
        return TriggerRouteResult(ok=True, route="run", run_id=result.run_id)

    if session is not None:
        return await _admit(session)
    async with UnitOfWork() as uow:
        return await _admit(uow.session)


async def async_route_trigger(trigger: IlloTrigger, *, session: Any | None = None) -> TriggerRouteResult:
    """Async trigger routing for request handlers that already own an AsyncSession."""
    if trigger.event_type not in _RUN_TRIGGER_EVENTS:
        return TriggerRouteResult(
            ok=False,
            route="unsupported",
            skipped_reason=f"No router registered for {trigger.event_type}",
        )
    return await _async_route_run_trigger(trigger, session=session)
