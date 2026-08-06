"""Shared admission policy for user-visible outbound replies."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from brain.systems.runs.direct_loop.final_reply_checker import (
    FinalReplyEnforcement,
    active_sibling_contract_issue,
    review_final_reply_once,
)
from brain.systems.runs.direct_loop.final_reply_evidence import (
    FinalReplyEvidence,
    ToolResultEvidence,
)
from brain.systems.runs.direct_loop.reply_coordination import ReplyCoordination
from brain.systems.runs.execution_context import get_or_create_agent_run_state
from brain.systems.runs.failures import failure_category_for_error, public_run_failure


MAX_OUTBOUND_REPLY_BLOCKS = 2
REPLY_ADMISSION_BLOCK_COUNT_METADATA_KEY = "reply_admission_block_count"


@dataclass(frozen=True)
class OutboundReplyAdmission:
    admitted: bool
    review: dict[str, Any]
    blocked_result: dict[str, Any] | None = None


def _safe_failure_message(diagnostic: object) -> str:
    category = failure_category_for_error(diagnostic)
    failure = public_run_failure("failed", category)
    return str((failure or {}).get("message") or "")


def _current_effective_model(agent_context: Any) -> str | None:
    """Read the model from the run's canonical live routing metadata."""

    execution_metadata = getattr(agent_context, "execution_metadata", None)
    if not isinstance(execution_metadata, Mapping):
        return None
    routing = execution_metadata.get("routing")
    if not isinstance(routing, Mapping):
        return None
    effective = routing.get("effective")
    if not isinstance(effective, Mapping):
        return None
    model = effective.get("model")
    return str(model) if model else None


def build_final_reply_check_context(
    agent_context: Any,
    evidence: FinalReplyEvidence | None = None,
) -> str:
    """Summarize recent execution evidence for the final-reply checker."""

    run = getattr(agent_context, "run", None)
    execution_artifacts = list(getattr(agent_context, "execution_artifacts", []) or [])
    lines: list[str] = [
        "Evidence guardrail: approve direct source/runtime/commit claims only when raw "
        "command output or file artifacts support them. Worker-summary-level evidence "
        "must be attributed as worker-reported, with uncertainty preserved."
    ]

    if run:
        worker_results = list(getattr(run, "worker_results", []) or [])
        lines.append(
            "Run summary: "
            f"workers={len(worker_results)}, "
            f"tokens={int(getattr(run, 'total_tokens', 0) or 0)}"
        )
        for idx, worker_result in enumerate(worker_results[-4:], 1):
            status = "success" if worker_result.success else "failed"
            worker_evidence = (
                worker_result.evidence
                if isinstance(worker_result.evidence, dict)
                else {}
            )
            unresolved = worker_evidence.get("unresolved_uncertainty") or []
            evidence_counts = {
                "files": len(worker_evidence.get("files") or []),
                "commands": len(worker_evidence.get("commands") or []),
                "artifacts": len(worker_evidence.get("artifacts") or []),
                "uncertainty": len(unresolved),
            }
            if worker_result.success:
                summary = str(worker_result.output or "").strip().replace("\n", " ")
            else:
                diagnostic = getattr(worker_result, "error", None) or getattr(
                    worker_result,
                    "output",
                    None,
                )
                summary = _safe_failure_message(diagnostic)
            lines.append(
                f"Worker {idx} [{worker_result.skill_name or 'unknown'} / {status}]: "
                f"trust={worker_result.trust_status or 'unknown'}; "
                f"evidence_counts={evidence_counts}; "
                f"summary={summary[:350] or '(no output)'}"
            )
            if unresolved and worker_result.success:
                lines.append(f"Worker {idx} unresolved: {str(unresolved[:3])[:350]}")

    intent_profile = getattr(agent_context, "intent_satisfaction", None)
    if isinstance(intent_profile, dict) and intent_profile:
        try:
            lines.append(
                "Intent satisfaction profile: "
                + json.dumps(
                    {
                        "intent_type": intent_profile.get("intent_type"),
                        "completion_mode": intent_profile.get("completion_mode"),
                        "completion_contract": intent_profile.get("completion_contract") or [],
                        "continuation_policy": intent_profile.get("continuation_policy"),
                    },
                    sort_keys=True,
                    default=str,
                )[:900]
            )
        except Exception:
            lines.append(f"Intent satisfaction profile: {str(intent_profile)[:900]}")

    for idx, artifact in enumerate(execution_artifacts[-3:], 1):
        if not isinstance(artifact, dict):
            lines.append(f"Artifact {idx}: {str(artifact)[:350]}")
            continue
        artifact_status = str(artifact.get("status") or "").strip().lower()
        artifact_summary = artifact.get("summary")
        if artifact_status in {"error", "failed", "failure"}:
            artifact_summary = _safe_failure_message(artifact_summary)
        try:
            compact = json.dumps(
                {
                    "kind": artifact.get("kind"),
                    "summary": artifact_summary,
                    "path": artifact.get("path"),
                    "status": artifact.get("status"),
                },
                default=str,
            )
        except Exception:
            compact = str(artifact)
        lines.append(f"Artifact {idx}: {compact[:350]}")

    recent_tool_results = list(
        evidence.tool_results
        if evidence is not None
        else (getattr(agent_context, "recent_tool_results", []) or [])
    )
    for idx, tool_result in enumerate(recent_tool_results[-5:], 1):
        if isinstance(tool_result, ToolResultEvidence):
            try:
                args_preview = json.dumps(
                    tool_result.arguments,
                    sort_keys=True,
                    default=str,
                )
            except Exception:
                args_preview = str(tool_result.arguments)
            try:
                result_preview = json.dumps(
                    tool_result.result,
                    sort_keys=True,
                    default=str,
                )
            except Exception:
                result_preview = str(tool_result.result)
            is_error = tool_result.failed
        elif isinstance(tool_result, dict):
            args_preview = tool_result.get("args_preview")
            result_preview = tool_result.get("result_preview")
            is_error = bool(tool_result.get("is_error"))
        else:
            continue
        if is_error:
            result_preview = _safe_failure_message(result_preview)
        try:
            compact = json.dumps(
                {
                    "tool_name": (
                        tool_result.tool_name
                        if isinstance(tool_result, ToolResultEvidence)
                        else tool_result.get("tool_name")
                    ),
                    "args_preview": str(args_preview or "")[:400],
                    "is_error": is_error,
                    "result_preview": result_preview,
                },
                default=str,
                sort_keys=True,
            )
        except Exception:
            compact = str(tool_result)
        lines.append(f"Recent tool result {idx}: {compact[:650]}")

    return "\n".join(lines)[:2500]


def _coordination_review(
    coordination: ReplyCoordination | dict[str, Any] | None,
    evidence: FinalReplyEvidence,
) -> dict[str, Any]:
    issue = active_sibling_contract_issue(coordination, evidence)
    if issue is None:
        return {
            "status": "resolved",
            "approved": True,
            "rationale": "Outbound reply coordination is admitted.",
            "missing_requirements": [],
            "raw_output": "deterministic_active_sibling_contract",
            "enforcement": FinalReplyEnforcement.ADVISORY,
        }
    return {
        "status": "continue",
        "approved": False,
        "rationale": issue,
        "missing_requirements": [
            "Declare coordination as wait, reference an active run id, or handoff."
        ],
        "raw_output": "deterministic_active_sibling_contract",
        "enforcement": FinalReplyEnforcement.BLOCK,
    }


def _enforcement(review: Mapping[str, Any]) -> FinalReplyEnforcement:
    try:
        return FinalReplyEnforcement(
            review.get("enforcement", FinalReplyEnforcement.ADVISORY)
        )
    except (TypeError, ValueError):
        return FinalReplyEnforcement.ADVISORY


def _block_state(agent_context: Any) -> dict[str, int]:
    return get_or_create_agent_run_state(
        "outbound_reply_admission",
        lambda: {
            "blocked_attempts": int(
                getattr(agent_context, "reply_admission_block_count", 0) or 0
            )
        },
    )


def _bounded_admission(
    review: dict[str, Any],
    *,
    agent_context: Any,
) -> OutboundReplyAdmission:
    if _enforcement(review) is not FinalReplyEnforcement.BLOCK:
        return OutboundReplyAdmission(admitted=True, review=review)

    state = _block_state(agent_context)
    blocked_attempts = int(state.get("blocked_attempts", 0) or 0)
    if blocked_attempts < MAX_OUTBOUND_REPLY_BLOCKS:
        blocked_attempts += 1
        state["blocked_attempts"] = blocked_attempts
        agent_context.reply_admission_block_count = blocked_attempts
        execution_metadata = getattr(agent_context, "execution_metadata", None)
        if isinstance(execution_metadata, dict):
            execution_metadata[REPLY_ADMISSION_BLOCK_COUNT_METADATA_KEY] = blocked_attempts
            agent_context.execution_metadata = execution_metadata
        return OutboundReplyAdmission(
            admitted=False,
            review=review,
            blocked_result={
                "ok": False,
                "posted": False,
                "blocked": True,
                "error": "outbound_reply_admission_blocked",
                "checker_status": review.get("status", "continue"),
                "checker_reason": review.get("rationale"),
                "missing_requirements": review.get("missing_requirements") or [],
                "reply_admission_block_count": blocked_attempts,
                "instruction": (
                    "Revise the reply and its typed coordination declaration. "
                    "The gate degrades after two blocked attempts."
                ),
            },
        )

    downgraded = dict(review)
    downgraded["enforcement"] = FinalReplyEnforcement.ADVISORY
    downgraded["rationale"] = (
        f"{str(review.get('rationale') or '').strip()} "
        "Outbound reply admission already blocked two replies in this run, so this "
        "verdict is now advisory and cannot prevent a human-visible reply."
    ).strip()
    return OutboundReplyAdmission(admitted=True, review=downgraded)


def admit_outbound_reply(
    *,
    agent_context: Any,
    content: str,
    coordination: ReplyCoordination | dict[str, Any] | None,
    review_completion: bool,
) -> OutboundReplyAdmission:
    """Admit one outbound reply before its handler crosses the post boundary."""

    evidence = FinalReplyEvidence.from_agent_context(agent_context)
    declared_coordination = ReplyCoordination.from_value(coordination)
    if review_completion:
        resolved_llm_context = getattr(agent_context, "resolved_llm_context", None)
        review = review_final_reply_once(
            user_request=(
                getattr(agent_context, "user_request", None)
                or getattr(getattr(agent_context, "run", None), "user_task", None)
                or ""
            ),
            candidate_output=str(content or ""),
            execution_context=build_final_reply_check_context(agent_context, evidence),
            evidence=evidence,
            coordination=declared_coordination,
            intent_profile=getattr(agent_context, "intent_satisfaction", None),
            user_id=getattr(agent_context, "user_id", None),
            provider=(
                resolved_llm_context.provider if resolved_llm_context else None
            ),
            llm=(resolved_llm_context.llm if resolved_llm_context else None),
            model=_current_effective_model(agent_context),
            session_id=getattr(agent_context, "session_id", None),
            agent_context=agent_context,
        )
    else:
        review = _coordination_review(declared_coordination, evidence)
    return _bounded_admission(review, agent_context=agent_context)


__all__ = [
    "MAX_OUTBOUND_REPLY_BLOCKS",
    "OutboundReplyAdmission",
    "REPLY_ADMISSION_BLOCK_COUNT_METADATA_KEY",
    "admit_outbound_reply",
    "build_final_reply_check_context",
]
