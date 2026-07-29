"""Runtime result-contract gate for scheduled Cycle visible finalization."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy import select

from brain.platform.integrations.provider_error_sentinel import (
    provider_error_kind,
    safe_provider_error_sentinel,
)
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.systems.personality import soul_prompt_section

logger = logging.getLogger(__name__)

MISSION_RESULT_CONTRACT_VERDICT_KEY = "mission_result_contract_verdict"
_CONTRACT_KIND = "autonomous_cycle_run_result"
_FINAL_ANSWER_TYPE = "final_answer"
_MANDATORY_DEGRADATION_ESCALATION_PREFIX = "mandatory_degradation_escalation:"

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
_EVIDENCE_HEALTH_REPORT_RE = re.compile(
    r"(?:evidence_health|evidence health)\s*(?::|=|is)\s*"
    r"(?P<value>ok|healthy|degraded|partial|sparse|failed|unavailable|unknown)"
    r"(?P<detail>[^\n.]{0,240})",
    re.IGNORECASE,
)

_NON_PRESERVABLE_OUTPUTS = frozenset(
    {
        "visible_final_answer",
        "candidate_is_runtime_introspection",
    }
)


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


def _missing_outputs_allow_append(missing_outputs: list[str]) -> bool:
    return not _NON_PRESERVABLE_OUTPUTS.intersection(missing_outputs)


def _looks_like_introspection_boilerplate(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(marker in normalized for marker in _INTROSPECTION_MARKERS)


def _contains_any(normalized_text: str, terms: tuple[str, ...]) -> bool:
    return any(term in normalized_text for term in terms)


def _reported_evidence_health(candidate_answer: str | None) -> dict[str, Any] | None:
    matches = list(_EVIDENCE_HEALTH_REPORT_RE.finditer(str(candidate_answer or "")))
    if not matches:
        return None
    match = matches[-1]
    value = str(match.group("value") or "").lower()
    status = "ok" if value in {"ok", "healthy"} else "degraded"
    report: dict[str, Any] = {"status": status, "reported_value": value}
    if status == "degraded":
        detail = re.sub(r"^[\s:;=—-]+", "", str(match.group("detail") or "")).strip()
        report["cause"] = detail or "Evidence health reported degraded without a named cause."
    return report


def _satisfies_required_output(
    requirement: str,
    *,
    normalized_candidate: str,
    result_contract: dict[str, Any],
) -> bool:
    if requirement.startswith(_MANDATORY_DEGRADATION_ESCALATION_PREFIX):
        required_key = requirement.removeprefix(
            _MANDATORY_DEGRADATION_ESCALATION_PREFIX
        )
        for raw_escalation in _json_list(
            result_contract.get("mandatory_degradation_escalations")
        ):
            escalation = _json_dict(raw_escalation)
            if str(escalation.get("key") or "").strip() != required_key:
                continue
            summary = _normalize_text(escalation.get("summary"))
            return bool(summary and summary in normalized_candidate)
        return False
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
    if requirement in {"domain_tracking", "domain_tracking_summary"}:
        return _contains_any(
            normalized_candidate,
            ("domain tracking", "tracking summary", "domain record", "domain ledger"),
        )
    return requirement.replace("_", " ") in normalized_candidate


def evaluate_cycle_result_contract(
    *,
    candidate_answer: str | None,
    result_contract: dict[str, Any],
    mission: str,
    evidence_packet: dict[str, Any] | None = None,
    provider_exception: BaseException | str | None = None,
) -> dict[str, Any]:
    """Validate a visible answer against only its advertised required outputs."""

    normalized_candidate = _normalize_text(candidate_answer)
    detected_provider_error = provider_error_kind(
        candidate_answer,
        provider_exception=provider_exception,
    )
    missing: list[str] = []

    if not normalized_candidate or detected_provider_error:
        missing.append("visible_final_answer")
    if _looks_like_introspection_boilerplate(candidate_answer):
        missing.append("candidate_is_runtime_introspection")

    enforced_required_outputs = _dedupe(
        [
            str(requirement or "").strip()
            for requirement in _json_list(result_contract.get("required_outputs"))
        ]
    )
    for requirement_name in enforced_required_outputs:
        if not _satisfies_required_output(
            requirement_name,
            normalized_candidate=normalized_candidate,
            result_contract=result_contract,
        ):
            missing.append(requirement_name)

    missing = _dedupe(missing)
    return {
        "approved": not missing,
        "missing_outputs": missing,
        "enforced_required_outputs": enforced_required_outputs,
        "candidate_summary": (
            safe_provider_error_sentinel(detected_provider_error)
            if detected_provider_error
            else _candidate_summary(candidate_answer)
        ),
        "provider_error": detected_provider_error,
        "reported_evidence_health": _reported_evidence_health(candidate_answer),
    }


def _metadata_result_contract(agent_run: AgentRunRow | None) -> dict[str, Any]:
    metadata = _json_dict(getattr(agent_run, "metadata_", None))
    launch_envelope = _json_dict(metadata.get("launch_envelope"))
    contract = launch_envelope.get("result_contract")
    if isinstance(contract, dict):
        return dict(contract)
    contract = _json_dict(metadata.get("contract")).get("result")
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
    contract = _metadata_result_contract(agent_run)
    if contract:
        return contract
    context_snapshot = _json_dict(getattr(cycle_run, "context_snapshot", None))
    contract = context_snapshot.get("result_contract")
    if isinstance(contract, dict):
        return dict(contract)
    return {}


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
    detected_provider_error = provider_error_kind(text_value)
    if detected_provider_error:
        logger.error(
            "cycle_provider_error_final_answer_append_blocked run_id=%s raw_error=%s",
            getattr(agent_run, "id", None),
            text_value,
        )
        text_value = safe_provider_error_sentinel(detected_provider_error)
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


def _captured_provider_error(evidence_packet: dict[str, Any]) -> str | None:
    for event in reversed(_json_list(evidence_packet.get("recent_events"))):
        event_data = _json_dict(event)
        if event_data.get("event_type") != "run.failed":
            continue
        error = str(event_data.get("result_preview") or "").strip()
        if provider_error_kind(error):
            return error
    return None


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
    append_only: bool = False,
) -> list[dict[str, str]]:
    repair_instruction = (
        "The candidate is a substantive draft that must remain unchanged. Produce only one or "
        "more appendable sections for the named missing outputs. Append only the missing sections; "
        "do not repeat or rewrite the candidate. Return the new section text only."
        if append_only
        else "Produce the corrected visible final answer only."
    )
    candidate_label = (
        "Candidate visible answer to preserve unchanged"
        if append_only
        else "Candidate visible answer"
    )
    soul_section = soul_prompt_section()
    return [
        {
            "role": "system",
            "content": (
                (f"{soul_section}\n\n" if soul_section else "")
                + "You are repairing one scheduled Cycle final answer before it becomes visible. "
                "Use only the supplied mission, result contract, evidence packet, and candidate. "
                f"Do not claim fresh tool access. {repair_instruction} "
                "The repaired text is read by a teammate, so write it in the voice the Soul "
                "above describes. "
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
                f"{candidate_label}:\n"
                f"{candidate_answer[:4000]}"
            ),
        },
    ]


async def _repair_model(session: Any, agent_run: AgentRunRow) -> str:
    model_policy = _json_dict(getattr(agent_run, "model_policy", None))
    metadata = _json_dict(getattr(agent_run, "metadata_", None))
    for container in (model_policy, metadata):
        for key in ("model", "model_override"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    from brain.platform.providers.model_policy import async_get_default_model

    return await async_get_default_model(
        session,
        include_provider_prefix=True,
        user_id=str(getattr(agent_run, "user_id", "") or "") or None,
        org_id=str(getattr(agent_run, "org_id", "") or "") or None,
    )


async def _async_repair_cycle_contract_answer(
    *,
    session: Any,
    agent_run: AgentRunRow,
    mission: str,
    result_contract: dict[str, Any],
    evidence_packet: dict[str, Any],
    missing_outputs: list[str],
    candidate_answer: str,
) -> str | None:
    """Run exactly one bounded LLM repair pass for a failed Cycle contract answer."""

    try:
        from brain.platform.integrations.llm import async_resolve_llm_client
        from brain.platform.integrations.providers import get_provider
        from brain.platform.providers.model_policy import (
            infer_provider_from_model,
            required_openai_auth_mode,
            resolve_default_provider,
        )
        from brain.systems.runs.direct_loop.request import build_api_request
        from brain.systems.sessions import _content_to_dicts
        from brain.systems.sessions.harvest import _extract_text

        user_id = str(getattr(agent_run, "user_id", "") or "") or None
        org_id = str(getattr(agent_run, "org_id", "") or "") or None
        model = await _repair_model(session, agent_run)
        default_provider = resolve_default_provider(user_id=user_id, org_id=org_id)
        requested_provider = infer_provider_from_model(model, default=default_provider)
        llm = await async_resolve_llm_client(
            user_id=user_id,
            org_id=org_id,
            provider=requested_provider,
            auth_mode=(
                required_openai_auth_mode(model)
                if requested_provider == "openai"
                else None
            ),
            session=session,
        )
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
                append_only=_missing_outputs_allow_append(missing_outputs),
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


def _completed_side_effect_names(evidence_packet: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for event in _json_list(evidence_packet.get("recent_events")):
        event_data = _json_dict(event)
        if event_data.get("event_type") != "run.tool_completed" or event_data.get("is_error"):
            continue
        name = str(event_data.get("tool_name") or "").strip()
        if name:
            names.append(name)
    return _dedupe(names)


def _candidate_can_be_preserved(
    candidate_answer: str,
    review: dict[str, Any],
) -> bool:
    missing_outputs = set(_json_list(review.get("missing_outputs")))
    return (
        bool(str(candidate_answer or "").strip())
        and not review.get("provider_error")
        and _missing_outputs_allow_append(list(missing_outputs))
    )


def _append_targeted_repair(candidate_answer: str, repair_answer: str) -> str:
    candidate = str(candidate_answer or "").strip()
    repair = str(repair_answer or "").strip()
    if not candidate:
        return repair
    if not repair or repair in candidate:
        return candidate
    if candidate in repair:
        return repair
    return f"{candidate}\n\n{repair}"


def _degraded_visible_answer(
    missing_outputs: list[str],
    *,
    provider_error: str | None = None,
    evidence_packet: dict[str, Any] | None = None,
    candidate_answer: str | None = None,
) -> str:
    if provider_error:
        side_effect_names = _completed_side_effect_names(_json_dict(evidence_packet))
        side_effect_sentence = (
            "Side effects completed before the provider failure remain applied: "
            f"{', '.join(side_effect_names[:8])}."
            if side_effect_names
            else "No completed side effects were recorded before the provider failure."
        )
        return (
            "Cycle run degraded: mission_contract_failed. The upstream model provider failed "
            f"with {provider_error} after a bounded retry, so no safe visible mission answer "
            f"was produced. {side_effect_sentence}"
        )
    missing = ", ".join(missing_outputs[:8]) if missing_outputs else "required Cycle outputs"
    explanation = (
        "Cycle run degraded: mission_contract_failed. The run completed side effects, "
        f"but its visible final answer did not satisfy the Cycle result contract. Missing: {missing}."
    )
    candidate = str(candidate_answer or "").strip()
    if not candidate:
        return explanation
    return f"{candidate}\n\n---\n{explanation}"


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
        "enforced_required_outputs": list(
            initial_review.get("enforced_required_outputs") or []
        ),
        "repair_attempted": False,
        "repair_succeeded": False,
        "settlement_status": (
            "mission_success" if initial_review.get("approved") else "mission_contract_failed"
        ),
        "visible_answer_source": "candidate" if initial_review.get("approved") else None,
        "side_effects_succeeded": bool(evidence_packet.get("side_effects_succeeded")),
        "domain_side_effects_succeeded": bool(evidence_packet.get("domain_side_effects_succeeded")),
        "provider_error": initial_review.get("provider_error"),
        "reported_evidence_health": initial_review.get("reported_evidence_health"),
    }


async def async_prepare_cycle_run_visible_finalization(
    session: Any,
    agent_run_id: int,
    *,
    provider_exception: BaseException | str | None = None,
    provider_errors_only: bool = False,
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
    captured_provider_exception = provider_exception or _captured_provider_error(evidence_packet)
    detected_provider_error = provider_error_kind(
        candidate_answer,
        provider_exception=captured_provider_exception,
    )
    if provider_errors_only and not detected_provider_error:
        return None
    if detected_provider_error:
        raw_provider_error = candidate_answer or str(captured_provider_exception or "")
        logger.error(
            "cycle_provider_error_final_answer_blocked run_id=%s artifact_id=%s raw_error=%s",
            int(agent_run.id),
            getattr(artifact, "id", None),
            raw_provider_error,
        )
        candidate_answer = safe_provider_error_sentinel(detected_provider_error)
        if artifact is not None:
            artifact.text = candidate_answer
            artifact.visibility = "internal"
            artifact_payload = _json_dict(getattr(artifact, "payload", None))
            artifact_payload.update(
                {
                    "provider_error": detected_provider_error,
                    "blocked_from_visibility": True,
                }
            )
            artifact.payload = artifact_payload
    initial_review = evaluate_cycle_result_contract(
        candidate_answer=candidate_answer,
        result_contract=result_contract,
        mission=mission,
        evidence_packet=evidence_packet,
        provider_exception=captured_provider_exception,
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

    append_only = _candidate_can_be_preserved(candidate_answer, initial_review)
    preserved_answer = candidate_answer if append_only else None
    preserved_answer_source = "candidate" if append_only else None
    verdict["repair_attempted"] = True
    verdict["repair_mode"] = "append_missing_outputs" if append_only else "replace_invalid_answer"
    try:
        repair_answer = await _async_repair_cycle_contract_answer(
            session=session,
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
        combined_answer = (
            _append_targeted_repair(candidate_answer, repair_answer)
            if append_only and not provider_error_kind(repair_answer)
            else repair_answer
        )
        repair_review = evaluate_cycle_result_contract(
            candidate_answer=combined_answer,
            result_contract=result_contract,
            mission=mission,
            evidence_packet=evidence_packet,
        )
        if repair_review.get("provider_error"):
            logger.error(
                "cycle_contract_repair_provider_error run_id=%s raw_error=%s",
                int(agent_run.id),
                repair_answer,
            )
        if repair_review["approved"]:
            repaired_artifact = await _append_final_answer_artifact_once(
                session,
                agent_run,
                combined_answer,
                payload={
                    "source": "cycle_result_contract_gate",
                    "repair_for_artifact_id": getattr(artifact, "id", None),
                    "initial_missing_outputs": list(initial_review["missing_outputs"]),
                    "repair_mode": verdict["repair_mode"],
                },
            )
            verdict.update(
                {
                    "repair_succeeded": True,
                    "settlement_status": "mission_success_after_repair",
                    "visible_answer_source": (
                        "candidate_with_repair" if append_only else "repair"
                    ),
                    "repaired_artifact_id": getattr(repaired_artifact, "id", None),
                    "repaired_summary": repair_review["candidate_summary"],
                    "final_missing_outputs": [],
                    "reported_evidence_health": repair_review.get(
                        "reported_evidence_health"
                    ),
                }
            )
            _persist_cycle_contract_verdict(cycle_run, verdict)
            return verdict
        verdict["repair_missing_outputs"] = list(repair_review["missing_outputs"])
        verdict["final_missing_outputs"] = list(repair_review["missing_outputs"])
        if not append_only and _candidate_can_be_preserved(combined_answer, repair_review):
            preserved_answer = combined_answer
            preserved_answer_source = "repair"

    missing_outputs = list(verdict.get("final_missing_outputs") or verdict["missing_outputs"])
    degraded_answer = _degraded_visible_answer(
        missing_outputs,
        provider_error=verdict.get("provider_error"),
        evidence_packet=evidence_packet,
        candidate_answer=preserved_answer,
    )
    degraded_artifact = await _append_final_answer_artifact_once(
        session,
        agent_run,
        degraded_answer,
        payload={
            "source": "cycle_result_contract_gate",
            "settlement_status": "mission_contract_failed",
            "missing_outputs": missing_outputs,
            "preserved_candidate_artifact_id": (
                getattr(artifact, "id", None)
                if preserved_answer_source == "candidate"
                else None
            ),
            "preserved_answer_source": preserved_answer_source,
        },
    )
    verdict.update(
        {
            "settlement_status": "mission_contract_failed",
            "visible_answer_source": (
                "candidate_with_degraded_explanation"
                if preserved_answer_source == "candidate"
                else (
                    "repair_with_degraded_explanation"
                    if preserved_answer_source == "repair"
                    else "degraded_explanation"
                )
            ),
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
    verdict_data = _json_dict(verdict)
    contract_failed = verdict_data.get("settlement_status") == "mission_contract_failed"
    provider_error = str(verdict_data.get("provider_error") or "").strip()
    if contract_failed and (requested_status == "completed" or provider_error):
        missing = _json_list(verdict_data.get("final_missing_outputs"))
        if provider_error:
            return "degraded", f"mission_contract_failed: upstream provider {provider_error}"
        detail = ", ".join(str(item) for item in missing[:8]) or "required Cycle outputs"
        return "degraded", f"mission_contract_failed: missing {detail}"
    if requested_status != "completed":
        return requested_status, error
    return requested_status, error
