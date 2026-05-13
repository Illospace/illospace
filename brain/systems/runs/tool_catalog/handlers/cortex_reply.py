"""Cortex Reply orchestration tool handlers."""

from __future__ import annotations

import re
import json

from brain.systems.runs.tool_catalog.handlers.common import *
from brain.systems.runs.tool_catalog.handlers.common import _agent_context


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


def _build_final_reply_check_context() -> str:
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
            evidence = worker_result.evidence if isinstance(worker_result.evidence, dict) else {}
            unresolved = evidence.get("unresolved_uncertainty") or []
            evidence_counts = {
                "files": len(evidence.get("files") or []),
                "commands": len(evidence.get("commands") or []),
                "artifacts": len(evidence.get("artifacts") or []),
                "uncertainty": len(unresolved),
            }
            summary = (worker_result.output or worker_result.error or "").strip().replace("\n", " ")
            lines.append(
                f"Worker {idx} [{worker_result.skill_name or 'unknown'} / {status}]: "
                f"trust={worker_result.trust_status or 'unknown'}; "
                f"evidence_counts={evidence_counts}; "
                f"summary={summary[:350] or '(no output)'}"
            )
            if unresolved:
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
        try:
            compact = json.dumps(
                {
                    "kind": artifact.get("kind"),
                    "summary": artifact.get("summary"),
                    "path": artifact.get("path"),
                    "status": artifact.get("status"),
                },
                default=str,
            )
        except Exception:
            compact = str(artifact)
        lines.append(f"Artifact {idx}: {compact[:350]}")

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
    if review.get("override"):
        result["checker_override"] = review["override"]
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
    execution_context = _build_final_reply_check_context()
    review = review_final_reply_once(
        user_request=user_request,
        candidate_output=proposed_reply,
        execution_context=execution_context,
        intent_profile=getattr(_agent_context, "intent_satisfaction", None),
        user_id=getattr(_agent_context, "user_id", None),
        session_id=getattr(_agent_context, "session_id", None),
    )
    if review["status"] not in {"resolved", "blocked_on_user"} and _looks_like_concrete_blocker_reply(
        proposed_reply,
        execution_context,
    ):
        review = {
            "status": "blocked_on_user",
            "approved": True,
            "rationale": (
                "The candidate reports a concrete backend/tool dependency blocker, "
                "so continuing the agent loop would not make progress."
            ),
            "missing_requirements": [],
            "raw_output": review.get("raw_output", ""),
            "override": "concrete_dependency_blocker",
        }
    if review["status"] not in {"resolved", "blocked_on_user"}:
        return {
            "blocked": True,
            "error": "Final reply checker rejected cortex_reply.",
            "checker_status": review["status"],
            "checker_reason": review.get("rationale") or "The proposed final reply does not show the work is finished.",
            "missing_requirements": review.get("missing_requirements") or [],
            "instruction": (
                "Do not send this as the final reply yet. Continue working. "
                "If more execution is required, use the normal project tools or let the AgentRun recipe escalate. "
                "Only call cortex_reply after the user goal is complete or concretely blocked by user input, "
                "credentials, an unavailable service, or a backend/tool dependency."
            ),
        }

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
        if review.get("override"):
            result["checker_override"] = review["override"]
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
