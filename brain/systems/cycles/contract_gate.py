"""Runtime result-contract gate for scheduled Cycle visible finalization."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.cycle import Cycle, CycleRun

logger = logging.getLogger(__name__)

MISSION_RESULT_CONTRACT_VERDICT_KEY = "mission_result_contract_verdict"
_CONTRACT_KIND = "autonomous_cycle_run_result"
_FINAL_ANSWER_TYPE = "final_answer"

_INTROSPECTION_MARKERS = (
    "verified current runtime facts",
    "i'm illo, the agent inside an illospace workspace",
    "runtime scope: workspace-bound",
    "source root / working directory",
    "git metadata is unavailable",
)

_EVIDENCE_HEALTH_TERMS = (
    "evidence_health",
    "evidence health",
)
_EVIDENCE_HEALTH_VALUES = (
    "ok",
    "healthy",
    "degraded",
    "partial",
    "sparse",
    "failed",
    "unavailable",
    "unknown",
)

_MISSION_OUTPUT_ALIASES: dict[str, tuple[str, ...]] = {
    "24h readout": (
        "24h readout",
        "24-hour readout",
        "24 hour readout",
        "last 24h",
        "last 24 hours",
    ),
    "failure map": ("failure map", "failure-map", "failure modes", "failures"),
    "codebase implications": (
        "codebase implications",
        "code implications",
        "implementation implications",
    ),
    "proposals": ("proposals", "proposal"),
    "tracking summary": ("tracking summary", "tracking", "domain tracking"),
    "impact loop": ("impact loop", "impact-loop"),
    "next action": ("next action", "next step", "blocker"),
}


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _candidate_summary(value: Any, *, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _looks_like_introspection_boilerplate(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(marker in normalized for marker in _INTROSPECTION_MARKERS)


def _contains_any(normalized_text: str, terms: tuple[str, ...]) -> bool:
    return any(term in normalized_text for term in terms)


def _mission_required_output_labels(mission: str) -> list[str]:
    normalized = _normalize_text(mission)
    labels = [
        label
        for label, aliases in _MISSION_OUTPUT_ALIASES.items()
        if _contains_any(normalized, aliases)
    ]
    return _dedupe(labels)


def _domain_tracking_required(mission: str, evidence_packet: dict[str, Any]) -> bool:
    if bool(evidence_packet.get("domain_side_effects_succeeded")):
        return True
    normalized = _normalize_text(mission)
    return "domain" in normalized and ("record" in normalized or "tracking" in normalized)


def _satisfies_required_output(
    requirement: str,
    *,
    normalized_candidate: str,
) -> bool:
    if requirement == "answer_the_cycle_mission":
        return (
            len(normalized_candidate) >= 80
            and not _looks_like_introspection_boilerplate(normalized_candidate)
        )
    if requirement == "summarize_workspace_evidence_or_explicit_gaps":
        return _contains_any(
            normalized_candidate,
            (
                "evidence",
                "gap",
                "gaps",
                "source",
                "sources",
                "readout",
                "reviewed",
                "observed",
                "workspace",
            ),
        )
    if requirement == "report_evidence_health":
        return _contains_any(normalized_candidate, _EVIDENCE_HEALTH_TERMS) and _contains_any(
            normalized_candidate,
            _EVIDENCE_HEALTH_VALUES,
        )
    if requirement == "record_next_action_or_blocker":
        return _contains_any(normalized_candidate, ("next action", "next step", "blocker"))
    if requirement == "short_self_review_summary":
        return _contains_any(
            normalized_candidate,
            ("self-review", "self review", "review summary", "self review summary"),
        )
    return requirement.replace("_", " ") in normalized_candidate


def evaluate_cycle_result_contract(
    *,
    candidate_answer: str,
    result_contract: dict[str, Any],
    mission: str,
    evidence_packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic verdict for a candidate scheduled-Cycle visible answer."""

    evidence_packet = _json_dict(evidence_packet)
    normalized_candidate = _normalize_text(candidate_answer)
    missing: list[str] = []

    if not normalized_candidate:
        missing.append("visible_final_answer")
    if _looks_like_introspection_boilerplate(candidate_answer):
        missing.append("candidate_is_runtime_introspection")

    for requirement in _json_list(result_contract.get("required_outputs")):
        requirement_name = str(requirement or "").strip()
        if not requirement_name:
            continue
        if not _satisfies_required_output(
            requirement_name,
            normalized_candidate=normalized_candidate,
        ):
            missing.append(requirement_name)

    for label in _mission_required_output_labels(mission):
        if not _contains_any(normalized_candidate, _MISSION_OUTPUT_ALIASES[label]):
            missing.append(label)

    if _domain_tracking_required(mission, evidence_packet):
        has_domain_tracking = "domain" in normalized_candidate and _contains_any(
            normalized_candidate,
            ("record", "records", "tracking", "ledger"),
        )
        if not has_domain_tracking:
            missing.append("domain_tracking_summary")

    missing = _dedupe(missing)
    return {
        "approved": not missing,
        "missing_outputs": missing,
        "candidate_summary": _candidate_summary(candidate_answer),
    }


def _metadata_result_contract(agent_run: AgentRunRow | None) -> dict[str, Any]:
    metadata = _json_dict(getattr(agent_run, "metadata_", None))
    contract = _json_dict(metadata.get("contract")).get("result")
    if isinstance(contract, dict):
        return dict(contract)
    launch_envelope = _json_dict(metadata.get("launch_envelope"))
    contract = launch_envelope.get("result_contract")
    if isinstance(contract, dict):
        return dict(contract)
    launch_receipt = _json_dict(metadata.get("launch_receipt"))
    contract = launch_receipt.get("result_contract")
    if isinstance(contract, dict):
        return dict(contract)
    return {}


def cycle_result_contract_for_run(
    agent_run: AgentRunRow | None,
    cycle_run: CycleRun | None,
) -> dict[str, Any]:
    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    contract = context_snapshot.get("result_contract")
    if isinstance(contract, dict):
        return dict(contract)
    return _metadata_result_contract(agent_run)


def _is_result_contract(contract: dict[str, Any]) -> bool:
    return contract.get("kind") == _CONTRACT_KIND


def _is_cycle_agent_run(agent_run: AgentRunRow | None) -> bool:
    metadata = _json_dict(getattr(agent_run, "metadata_", None))
    return metadata.get("source") == "cycle" and bool(metadata.get("cycle_run_id"))


def _cycle_run_id_for_agent_run(agent_run: AgentRunRow) -> int | None:
    metadata = _json_dict(getattr(agent_run, "metadata_", None))
    try:
        return int(metadata.get("cycle_run_id"))
    except (TypeError, ValueError):
        return None


def _mission_text(agent_run: AgentRunRow | None, cycle_run: CycleRun | None, cycle: Cycle | None) -> str:
    for value in (
        getattr(cycle_run, "prompt_snapshot", None),
        getattr(cycle, "prompt", None),
        getattr(agent_run, "input_message", None),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def persisted_cycle_contract_verdict(cycle_run: CycleRun | None) -> dict[str, Any] | None:
    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    verdict = context_snapshot.get(MISSION_RESULT_CONTRACT_VERDICT_KEY)
    return dict(verdict) if isinstance(verdict, dict) else None


def _persist_cycle_contract_verdict(cycle_run: CycleRun, verdict: dict[str, Any]) -> None:
    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    context_snapshot[MISSION_RESULT_CONTRACT_VERDICT_KEY] = dict(verdict)
    cycle_run.context_snapshot = context_snapshot


async def _latest_final_answer_artifact(
    session: Any,
    *,
    run_id: int,
) -> AgentRunArtifactRow | None:
    result = await session.scalars(
        select(AgentRunArtifactRow)
        .where(
            AgentRunArtifactRow.run_id == int(run_id),
            AgentRunArtifactRow.artifact_type == _FINAL_ANSWER_TYPE,
        )
        .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
        .limit(1)
    )
    return result.first()


async def _append_final_answer_artifact_once(
    session: Any,
    agent_run: AgentRunRow,
    text: str,
    *,
    payload: dict[str, Any],
) -> AgentRunArtifactRow:
    text_value = str(text or "")
    existing = (
        await session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(agent_run.id),
                AgentRunArtifactRow.artifact_type == _FINAL_ANSWER_TYPE,
                AgentRunArtifactRow.text == text_value,
            )
            .order_by(AgentRunArtifactRow.id.asc())
            .limit(1)
        )
    ).first()
    if existing is not None:
        return existing

    row = AgentRunArtifactRow(
        run_id=int(agent_run.id),
        root_run_id=getattr(agent_run, "root_run_id", None) or int(agent_run.id),
        artifact_type=_FINAL_ANSWER_TYPE,
        title="Cycle contract final answer",
        payload=dict(payload or {}),
        text=text_value,
        uri=None,
        visibility="public",
    )
    session.add(row)
    flush = getattr(session, "flush", None)
    if callable(flush):
        await flush()
    return row


def _event_tool_name(event: AgentRunEventRow) -> str:
    payload = _json_dict(getattr(event, "payload", None))
    return str(payload.get("tool_name") or payload.get("tool") or "").strip()


def _event_result_preview(event: AgentRunEventRow, *, limit: int = 600) -> str:
    payload = _json_dict(getattr(event, "payload", None))
    value = payload.get("result") or payload.get("result_preview") or payload.get("error") or ""
    return _candidate_summary(value, limit=limit)


async def _cycle_contract_evidence_packet(
    session: Any,
    *,
    agent_run: AgentRunRow,
    cycle_run: CycleRun,
    cycle: Cycle | None,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    side_effects_succeeded = False
    domain_side_effects_succeeded = False

    try:
        result = await session.scalars(
            select(AgentRunEventRow)
            .where(AgentRunEventRow.run_id == int(agent_run.id))
            .order_by(AgentRunEventRow.sequence_no.desc(), AgentRunEventRow.id.desc())
            .limit(40)
        )
        event_rows = list(result.all())
    except Exception:
        logger.debug("cycle_contract_gate_event_packet_failed", exc_info=True)
        event_rows = []

    for event in reversed(event_rows):
        event_type = str(getattr(event, "event_type", "") or "")
        payload = _json_dict(getattr(event, "payload", None))
        tool_name = _event_tool_name(event)
        result_preview = _event_result_preview(event)
        is_error = bool(payload.get("is_error")) or event_type == "run.tool_failed"
        if event_type == "run.tool_completed" and not is_error:
            side_effects_succeeded = True
            domain_blob = _normalize_text(f"{tool_name} {result_preview}")
            if "domain" in domain_blob:
                domain_side_effects_succeeded = True
        events.append(
            {
                "event_type": event_type,
                "tool_name": tool_name,
                "is_error": is_error,
                "result_preview": result_preview,
            }
        )

    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    return {
        "cycle_id": getattr(cycle, "id", None) or getattr(cycle_run, "cycle_id", None),
        "cycle_run_id": getattr(cycle_run, "id", None),
        "agent_run_id": getattr(agent_run, "id", None),
        "scheduled_review_window": context_snapshot.get("scheduled_review_window"),
        "evidence_health": context_snapshot.get("evidence_health"),
        "side_effects_succeeded": side_effects_succeeded,
        "domain_side_effects_succeeded": domain_side_effects_succeeded,
        "recent_events": events[-20:],
    }


def _repair_prompt(
    *,
    mission: str,
    result_contract: dict[str, Any],
    evidence_packet: dict[str, Any],
    missing_outputs: list[str],
    candidate_answer: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are repairing one scheduled Cycle final answer before it becomes visible. "
                "Use only the supplied mission, result contract, evidence packet, and candidate. "
                "Do not claim fresh tool access. Produce the corrected visible final answer only. "
                "If evidence is insufficient, say evidence_health=degraded and name the gap."
            ),
        },
        {
            "role": "user",
            "content": (
                "Original Cycle mission:\n"
                f"{mission[:4000]}\n\n"
                "Result contract:\n"
                f"{json.dumps(result_contract, ensure_ascii=True, sort_keys=True, default=str)[:3000]}\n\n"
                "Evidence packet:\n"
                f"{json.dumps(evidence_packet, ensure_ascii=True, sort_keys=True, default=str)[:5000]}\n\n"
                "Missing required outputs:\n"
                f"{json.dumps(missing_outputs, ensure_ascii=True)}\n\n"
                "Candidate visible answer:\n"
                f"{candidate_answer[:4000]}"
            ),
        },
    ]


def _repair_model(agent_run: AgentRunRow) -> str:
    model_policy = _json_dict(getattr(agent_run, "model_policy", None))
    metadata = _json_dict(getattr(agent_run, "metadata_", None))
    for container in (model_policy, metadata):
        for key in ("model", "model_override"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    from brain.platform.providers.model_policy import get_default_model, resolve_default_provider

    provider = resolve_default_provider(
        user_id=str(getattr(agent_run, "user_id", "") or "") or None,
        org_id=str(getattr(agent_run, "org_id", "") or "") or None,
    )
    return get_default_model(
        provider=provider,
        include_provider_prefix=False,
        user_id=str(getattr(agent_run, "user_id", "") or "") or None,
    )


async def _async_repair_cycle_contract_answer(
    *,
    agent_run: AgentRunRow,
    mission: str,
    result_contract: dict[str, Any],
    evidence_packet: dict[str, Any],
    missing_outputs: list[str],
    candidate_answer: str,
) -> str | None:
    """Run exactly one bounded LLM repair pass for a failed Cycle contract answer."""

    try:
        from brain.platform.integrations.llm import resolve_llm_client
        from brain.platform.integrations.providers import get_provider
        from brain.platform.providers.model_policy import infer_provider_from_model, resolve_default_provider
        from brain.systems.runs.direct_loop.request import build_api_request
        from brain.systems.sessions import _content_to_dicts
        from brain.systems.sessions.harvest import _extract_text

        user_id = str(getattr(agent_run, "user_id", "") or "") or None
        org_id = str(getattr(agent_run, "org_id", "") or "") or None
        model = _repair_model(agent_run)
        default_provider = resolve_default_provider(user_id=user_id, org_id=org_id)
        requested_provider = infer_provider_from_model(model, default=default_provider)
        llm = resolve_llm_client(user_id=user_id, org_id=org_id, provider=requested_provider)
        provider = get_provider(llm.provider, llm.client)
        session_id = f"cycle-contract-repair-{int(agent_run.id)}"
        request = build_api_request(
            model=model,
            messages=_repair_prompt(
                mission=mission,
                result_contract=result_contract,
                evidence_packet=evidence_packet,
                missing_outputs=missing_outputs,
                candidate_answer=candidate_answer,
            ),
            max_tokens=1200,
            system=None,
            tools=None,
            reasoning_effort=None,
            extra_headers=llm.build_request_headers(session_id=session_id),
            provider_name=llm.provider,
            session_id=session_id,
            persist_session=False,
            cache_tools=False,
            operation_type="verifier",
        )
        response = await asyncio.to_thread(provider.create, request)
        text = _extract_text([{"role": "assistant", "content": _content_to_dicts(response.content)}])
        return str(text or "").strip() or None
    except Exception:
        logger.exception("cycle_contract_repair_failed", extra={"run_id": getattr(agent_run, "id", None)})
        return None


def _degraded_visible_answer(missing_outputs: list[str]) -> str:
    missing = ", ".join(missing_outputs[:8]) if missing_outputs else "required Cycle outputs"
    return (
        "Cycle run degraded: mission_contract_failed. The run completed side effects, "
        f"but its visible final answer did not satisfy the Cycle result contract. Missing: {missing}."
    )


def _base_verdict(
    *,
    candidate_answer: str,
    candidate_artifact_id: Any,
    initial_review: dict[str, Any],
    evidence_packet: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": "cycle_result_contract_verdict",
        "schema_version": 1,
        "candidate_artifact_id": candidate_artifact_id,
        "candidate_summary": initial_review.get("candidate_summary") or _candidate_summary(candidate_answer),
        "missing_outputs": list(initial_review.get("missing_outputs") or []),
        "final_missing_outputs": list(initial_review.get("missing_outputs") or []),
        "repair_attempted": False,
        "repair_succeeded": False,
        "settlement_status": (
            "mission_success" if initial_review.get("approved") else "mission_contract_failed"
        ),
        "visible_answer_source": "candidate" if initial_review.get("approved") else None,
        "side_effects_succeeded": bool(evidence_packet.get("side_effects_succeeded")),
        "domain_side_effects_succeeded": bool(evidence_packet.get("domain_side_effects_succeeded")),
    }


async def async_prepare_cycle_run_visible_finalization(
    session: Any,
    agent_run_id: int,
) -> dict[str, Any] | None:
    """Ensure a scheduled Cycle final answer satisfies its result contract before visibility."""

    agent_run = await session.get(AgentRunRow, int(agent_run_id))
    if not _is_cycle_agent_run(agent_run):
        return None
    cycle_run_id = _cycle_run_id_for_agent_run(agent_run)
    if cycle_run_id is None:
        return None
    cycle_run = await session.get(CycleRun, cycle_run_id)
    cycle = await session.get(Cycle, cycle_run.cycle_id) if cycle_run else None
    if cycle_run is None or cycle is None:
        return None

    existing = persisted_cycle_contract_verdict(cycle_run)
    if existing is not None:
        return existing

    result_contract = cycle_result_contract_for_run(agent_run, cycle_run)
    if not _is_result_contract(result_contract):
        return None

    artifact = await _latest_final_answer_artifact(session, run_id=int(agent_run.id))
    candidate_answer = str(getattr(artifact, "text", None) or "").strip()
    mission = _mission_text(agent_run, cycle_run, cycle)
    evidence_packet = await _cycle_contract_evidence_packet(
        session,
        agent_run=agent_run,
        cycle_run=cycle_run,
        cycle=cycle,
    )
    initial_review = evaluate_cycle_result_contract(
        candidate_answer=candidate_answer,
        result_contract=result_contract,
        mission=mission,
        evidence_packet=evidence_packet,
    )
    verdict = _base_verdict(
        candidate_answer=candidate_answer,
        candidate_artifact_id=getattr(artifact, "id", None),
        initial_review=initial_review,
        evidence_packet=evidence_packet,
    )

    if initial_review["approved"]:
        _persist_cycle_contract_verdict(cycle_run, verdict)
        return verdict

    verdict["repair_attempted"] = True
    try:
        repair_answer = await _async_repair_cycle_contract_answer(
            agent_run=agent_run,
            mission=mission,
            result_contract=result_contract,
            evidence_packet=evidence_packet,
            missing_outputs=list(initial_review["missing_outputs"]),
            candidate_answer=candidate_answer,
        )
    except Exception:
        logger.exception("cycle_contract_repair_call_failed", extra={"run_id": int(agent_run.id)})
        repair_answer = None
    if repair_answer:
        repair_review = evaluate_cycle_result_contract(
            candidate_answer=repair_answer,
            result_contract=result_contract,
            mission=mission,
            evidence_packet=evidence_packet,
        )
        if repair_review["approved"]:
            repaired_artifact = await _append_final_answer_artifact_once(
                session,
                agent_run,
                repair_answer,
                payload={
                    "source": "cycle_result_contract_gate",
                    "repair_for_artifact_id": getattr(artifact, "id", None),
                    "initial_missing_outputs": list(initial_review["missing_outputs"]),
                },
            )
            verdict.update(
                {
                    "repair_succeeded": True,
                    "settlement_status": "mission_success_after_repair",
                    "visible_answer_source": "repair",
                    "repaired_artifact_id": getattr(repaired_artifact, "id", None),
                    "repaired_summary": repair_review["candidate_summary"],
                    "final_missing_outputs": [],
                }
            )
            _persist_cycle_contract_verdict(cycle_run, verdict)
            return verdict
        verdict["repair_missing_outputs"] = list(repair_review["missing_outputs"])
        verdict["final_missing_outputs"] = list(repair_review["missing_outputs"])

    missing_outputs = list(verdict.get("final_missing_outputs") or verdict["missing_outputs"])
    degraded_answer = _degraded_visible_answer(missing_outputs)
    degraded_artifact = await _append_final_answer_artifact_once(
        session,
        agent_run,
        degraded_answer,
        payload={
            "source": "cycle_result_contract_gate",
            "settlement_status": "mission_contract_failed",
            "missing_outputs": missing_outputs,
        },
    )
    verdict.update(
        {
            "settlement_status": "mission_contract_failed",
            "visible_answer_source": "degraded_explanation",
            "degraded_artifact_id": getattr(degraded_artifact, "id", None),
            "degraded_summary": _candidate_summary(degraded_answer),
            "final_missing_outputs": missing_outputs,
        }
    )
    _persist_cycle_contract_verdict(cycle_run, verdict)
    return verdict


def cycle_finalization_status_from_verdict(
    requested_status: str,
    *,
    verdict: dict[str, Any] | None,
    error: str | None = None,
) -> tuple[str, str | None]:
    if requested_status != "completed":
        return requested_status, error
    if _json_dict(verdict).get("settlement_status") == "mission_contract_failed":
        missing = _json_list(_json_dict(verdict).get("final_missing_outputs"))
        detail = ", ".join(str(item) for item in missing[:8]) or "required Cycle outputs"
        return "degraded", f"mission_contract_failed: missing {detail}"
    return requested_status, error
