"""Durable Cycle memory, revisions, snapshots, and evaluations."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select

from brain.kernel.common.serialization import jsonable
from brain.platform.db.models.cycle import (
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
    CycleRun,
    CycleRunEvaluation,
)
from brain.systems.briefing.packet_outcome_monitor import (
    async_monitor_packet_outcomes,
)
from brain.systems.cycles.common import (
    CYCLE_LEDGER_OUTPUT_TARGET_TYPE,
    SCHEDULED_DIGEST_RUN_KIND,
    actor_id,
    actor_type,
    cycle_run_launch_context,
    creator_payload,
    json_dict,
    string_or_none,
    validate_nonempty_trimmed,
)
from brain.systems.cycles.contract_gate import MISSION_RESULT_CONTRACT_VERDICT_KEY
from brain.systems.cycles.contracts import (
    cycle_launch_receipt,
    cycle_result_contract,
    cycle_scheduled_review_window,
    pending_evidence_health_receipt,
)
from brain.systems.cycles.degradation import (
    advance_degradation_state,
    degradation_causes,
    degradation_tracking_for_run,
)
from brain.systems.cycles.exception_ping import exception_ping_ledger_snapshot
from brain.systems.cycles.cycle_failure_guard import (
    async_apply_cycle_terminal_failure_guard,
)
from brain.systems.cycles.serializers import (
    serialize_cycle_guidance,
    serialize_cycle_output_target,
    serialize_cycle_revision,
)
from brain.systems.cycles.output_targets import default_output_target_specs
from brain.systems.runs.token_usage import (
    async_summarize_run_tree_usage_in_savepoint,
    usage_totals_payload,
)
from brain.systems.failure_guard.core import serialize_failure_guard
from brain.systems.failure_guard.cycle_latches import CycleAlertLatchStore

logger = logging.getLogger(__name__)


async def async_record_cycle_revision(
    session,
    cycle: Cycle,
    *,
    source_type: str = "system",
    source_id: str | None = None,
    rationale: str | None = None,
) -> CycleRevision:
    await session.execute(select(Cycle.id).where(Cycle.id == cycle.id).with_for_update())
    result = await session.execute(
        select(func.coalesce(func.max(CycleRevision.revision_number), 0)).where(
            CycleRevision.cycle_id == cycle.id
        )
    )
    current_revision = int(result.scalar_one() or 0)
    revision = CycleRevision(
        cycle_id=cycle.id,
        revision_number=current_revision + 1,
        source_type=actor_type(source_type),
        source_id=actor_id(source_id),
        rationale=(rationale or "").strip() or None,
        name=cycle.name,
        prompt=cycle.prompt,
        schedule_expr=cycle.schedule_expr,
        timezone=cycle.timezone,
        enabled=cycle.enabled,
        model_override=cycle.model_override,
        thinking_override=cycle.thinking_override,
        target_idea_id=cycle.target_idea_id,
        context_policy={
            "workspace_id": string_or_none(cycle.org_id),
            "owner_user_id": string_or_none(cycle.user_id),
            **creator_payload(cycle),
        },
    )
    session.add(revision)
    await session.flush()
    return revision


async def async_add_cycle_guidance(
    session,
    cycle: Cycle,
    *,
    guidance: str,
    source_type: str = "user",
    source_id: str | None = None,
    rationale: str | None = None,
    revision_id: int | None = None,
) -> CycleGuidance:
    row = CycleGuidance(
        cycle_id=cycle.id,
        revision_id=revision_id,
        source_type=actor_type(source_type),
        source_id=actor_id(source_id),
        guidance=validate_nonempty_trimmed(guidance, "guidance"),
        rationale=(rationale or "").strip() or None,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def async_add_cycle_output_target(
    session,
    cycle: Cycle,
    *,
    target_type: str,
    target_id: str | None = None,
    label: str | None = None,
    config: dict | None = None,
    source_type: str = "user",
    source_id: str | None = None,
    rationale: str | None = None,
    revision_id: int | None = None,
) -> CycleOutputTarget:
    row = CycleOutputTarget(
        cycle_id=cycle.id,
        revision_id=revision_id,
        target_type=validate_nonempty_trimmed(target_type, "target_type"),
        target_id=(str(target_id).strip() if target_id is not None else None) or None,
        label=(label or "").strip() or None,
        config=json_dict(config),
        source_type=actor_type(source_type),
        source_id=actor_id(source_id),
        rationale=(rationale or "").strip() or None,
        is_active=True,
    )
    session.add(row)
    await session.flush()
    return row


async def async_remove_cycle_output_target(
    session,
    cycle: Cycle,
    *,
    target_id: int,
    source_type: str = "user",
    source_id: str | None = None,
    rationale: str | None = None,
    revision_id: int | None = None,
) -> CycleOutputTarget | None:
    row = await session.get(CycleOutputTarget, target_id)
    if row is None or row.cycle_id != cycle.id:
        return None
    row.is_active = False
    row.revision_id = revision_id
    row.source_type = actor_type(source_type)
    row.source_id = actor_id(source_id)
    row.rationale = (rationale or "").strip() or row.rationale
    await session.flush()
    return row


async def async_prepare_cycle_run_memory_snapshot(session, cycle: Cycle, run: CycleRun) -> None:
    revision = await _async_latest_cycle_revision(session, cycle.id)
    guidance_rows = await _async_active_cycle_guidance(session, cycle.id)
    target_rows = await _async_active_cycle_output_targets(session, cycle.id)
    degradation_tracking = degradation_tracking_for_run(
        getattr(cycle, "degradation_state", None),
        scheduled_for=getattr(run, "scheduled_for", None),
    )
    snapshot = _build_cycle_run_memory_snapshot(
        cycle,
        run=run,
        revision=revision,
        guidance_rows=guidance_rows,
        target_rows=target_rows,
        degradation_tracking=degradation_tracking,
    )

    if snapshot["revision_id"] is not None:
        run.revision_id = snapshot["revision_id"]

    run.guidance_snapshot = snapshot["guidance_snapshot"]
    run.output_targets_snapshot = snapshot["output_targets_snapshot"]
    run.context_snapshot = snapshot["context_snapshot"]


def _build_cycle_run_memory_snapshot(
    cycle: Cycle,
    *,
    run: CycleRun | None = None,
    revision: CycleRevision | None,
    guidance_rows: list[CycleGuidance],
    target_rows: list[CycleOutputTarget],
    degradation_tracking: dict | None = None,
) -> dict:
    output_targets = [serialize_cycle_output_target(target) for target in target_rows]
    _ensure_default_output_targets(cycle, output_targets)
    scheduled_for = getattr(run, "scheduled_for", None)
    cycle_run_id = getattr(run, "id", None)
    launch_context = cycle_run_launch_context(run)
    degradation_tracking = degradation_tracking or degradation_tracking_for_run(
        getattr(cycle, "degradation_state", None),
        scheduled_for=scheduled_for,
    )
    result_contract = cycle_result_contract(
        degradation_tracking,
        run_kind=str(
            launch_context.get("run_kind") or SCHEDULED_DIGEST_RUN_KIND
        ),
    )

    return {
        "revision_id": revision.id if revision is not None else None,
        "guidance_snapshot": jsonable(
            [serialize_cycle_guidance(row) for row in guidance_rows]
        ),
        "output_targets_snapshot": jsonable(output_targets),
        "context_snapshot": jsonable(
            {
                "revision": serialize_cycle_revision(revision),
                "workspace_id": string_or_none(cycle.org_id),
                "owner_user_id": string_or_none(cycle.user_id),
                "scheduled_review_window": cycle_scheduled_review_window(scheduled_for),
                "result_contract": result_contract,
                "evidence_health": pending_evidence_health_receipt(scheduled_for),
                "launch_context": launch_context,
                "degradation_tracking": degradation_tracking,
                "exception_ping_ledger": exception_ping_ledger_snapshot(
                    getattr(cycle, "exception_ping_state", None)
                ),
                "launch_receipts": [
                    cycle_launch_receipt(
                        cycle_id=cycle.id,
                        cycle_run_id=cycle_run_id,
                        scheduled_for=scheduled_for,
                        timezone_name=cycle.timezone,
                        launch_context=launch_context,
                        result_contract=result_contract,
                    )
                ],
                **creator_payload(cycle),
            }
        ),
    }


def append_cycle_run_output_target_snapshot(
    run: CycleRun,
    *,
    target_type: str,
    target_id: str,
    label: str,
    config: dict | None = None,
    source_type: str = "system",
    rationale: str | None = None,
) -> None:
    output_targets = list(run.output_targets_snapshot or [])
    if any(
        isinstance(target, dict)
        and target.get("target_type") == target_type
        and target.get("target_id") == target_id
        for target in output_targets
    ):
        return
    output_targets.append(
        {
            "target_type": target_type,
            "target_id": target_id,
            "label": label,
            "config": json_dict(config),
            "source_type": source_type,
            "rationale": rationale,
            "is_active": True,
        }
    )
    run.output_targets_snapshot = jsonable(output_targets)


async def _apply_cycle_terminal_guards(
    session,
    run: CycleRun,
    cycle: Cycle,
    *,
    status: str,
    error: str | None,
    now,
) -> None:
    latch_store = CycleAlertLatchStore(session=session, cycle_id=cycle.id)
    run_kind = str(
        cycle_run_launch_context(run).get("run_kind")
        or SCHEDULED_DIGEST_RUN_KIND
    )
    if run_kind == SCHEDULED_DIGEST_RUN_KIND:
        try:
            await async_monitor_packet_outcomes(
                session,
                cycle,
                cycle_run_id=run.id,
                now=now,
                latch_store=latch_store,
            )
        except Exception:
            logger.exception(
                "Packet outcome monitor failed safely: cycle_id=%s cycle_run_id=%s",
                cycle.id,
                run.id,
            )
    evaluation = await async_apply_cycle_terminal_failure_guard(
        session,
        cycle,
        cycle_run_id=run.id,
        status=status,
        error_text=error,
        latch_store=latch_store,
        now=now,
    )
    if evaluation is not None:
        context_snapshot = dict(run.context_snapshot or {})
        context_snapshot["failure_guard"] = serialize_failure_guard(evaluation)
        run.context_snapshot = jsonable(context_snapshot)


async def finalize_cycle_run(
    run: CycleRun,
    cycle: Cycle,
    *,
    session,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    run.status = status
    run.completed_at = now
    run.error = error
    run.skip_reason = skip_reason
    cycle.last_run_at = now
    cycle.last_status = status
    cycle.last_error = error
    await record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status=status,
        error=error,
        skip_reason=skip_reason,
    )
    await _apply_cycle_terminal_guards(
        session,
        run,
        cycle,
        status=status,
        error=error,
        now=now,
    )


async def finalize_stale_cycle_run(
    run: CycleRun,
    cycle: Cycle | None,
    *,
    session,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    run.status = status
    run.completed_at = now
    run.error = error
    run.skip_reason = skip_reason
    if cycle is None:
        return
    await record_cycle_run_evaluation(
        session,
        run,
        cycle,
        status=status,
        error=error,
        skip_reason=skip_reason,
        evaluator_type="recovery",
    )
    cycle_last_run_at = _aware_utc(cycle.last_run_at)
    scheduled_for = _aware_utc(run.scheduled_for)
    if cycle_last_run_at is None or (
        scheduled_for is not None and scheduled_for >= cycle_last_run_at
    ):
        cycle.last_run_at = now
        cycle.last_status = status
        cycle.last_error = error
    await _apply_cycle_terminal_guards(
        session,
        run,
        cycle,
        status=status,
        error=error,
        now=now,
    )


async def record_cycle_run_evaluation(
    session,
    run: CycleRun,
    cycle: Cycle,
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
    evaluator_type: str = "system",
    evaluator_id: str | None = None,
) -> None:
    if run.id is None:
        raise ValueError("CycleRun must be flushed before recording an evaluation")
    usage = None
    if run.run_id is not None:
        try:
            run_usage = await async_summarize_run_tree_usage_in_savepoint(
                session,
                int(run.run_id),
            )
        except Exception:
            logger.warning(
                "cycle_run_usage_summary_failed",
                extra={"cycle_run_id": run.id, "agent_run_id": run.run_id},
                exc_info=True,
            )
            run_usage = None
        if run_usage is not None:
            usage = usage_totals_payload(run_usage)
    summary = cycle_run_evaluation_summary(
        status=status,
        error=error,
        skip_reason=skip_reason,
        usage=usage,
    )
    score = (
        1
        if status == "completed"
        else 0
        if status in {"failed", "degraded", "auth_blocked", "quota_blocked"}
        else None
    )
    context_snapshot = json_dict(getattr(run, "context_snapshot", None))
    launch_tracking = json_dict(context_snapshot.get("degradation_tracking"))
    verdict = json_dict(context_snapshot.get(MISSION_RESULT_CONTRACT_VERDICT_KEY))
    observed_causes = degradation_causes(
        status=status,
        error=error,
        evidence_health=json_dict(context_snapshot.get("evidence_health")),
        reported_evidence_health=json_dict(verdict.get("reported_evidence_health")),
    )
    degradation_state = advance_degradation_state(
        launch_tracking,
        causes=observed_causes,
        scheduled_for=run.scheduled_for,
        mandatory_digest_satisfied=(
            status == "completed"
            and verdict.get("settlement_status")
            in {"mission_success", "mission_success_after_repair"}
        ),
    )
    cycle.degradation_state = jsonable(degradation_state)
    context_snapshot["degradation_tracking"] = {
        **launch_tracking,
        "observed_causes": observed_causes,
        "result_state": degradation_state,
    }
    if usage is not None:
        context_snapshot["usage"] = usage
    run.context_snapshot = jsonable(context_snapshot)
    session.add(
        CycleRunEvaluation(
            cycle_id=cycle.id,
            cycle_run_id=run.id,
            evaluator_type=evaluator_type,
            evaluator_id=actor_id(evaluator_id),
            summary=summary,
            score=score,
            details={
                "status": status,
                "error": error,
                "skip_reason": skip_reason,
                "agent_run_id": run.run_id,
                "idea_id": string_or_none(run.idea_id),
                "scheduled_review_window": context_snapshot.get("scheduled_review_window"),
                "result_contract": context_snapshot.get("result_contract"),
                "evidence_health": context_snapshot.get("evidence_health"),
                "degradation_tracking": context_snapshot.get("degradation_tracking"),
                "launch_receipts": context_snapshot.get("launch_receipts", []),
                "usage": usage,
                MISSION_RESULT_CONTRACT_VERDICT_KEY: context_snapshot.get(
                    MISSION_RESULT_CONTRACT_VERDICT_KEY
                ),
            },
        )
    )


async def _async_latest_cycle_revision(session, cycle_id: int) -> CycleRevision | None:
    result = await session.scalars(
        select(CycleRevision)
        .where(CycleRevision.cycle_id == cycle_id)
        .order_by(CycleRevision.revision_number.desc(), CycleRevision.id.desc())
        .limit(1)
    )
    return result.first()


async def _async_active_cycle_guidance(session, cycle_id: int) -> list[CycleGuidance]:
    result = await session.scalars(
        select(CycleGuidance)
        .where(CycleGuidance.cycle_id == cycle_id, CycleGuidance.is_active.is_(True))
        .order_by(CycleGuidance.created_at.asc(), CycleGuidance.id.asc())
        .limit(25)
    )
    return list(result.all())


async def _async_active_cycle_output_targets(session, cycle_id: int) -> list[CycleOutputTarget]:
    result = await session.scalars(
        select(CycleOutputTarget)
        .where(CycleOutputTarget.cycle_id == cycle_id, CycleOutputTarget.is_active.is_(True))
        .order_by(CycleOutputTarget.created_at.asc(), CycleOutputTarget.id.asc())
        .limit(25)
    )
    return list(result.all())


def _ensure_default_output_targets(cycle: Cycle, output_targets: list[dict]) -> None:
    for spec in default_output_target_specs(cycle):
        if _has_output_target(output_targets, spec.target_type, spec.target_id):
            continue
        row = spec.snapshot()
        if spec.target_type == CYCLE_LEDGER_OUTPUT_TARGET_TYPE:
            output_targets.insert(0, row)
        else:
            output_targets.append(row)


def _has_output_target(output_targets: list[dict], target_type: str, target_id: str | None) -> bool:
    return any(
        target.get("target_type") == target_type and target.get("target_id") == target_id
        for target in output_targets
    )


def cycle_run_evaluation_summary(
    *,
    status: str,
    error: str | None = None,
    skip_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> str:
    burn = ""
    if usage is not None:
        burn = (
            f" Burn: {int(usage.get('tokens_total') or 0):,} tokens; "
            f"estimated cost ${float(usage.get('estimated_cost') or 0):.6f}."
        )
    if status == "completed":
        return "Cycle run completed and was recorded in the Cycle ledger." + burn
    if status == "failed":
        detail = error or "unknown failure"
        return f"Cycle run failed and was recorded in the Cycle ledger: {detail}" + burn
    if status == "degraded":
        detail = error or "mission result contract degraded"
        return f"Cycle run degraded and was recorded in the Cycle ledger: {detail}" + burn
    if status == "auth_blocked":
        detail = error or "external auth preflight blocked the run"
        return f"Cycle run was auth-blocked and recorded in the Cycle ledger: {detail}" + burn
    if status == "quota_blocked":
        detail = error or "subscription quota preflight blocked the run"
        return f"Cycle run was quota-blocked and recorded in the Cycle ledger: {detail}" + burn
    if status == "skipped":
        detail = skip_reason or "unknown skip reason"
        return f"Cycle run was skipped and recorded in the Cycle ledger: {detail}" + burn
    return f"Cycle run reached status {status} and was recorded in the Cycle ledger." + burn


def _aware_utc(value):
    from datetime import timezone

    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
