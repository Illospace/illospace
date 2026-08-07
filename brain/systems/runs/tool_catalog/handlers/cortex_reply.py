"""Cortex Reply orchestration tool handlers."""

from __future__ import annotations

import json
import re

from brain.systems.runs.tool_catalog.handlers.common import *
from brain.systems.runs.tool_catalog.handlers.common import _agent_context
from brain.systems.runs.direct_loop.final_reply_checker import (
    FinalReplyEnforcement,
)
from brain.systems.runs.outbound_reply_admission import (
    admit_outbound_reply,
)


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


def _handle_cortex_reply(
    content: str,
    coordination: dict | None = None,
) -> dict:
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

    admission = admit_outbound_reply(
        agent_context=_agent_context,
        content=proposed_reply,
        coordination=coordination,
        review_completion=True,
    )
    if not admission.admitted:
        return dict(admission.blocked_result or {})
    review = admission.review

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
        from brain.platform.events import publish_safe as _ws_publish
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
