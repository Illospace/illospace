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


@dataclass(frozen=True)
class PreparedSurfaceEnvelope:
    """Validated surface payload, identity, and receipt target."""

    identity: SurfaceIdentity
    payload: dict[str, Any]
    surface_target: SurfaceTarget


@dataclass(frozen=True)
class RejectedSurfaceEnvelope:
    """Receipt result completed while preparing a surface envelope."""

    result: dict[str, Any]


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

    preparation = await prepare_surface_envelope(
        session,
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=spec,
    )
    if isinstance(preparation, RejectedSurfaceEnvelope):
        return preparation.result
    return await admit_prepared_surface_envelope(
        session,
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=spec,
        prepared=preparation,
        work=WorkIntakeEvent.from_trigger_payload(preparation.payload),
    )


async def prepare_surface_envelope(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
    spec: SurfaceAdmissionSpec,
) -> PreparedSurfaceEnvelope | RejectedSurfaceEnvelope:
    """Resolve authority, validate payload, and construct the receipt target."""

    identity = await spec.resolve_identity(session, context, normalized)
    if not identity.authority_user_id:
        return RejectedSurfaceEnvelope(
            await _complete_failure(
                complete,
                spec=spec,
                event=event,
                reason=spec.missing_authority_reason,
                error=spec.missing_authority_error,
                target={"kind": spec.kind},
                reasoning_summary=spec.missing_authority_reasoning,
                operation=spec.missing_authority_failure_operation,
            )
        )

    try:
        payload = await spec.build_payload(
            session,
            context,
            event,
            normalized,
            identity,
        )
    except spec.payload_error_types as exc:
        return RejectedSurfaceEnvelope(
            await _complete_failure(
                complete,
                spec=spec,
                event=event,
                reason=spec.invalid_payload_reason,
                error=str(exc),
                target={"kind": spec.kind},
                reasoning_summary=str(exc),
                operation=spec.payload_failure_operation,
            )
        )

    return PreparedSurfaceEnvelope(
        identity=identity,
        payload=payload,
        surface_target=spec.build_target(payload, normalized),
    )


async def admit_prepared_surface_envelope(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
    spec: SurfaceAdmissionSpec,
    prepared: PreparedSurfaceEnvelope,
    work: WorkIntakeEvent,
) -> dict[str, Any]:
    """Admit prepared work and complete its receipt with a concrete run ID."""

    admission = await admit_work(session, work)
    if not admission.ok or admission.run_id is None:
        reason = admission.skipped_reason or "run_admission_failed"
        return await _complete_failure(
            complete,
            spec=spec,
            event=event,
            reason=reason,
            error=reason,
            target=prepared.surface_target.value,
            reasoning_summary=(
                admission.skipped_reason or spec.admission_failure_reasoning
            ),
            origin=(
                normalized.get("origin")
                if spec.include_origin_in_outcome
                else None
            ),
            include_target_copy=True,
        )
    return await _complete_prepared_surface_envelope(
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=spec,
        prepared=prepared,
        run_id=admission.run_id,
    )


async def complete_prepared_surface_envelope(
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
    spec: SurfaceAdmissionSpec,
    prepared: PreparedSurfaceEnvelope,
) -> dict[str, Any]:
    """Complete a prepared observation without claiming run admission."""

    return await _complete_prepared_surface_envelope(
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=spec,
        prepared=prepared,
    )


async def _complete_prepared_surface_envelope(
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
    spec: SurfaceAdmissionSpec,
    prepared: PreparedSurfaceEnvelope,
    run_id: int | None = None,
) -> dict[str, Any]:
    """Complete a prepared receipt with its optional admitted-run context."""

    target = dict(prepared.surface_target.value)
    action_result: dict[str, Any] = {
        "operation": spec.success_operation,
        "event_id": str(event.id),
    }
    tool_use: dict[str, Any] = {
        "type": spec.tool_type,
        **(
            {"status": spec.success_tool_status}
            if spec.success_tool_status is not None
            else {}
        ),
        **dict(prepared.surface_target.tool_context),
    }
    if run_id is not None:
        target["run_id"] = run_id
        action_result["run_id"] = run_id
        tool_use["run_id"] = run_id
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
                prepared.payload,
                target,
            )
        )

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
    "PreparedSurfaceEnvelope",
    "RejectedSurfaceEnvelope",
    "SurfaceAdmissionSpec",
    "SurfaceIdentity",
    "SurfaceTarget",
    "admit_prepared_surface_envelope",
    "admit_surface_envelope",
    "complete_prepared_surface_envelope",
    "prepare_surface_envelope",
]
