"""Shared authority-to-receipt lifecycle for direct inbound surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.inbound import InboundEventRow
from brain.systems.inbound.handlers import (
    InboundCompletion,
    InboundEventCompleter,
    InboundHandlerContext,
)
from brain.systems.inbound.status import STATUS_FAILED, STATUS_PROCESSED
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work


@dataclass(frozen=True)
class SurfaceIdentity:
    """Resolved execution authority plus optional lane-specific identity data."""

    authority_user_id: str | None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SurfaceTarget:
    """Lane-specific receipt target and successful tool-use context."""

    value: Mapping[str, Any]
    tool_context: Mapping[str, Any] = field(default_factory=dict)


AuthorityResolver = Callable[
    [AsyncSession, InboundHandlerContext, Mapping[str, Any]],
    Awaitable[SurfaceIdentity],
]
PayloadBuilder = Callable[
    [
        AsyncSession,
        InboundHandlerContext,
        InboundEventRow,
        Mapping[str, Any],
        SurfaceIdentity,
    ],
    Awaitable[dict[str, Any]],
]
TargetBuilder = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    SurfaceTarget,
]
AckBuilder = Callable[
    [str, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]],
    Mapping[str, Any],
]


@dataclass(frozen=True)
class SurfaceAdmissionSpec:
    """Everything that genuinely differs between direct inbound surfaces."""

    kind: str
    action_type: str
    success_operation: str
    failure_operation: str
    tool_type: str
    resolve_identity: AuthorityResolver
    build_payload: PayloadBuilder
    build_target: TargetBuilder
    success_reasoning: str
    admission_failure_reasoning: str
    missing_authority_error: str
    missing_authority_reasoning: str
    build_ack: AckBuilder | None = None
    success_tool_status: str | None = None
    outcome_target_key: str | None = None
    payload_error_types: tuple[type[Exception], ...] = ()
    invalid_payload_reason: str = "invalid_payload"
    payload_failure_operation: str | None = None
    missing_authority_reason: str = "missing_authority_user"
    missing_authority_failure_operation: str | None = None
    include_origin_in_outcome: bool = True
    action_result_target_fields: tuple[str, ...] = ()


async def admit_surface_envelope(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
    spec: SurfaceAdmissionSpec,
) -> dict[str, Any]:
    """Resolve authority, build a trigger, admit work, and complete its receipt."""

    identity = await spec.resolve_identity(session, context, normalized)
    if not identity.authority_user_id:
        return await _complete_failure(
            complete,
            spec=spec,
            event=event,
            reason=spec.missing_authority_reason,
            error=spec.missing_authority_error,
            target={"kind": spec.kind},
            reasoning_summary=spec.missing_authority_reasoning,
            operation=spec.missing_authority_failure_operation,
        )

    try:
        trigger_payload = await spec.build_payload(
            session,
            context,
            event,
            normalized,
            identity,
        )
    except spec.payload_error_types as exc:
        return await _complete_failure(
            complete,
            spec=spec,
            event=event,
            reason=spec.invalid_payload_reason,
            error=str(exc),
            target={"kind": spec.kind},
            reasoning_summary=str(exc),
            operation=spec.payload_failure_operation,
        )

    surface_target = spec.build_target(trigger_payload, normalized)
    target = dict(surface_target.value)
    admission = await admit_work(
        session,
        WorkIntakeEvent.from_trigger_payload(trigger_payload),
    )
    if not admission.ok:
        reason = admission.skipped_reason or "run_admission_failed"
        return await _complete_failure(
            complete,
            spec=spec,
            event=event,
            reason=reason,
            error=reason,
            target=target,
            reasoning_summary=admission.skipped_reason or spec.admission_failure_reasoning,
            origin=(
                normalized.get("origin")
                if spec.include_origin_in_outcome
                else None
            ),
            include_target_copy=True,
        )

    if admission.run_id is not None:
        target["run_id"] = admission.run_id
    action_result: dict[str, Any] = {
        "operation": spec.success_operation,
        "run_id": admission.run_id,
        "event_id": str(event.id),
    }
    if spec.include_origin_in_outcome:
        action_result["origin"] = normalized.get("origin")
    if spec.outcome_target_key:
        action_result[spec.outcome_target_key] = target
    for field_name in spec.action_result_target_fields:
        if field_name in target:
            action_result[field_name] = target[field_name]
    if spec.build_ack is not None:
        action_result.update(
            spec.build_ack(
                str(event.id),
                normalized,
                trigger_payload,
                target,
            )
        )

    tool_use = {
        "type": spec.tool_type,
        **(
            {"status": spec.success_tool_status}
            if spec.success_tool_status is not None
            else {}
        ),
        "run_id": admission.run_id,
        **dict(surface_target.tool_context),
    }
    return await complete(
        InboundCompletion(
            status=STATUS_PROCESSED,
            action_type=spec.action_type,
            action_result=action_result,
            confidence=1.0,
            target=target,
            tool_use=tool_use,
            reasoning_summary=spec.success_reasoning,
            reusable_pattern_candidate={
                "kind": spec.kind,
                "origin": normalized.get("origin"),
                "source_kind": context.source_kind,
            },
        )
    )


async def _complete_failure(
    complete: InboundEventCompleter,
    *,
    spec: SurfaceAdmissionSpec,
    event: InboundEventRow,
    reason: str,
    error: str,
    target: Mapping[str, Any],
    reasoning_summary: str,
    origin: Any = None,
    include_target_copy: bool = False,
    operation: str | None = None,
) -> dict[str, Any]:
    action_result: dict[str, Any] = {
        "operation": operation or spec.failure_operation,
        "reason": reason,
        "event_id": str(event.id),
    }
    if origin is not None:
        action_result["origin"] = origin
    if include_target_copy and spec.outcome_target_key:
        action_result[spec.outcome_target_key] = dict(target)
    for field_name in spec.action_result_target_fields:
        if field_name in target:
            action_result[field_name] = target[field_name]
    return await complete(
        InboundCompletion(
            status=STATUS_FAILED,
            action_type=spec.action_type,
            action_result=action_result,
            confidence=0.0,
            error=error,
            target=target,
            tool_use={"type": spec.tool_type, "status": "failed"},
            reasoning_summary=reasoning_summary,
        )
    )


__all__ = [
    "SurfaceAdmissionSpec",
    "SurfaceIdentity",
    "SurfaceTarget",
    "admit_surface_envelope",
]
