"""Automatic post-mortem analysis for failed runs.

Runs on every failure (fire-and-forget). Gathers telemetry data,
classifies the failure, extracts a lesson via Ollama (free), and
routes the lesson to the appropriate corrective system.
"""

import json
import logging

from sqlalchemy import select
from sqlalchemy import text

logger = logging.getLogger(__name__)


async def run_postmortem(run_id: int, error: str, skill_name: str | None = None):
    """Run post-mortem analysis for a failed run."""
    await _postmortem_worker(run_id, error, skill_name)


async def _postmortem_worker(run_id: int, error: str, skill_name: str | None):
    """Gather data, analyze, store results, route corrections."""
    try:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.telemetry.classify import classify_error

        classification = classify_error(error)

        # Look up skill_name from run if not provided
        if not skill_name:
            try:
                async with UnitOfWork() as uow:
                    result = await uow.session.execute(text(
                        "SELECT skill_used FROM agent_runs WHERE id = :id"
                    ), {"id": run_id})
                    row = result.mappings().first()
                    if row:
                        skill_name = row.get("skill_used") or row.get("skill_name")
            except Exception:
                pass

        # Gather per-API-call telemetry (context growth)
        context_growth = []
        tool_trace = []
        try:
            async with UnitOfWork() as uow:
                from brain.platform.db.models.agent_run import AgentRunEventRow

                result = await uow.session.execute(text(
                    "SELECT turn_number, tokens_input, context_messages, latency_ms, status, error "
                    "FROM agent_api_calls WHERE run_id = :id ORDER BY turn_number"
                ), {"id": run_id})
                context_growth = [dict(r) for r in result.mappings().all()]

                events_result = await uow.session.scalars(
                    select(AgentRunEventRow)
                    .where(
                        AgentRunEventRow.run_id == run_id,
                        AgentRunEventRow.event_type.in_(
                            ["run.tool_started", "run.tool_completed", "run.tool_failed"]
                        ),
                    )
                    .order_by(AgentRunEventRow.created_at.asc(), AgentRunEventRow.id.asc())
                )
                events = events_result.all()
                for event in events:
                    payload = dict(event.payload or {})
                    tool_trace.append(
                        {
                            "tool_name": payload.get("tool_name") or payload.get("tool") or "unknown",
                            "args_snippet": str(payload.get("args") or payload.get("input") or "")[:500],
                            "result_snippet": str(payload.get("result") or payload.get("error") or "")[:500],
                            "called_at": event.created_at,
                        }
                    )
        except Exception:
            pass  # Tables may not exist yet

        # Extract lesson via Ollama (free, local)
        lesson = _extract_lesson(error, classification, context_growth, tool_trace)

        # Build post-mortem result
        result = {
            "classification": classification,
            "context_growth": context_growth[-10:] if context_growth else [],
            "tool_trace_count": len(tool_trace),
            "lesson": lesson,
            "corrective_action": classification["corrective_system"],
        }

        # Store on run row
        async with UnitOfWork() as uow:
            await uow.session.execute(text(
                "UPDATE agent_runs SET postmortem = :postmortem WHERE id = :id"
            ), {"postmortem": json.dumps(result, default=str), "id": run_id})

        # Route to corrective system
        _route_correction(classification, lesson, skill_name)

        logger.info(
            "Post-mortem for run #%d: %s → %s",
            run_id, classification["category"], classification["corrective_system"],
        )

    except Exception as exc:
        logger.debug("Post-mortem failed for run #%d: %s", run_id, exc)


def _extract_lesson(
    error: str,
    classification: dict,
    context_growth: list,
    tool_trace: list,
) -> str:
    """Extract a structured lesson from the failure. Uses Ollama if available."""
    # Skip lesson extraction for transient API errors — nothing to learn
    if classification["category"] == "transient_api":
        return "Transient API error — no corrective action needed."

    # Try Ollama for richer analysis
    try:
        return _extract_with_gpu_server(error, classification, context_growth, tool_trace)
    except Exception:
        pass

    # Fallback: simple template-based lesson
    cat = classification["category"]
    summary = classification["summary"]

    if cat == "context_overflow":
        last_ctx = context_growth[-1]["context_messages"] if context_growth else "unknown"
        return f"Context grew to {last_ctx} messages before overflow. Consider trimming session history earlier."
    elif cat == "tool_failure":
        last_tool = tool_trace[-1]["tool_name"] if tool_trace else "unknown"
        return f"Tool '{last_tool}' failed: {summary}. Add this as a skill pitfall."
    elif cat == "stuck_loop":
        return f"Agent stuck in loop: {summary}. Add anti-heuristic to prevent this pattern."
    elif cat == "timeout":
        return f"Execution timed out: {summary}. Consider breaking into smaller subtasks."
    else:
        return f"Failure: {summary}"


def _extract_with_gpu_server(
    error: str,
    classification: dict,
    context_growth: list,
    tool_trace: list,
) -> str:
    """Use unified GPU server for free lesson extraction."""
    # Build compact context for the LLM
    growth_summary = ""
    if context_growth:
        first = context_growth[0]
        last = context_growth[-1]
        growth_summary = (
            f"Context grew from {first.get('context_messages', '?')} to "
            f"{last.get('context_messages', '?')} messages over {len(context_growth)} API calls. "
            f"Last call latency: {last.get('latency_ms', '?')}ms."
        )

    tools_used = ", ".join(t["tool_name"] for t in tool_trace[-5:]) if tool_trace else "none"

    prompt = (
        f"A run failed with category '{classification['category']}'.\n"
        f"Error: {error[:500]}\n"
        f"{growth_summary}\n"
        f"Last tools used: {tools_used}\n\n"
        "In one sentence, what should the system learn from this failure to prevent it next time? "
        "Be specific and actionable. Format: 'When [condition], [what to do instead].'"
    )

    from brain.platform.gpu_client import get_client
    result = get_client().generate(
        prompt=prompt, max_tokens=100,
        temperature=0.2, think=False, fallback_policy="auto",
    )

    content = (result or "").strip()
    # Strip <think> tags if present (qwen3.5 sometimes wraps reasoning)
    if "<think>" in content:
        import re
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content or "No lesson extracted."


def _route_correction(classification: dict, lesson: str, skill_name: str | None):
    """Route the post-mortem lesson to the appropriate corrective system."""
    system = classification["corrective_system"]

    if system == "skill_pitfalls" and skill_name and lesson:
        try:
            from brain.systems.feedback.heuristics import store_heuristics
            store_heuristics(skill_name, [{
                "condition": f"when {classification['summary'][:100]}",
                "action": lesson[:200],
            }])
            logger.info("Auto-generated guardrail for skill '%s'", skill_name)
        except Exception as exc:
            logger.debug("Failed to store guardrail: %s", exc)

    elif system == "guardrails" and skill_name and lesson:
        try:
            from brain.systems.feedback.heuristics import store_heuristics
            store_heuristics(skill_name, [{
                "condition": f"when {classification['summary'][:100]}",
                "action": f"AVOID: {lesson[:200]}",
            }])
            logger.info("Auto-generated anti-heuristic for skill '%s'", skill_name)
        except Exception as exc:
            logger.debug("Failed to store anti-heuristic: %s", exc)

    # Other corrective systems (retry_config, session_trimming) are logged
    # but don't auto-generate corrections — they inform nightly reflection.
