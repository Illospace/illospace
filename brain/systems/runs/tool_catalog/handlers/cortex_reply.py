"""Cortex Reply orchestration tool handlers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from brain.systems.runs.tool_catalog.handlers.common import *
from brain.systems.runs.tool_catalog.handlers.common import _agent_context
from brain.systems.runs.direct_loop.final_reply_checker import (
    FinalReplyEnforcement,
)
from brain.systems.runs.direct_loop.final_reply_evidence import (
    FinalReplyEvidence,
    ToolResultEvidence,
)
from brain.systems.runs.failures import failure_category_for_error, public_run_failure


_MAX_ARTIFACT_CONTRACT_BLOCKS = 2


def _current_effective_model() -> str | None:
    """Read the model from the run's canonical live routing metadata."""

    execution_metadata = _agent_context.execution_metadata
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


def _safe_failure_message(diagnostic: object) -> str:
    category = failure_category_for_error(diagnostic)
    failure = public_run_failure("failed", category)
    return str((failure or {}).get("message") or "")


def _normalize_reply_whitespace(content: str) -> str:
    """Clean common LLM markdown spacing artifacts without touching code fences."""

    parts = str(content or "").split("```")
    normalized_parts: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            normalized_parts.append(part)
            continue
        text = part.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n+\s*([,.;:])\s*\n+", r"\1 ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        normalized_parts.append(text)
    return "```".join(normalized_parts).strip()


def _build_final_reply_check_context(
    evidence: FinalReplyEvidence | None = None,
) -> str:
    """Summarize recent execution evidence for the final-reply checker."""
    run = getattr(_agent_context, "run", None)
    execution_artifacts = list(getattr(_agent_context, "execution_artifacts", []) or [])
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
            worker_evidence = worker_result.evidence if isinstance(worker_result.evidence, dict) else {}
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
                diagnostic = getattr(worker_result, "error", None) or getattr(worker_result, "output", None)
                summary = _safe_failure_message(diagnostic)
            lines.append(
                f"Worker {idx} [{worker_result.skill_name or 'unknown'} / {status}]: "
                f"trust={worker_result.trust_status or 'unknown'}; "
                f"evidence_counts={evidence_counts}; "
                f"summary={summary[:350] or '(no output)'}"
            )
            if unresolved and worker_result.success:
                lines.append(f"Worker {idx} unresolved: {str(unresolved[:3])[:350]}")

    intent_profile = getattr(_agent_context, "intent_satisfaction", None)
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
        else (getattr(_agent_context, "recent_tool_results", []) or [])
    )
    for idx, tool_result in enumerate(recent_tool_results[-5:], 1):
        if isinstance(tool_result, ToolResultEvidence):
            try:
                args_preview = json.dumps(tool_result.arguments, sort_keys=True, default=str)
            except Exception:
                args_preview = str(tool_result.arguments)
            try:
                result_preview = json.dumps(tool_result.result, sort_keys=True, default=str)
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


def _stage_cortex_reply(content: str, *, run_id: int | None, review: dict) -> dict:
    """Stage a run reply for lifecycle settlement to publish."""
    if not hasattr(_agent_context, "reply_contents"):
        _agent_context.reply_contents = []
    _agent_context.reply_contents.append(content)
    reply_count = len(_agent_context.reply_contents)

    result: dict = {
        "staged": True,
        "posted": False,
        "reply_number": reply_count,
        "checker_status": review["status"],
        "instruction": (
            "Final reply accepted and staged. Do not call cortex_reply again unless you need "
            "to replace this answer; end your turn so run settlement can verify and publish it."
        ),
    }
    if run_id:
        result["run_id"] = run_id
    checker_note = review.get("rationale")
    if checker_note:
        result["checker_note"] = checker_note
    result["checker_enforcement"] = str(
        review.get("enforcement", FinalReplyEnforcement.ADVISORY).value
        if isinstance(review.get("enforcement"), FinalReplyEnforcement)
        else review.get("enforcement", FinalReplyEnforcement.ADVISORY.value)
    )
    if reply_count > 1:
        result["note"] = (
            f"This is staged reply #{reply_count} in this run. "
            "Only the latest staged reply will be published after settlement."
        )
    return result


def _handle_cortex_reply(content: str) -> dict:
    """Accept a final Cortex reply.

    Run-scoped replies are staged, not posted immediately. The run
    lifecycle is the settlement boundary that verifies the answer and publishes
    it exactly once.
    """
    idea_id = getattr(_agent_context, "idea_id", None)
    if not idea_id:
        return {"error": "No idea_id in context — cortex_reply only works during cortex runs"}

    proposed_reply = _normalize_reply_whitespace(str(content or ""))
    if not proposed_reply:
        return {
            "blocked": True,
            "error": "Final reply checker rejected an empty cortex_reply.",
            "checker_status": "continue",
            "checker_reason": "No final user-facing result was provided.",
            "instruction": (
                "Do not end the run with an empty final reply. "
                "Continue the work, or explain the concrete blocker if specific user input is required."
            ),
        }

    from brain.systems.runs.direct_agent import review_final_reply_once

    user_request = (
        getattr(_agent_context, "user_request", None)
        or getattr(getattr(_agent_context, "run", None), "user_task", None)
        or ""
    )
    evidence = FinalReplyEvidence.from_agent_context(_agent_context)
    execution_context = _build_final_reply_check_context(evidence)
    resolved_llm_context = _agent_context.resolved_llm_context
    review = review_final_reply_once(
        user_request=user_request,
        candidate_output=proposed_reply,
        execution_context=execution_context,
        evidence=evidence,
        intent_profile=getattr(_agent_context, "intent_satisfaction", None),
        user_id=getattr(_agent_context, "user_id", None),
        provider=(resolved_llm_context.provider if resolved_llm_context else None),
        llm=(resolved_llm_context.llm if resolved_llm_context else None),
        model=_current_effective_model(),
        session_id=getattr(_agent_context, "session_id", None),
    )
    try:
        enforcement = FinalReplyEnforcement(
            review.get("enforcement", FinalReplyEnforcement.ADVISORY)
        )
    except (TypeError, ValueError):
        enforcement = FinalReplyEnforcement.ADVISORY
    if enforcement is FinalReplyEnforcement.BLOCK:
        block_count = int(getattr(_agent_context, "artifact_contract_block_count", 0) or 0)
        if block_count < _MAX_ARTIFACT_CONTRACT_BLOCKS:
            block_count += 1
            _agent_context.artifact_contract_block_count = block_count
            return {
                "blocked": True,
                "error": "Final reply violates the requested-artifact contract.",
                "checker_status": review["status"],
                "checker_reason": review.get("rationale"),
                "missing_requirements": review.get("missing_requirements") or [],
                "artifact_contract_block_count": block_count,
                "instruction": (
                    "Continue the required artifact work, or replace the reply with the requested "
                    "artifact name and its concrete blocker. Do not report a substitute artifact as success."
                ),
            }

        review = dict(review)
        review["enforcement"] = FinalReplyEnforcement.ADVISORY
        review["rationale"] = (
            f"{str(review.get('rationale') or '').strip()} "
            "The artifact-contract check already blocked two replies in this run, so this verdict "
            "is now advisory and cannot keep the run alive."
        ).strip()

    # Other checker verdicts are advisory: surface them as a non-blocking warning
    # so the model can decide whether to continue or reply again.

    run_id = getattr(getattr(_agent_context, "run", None), "run_id", None)
    if run_id:
        return _stage_cortex_reply(proposed_reply, run_id=run_id, review=review)

    try:
        from brain.systems.cortex.reply import reply_to_cortex
        metadata = {"run_id": run_id} if run_id else None
        reply_to_cortex(idea_id, proposed_reply, metadata=metadata)
        if not hasattr(_agent_context, "reply_contents"):
            _agent_context.reply_contents = []
        _agent_context.reply_contents.append(proposed_reply)
        reply_count = len(_agent_context.reply_contents)
        result: dict = {"posted": True, "idea_id": idea_id, "reply_number": reply_count}
        if run_id:
            result["run_id"] = run_id
        result["checker_status"] = review["status"]
        checker_note = review.get("rationale")
        if checker_note:
            result["checker_note"] = checker_note
        enforcement = review.get("enforcement", FinalReplyEnforcement.ADVISORY)
        result["checker_enforcement"] = (
            enforcement.value if isinstance(enforcement, FinalReplyEnforcement) else str(enforcement)
        )
        if reply_count > 1:
            result["note"] = (
                f"This is reply #{reply_count} in this run. "
                "Consider using my_activity to check if you're making forward progress."
            )
        return result
    except Exception as e:
        logger.warning(f"cortex_reply failed: {e}")
        return {"error": str(e)}


async def _handle_cortex_visual_reply(content_type: str, title: str, content: str, display: str = "inline") -> dict:
    """Persist and broadcast a visual content block in the Cortex workspace."""
    idea_id = getattr(_agent_context, "idea_id", None)
    if not idea_id:
        return {"error": "No idea_id in context — cortex_visual_reply only works during cortex runs"}

    # Content size limit (500KB)
    max_size = 500_000
    if len(content) > max_size:
        content = content[:max_size] + "\n\n[truncated — content exceeded 500KB limit]"

    display_mode = display or "inline"

    try:
        from brain.systems.cortex.events import publish_safe as _ws_publish
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.db.models.idea import VisualBlock
        from sqlalchemy import text as sa_text

        run = getattr(_agent_context, "run", None)
        d_id = run.run_id if run else None

        # Find the latest thread message for this idea to set position_after
        async with UnitOfWork() as uow:
            last_msg = (await uow.session.execute(sa_text(
                "SELECT id FROM idea_threads WHERE idea_id = :idea_id "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"idea_id": idea_id})).scalar()

            block = VisualBlock(
                idea_id=idea_id,
                content_type=content_type,
                title=title,
                content=content,
                display_mode=display_mode,
                position_after=last_msg,
                run_id=d_id,
            )
            uow.session.add(block)
            await uow.session.flush()
            block_id = block.id
            created_at = block.created_at

        # Broadcast via WebSocket
        _ws_publish("visual_reply", {
            "idea_id": idea_id,
            "block": {
                "id": block_id,
                "idea_id": idea_id,
                "run_id": d_id,
                "content_type": content_type,
                "title": title,
                "content": content,
                "display_mode": display_mode,
                "position_after": last_msg,
                "created_at": created_at.isoformat() if created_at else None,
            },
        })

        return {"posted": True, "block_id": block_id, "content_type": content_type, "display_mode": display_mode}
    except Exception as e:
        logger.warning(f"cortex_visual_reply failed: {e}")
        return {"error": str(e)}

__all__ = [name for name in globals() if not name.startswith("__")]
