"""
Cognitive Frames — Compressed context for token-efficient agent runs.

Instead of loading 5-10K tokens of system prompt + memories + skill procedure +
thread history into every run, we compress everything into a ~300-500 token
"cognitive frame" that captures the essence of what the agent needs to know.

The agent can pull additional context on-demand via brain tools if it hits
uncertainty — but most runs complete with just the frame.

Uses GPU server (local, free) for compression. Falls back to heuristic truncation.

Token savings: 60-80% reduction in per-run input tokens.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

from brain.systems.memory.attention_controller import AttentionController, observe_retrieval

logger = logging.getLogger("cognition.frame")


def _lazy_load_enabled() -> bool:
    return os.getenv("ATTENTION_LAZY_LOAD_ENABLED", "0").strip().lower() not in {"0", "false", "no"}


@dataclass
class CognitiveFrame:
    """Compressed context for an agent run."""
    task_essence: str          # core task in 1-2 sentences
    context: str               # compressed relevant context
    heuristics: list[str]      # applicable "when X, do Y" patterns
    pitfalls: list[str]        # specific things to avoid
    confidence: float          # system confidence (0-1) for this task
    total_tokens_est: int = 0  # estimated frame size in tokens

    def to_system_prompt(self) -> str:
        """Render the frame as a minimal system prompt."""
        parts = []

        if self.context:
            parts.append(self.context)

        if self.heuristics:
            parts.append("\n## Learned Patterns")
            for h in self.heuristics:
                parts.append(f"- {h}")

        if self.pitfalls:
            parts.append("\n## Pitfalls")
            for p in self.pitfalls:
                parts.append(f"- {p}")

        return "\n".join(parts)

    def to_user_message(self) -> str:
        """Render the task message with frame context."""
        return self.task_essence


def build_frame(
    task: str,
    skill: dict | None = None,
    thread_context: str | None = None,
    relevant_memories: list[dict] | None = None,
    heuristics: list[dict] | None = None,
    guardrails: list[str] | None = None,
    budget_tokens: int = 500,
    brain_available: bool = True,
) -> CognitiveFrame:
    """Build a cognitive frame by compressing all available context.

    Args:
        task: The task description
        skill: Skill dict (name, procedure, pitfalls, maturity, confidence)
        thread_context: Prior conversation summary
        relevant_memories: Pre-fetched brain memories
        heuristics: Pre-fetched skill heuristics
        guardrails: Pre-fetched guardrails (recent failures, warnings, pitfalls)
        budget_tokens: Target frame size in tokens

    Returns:
        CognitiveFrame ready to inject into agent run.
    """
    # ── Gather raw context ──
    raw_parts = []

    if skill:
        raw_parts.append(f"## Skill: {skill.get('name', 'unknown')} ({skill.get('maturity', 'emerging')})")
        procedure = skill.get("procedure", "")
        if procedure:
            raw_parts.append(f"### Procedure\n{procedure}")

    if thread_context:
        raw_parts.append(f"## Prior Context\n{thread_context}")

    if relevant_memories:
        raw_parts.append("## Relevant Experience")
        # Prefer semantic/procedural memories over raw episodes
        tiered = sorted(
            relevant_memories,
            key=lambda m: {"procedural": 0, "semantic": 1, "episodic": 2}.get(
                m.get("tier", m.get("memory_tier", "episodic")), 2
            ),
        )
        for mem in tiered[:5]:
            tier = mem.get("tier", mem.get("memory_tier", ""))
            tier_tag = f"/{tier}" if tier and tier != "episodic" else ""
            content = mem.get("content", "")[:200]
            raw_parts.append(f"- [{mem.get('type', 'memory')}{tier_tag}] {content}")

    raw_context = "\n".join(raw_parts)

    # ── Extract heuristics ──
    heuristic_texts = []
    if heuristics:
        for h in sorted(heuristics, key=lambda x: x.get("confidence", 0), reverse=True)[:5]:
            heuristic_texts.append(f"{h['condition']} → {h['action']}")

    # ── Extract pitfalls ──
    pitfall_texts = []
    if skill and skill.get("pitfalls"):
        pitfalls = skill["pitfalls"]
        if isinstance(pitfalls, str):
            try:
                pitfalls = json.loads(pitfalls)
            except (json.JSONDecodeError, TypeError):
                pitfalls = []
        for p in pitfalls[-5:]:
            if isinstance(p, dict):
                pitfall_texts.append(f"[{p.get('severity', 'medium')}] {p.get('text', '')[:100]}")
            elif isinstance(p, str):
                pitfall_texts.append(p[:100])

    # ── Compress context ──
    if raw_context and len(raw_context) > budget_tokens * 4:  # rough char-to-token ratio
        compressed = _compress_with_gpu_server(task, raw_context, budget_tokens)
        if not compressed:
            compressed = _heuristic_compress(raw_context, budget_tokens)
    else:
        compressed = raw_context

    # ── Inject pre-fetched guardrails (deterministic, not optional) ──
    if guardrails:
        for g in guardrails[:5]:
            pitfall_texts.append(g)

    # ── Brain unavailability warning ──
    if not brain_available:
        pitfall_texts.insert(0, "[critical] Brain context unavailable — use brain_recall tool to verify memories before acting")
        logger.warning("Building frame WITHOUT brain context — confidence reduced")

    # ── Compute confidence ──
    confidence = _compute_confidence(skill, heuristics, relevant_memories)
    if not brain_available:
        confidence = max(0.1, confidence - 0.3)  # significant penalty

    # Estimate tokens (rough: 1 token ≈ 4 chars)
    frame_text = compressed + "\n".join(heuristic_texts) + "\n".join(pitfall_texts)
    tokens_est = len(frame_text) // 4

    return CognitiveFrame(
        task_essence=task,
        context=compressed,
        heuristics=heuristic_texts,
        pitfalls=pitfall_texts,
        confidence=confidence,
        total_tokens_est=tokens_est,
    )


async def gather_frame_context(
    task: str,
    skill_name: str | None = None,
    idea_id: str | None = None,
    memory_limit: int = 3,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Gather all raw context needed to build a frame.

    This is the expensive part — DB queries, vector search, etc.
    Done once, then compressed into the frame.

    Returns dict with: skill, thread_context, memories, heuristics
    """
    result = {
        "skill": None,
        "thread_context": None,
        "memories": [],
        "candidate_memories": [],
        "suppressed_memories": [],
        "lazy_load_memories": [],
        "heuristics": [],
        "guardrails": [],        # pre-fetched guardrails (deterministic, not optional)
        "brain_available": True,  # tracks whether brain context was accessible
        "memory_status": "unchecked",
    }

    # ── Load skill ──
    if skill_name:
        try:
            from sqlalchemy import text
            from brain.platform.db.repositories.unit_of_work import UnitOfWork
            async with UnitOfWork() as uow:
                row = (await uow.session.execute(text("""
                    SELECT name, procedure, pitfalls, maturity, confidence,
                           model_tier, thinking_tier, fitness_score,
                           success_count, failure_count, use_count,
                           graduated_steps
                    FROM skills WHERE name = :name AND NOT archived
                """), {"name": skill_name})).mappings().first()
                if row:
                    result["skill"] = dict(row)
                    use = row["use_count"] or 0
                    succ = row["success_count"] or 0
                    result["skill"]["success_rate"] = succ / max(use, 1)
        except Exception as e:
            logger.debug(f"Skill load failed: {e}")

    # ── Load heuristics ──
    if skill_name:
        try:
            from sqlalchemy import text
            from brain.platform.db.repositories.unit_of_work import UnitOfWork
            async with UnitOfWork() as uow:
                result["heuristics"] = [dict(r) for r in (await uow.session.execute(text("""
                    SELECT condition, action, confidence
                    FROM skill_heuristics
                    WHERE skill_name = :name AND active AND confidence >= 0.6
                      AND (graduated = FALSE OR graduated IS NULL)
                    ORDER BY confidence DESC LIMIT 5
                """), {"name": skill_name})).mappings().all()]
        except Exception as e:
            logger.debug(f"Heuristic load failed: {e}")

    # ── Recall relevant memories ──
    try:
        from brain.app.mcp.server import async_tool_brain_recall
        memories = await async_tool_brain_recall(
            query=task,
            limit=memory_limit,
            user_id=user_id,
            org_id=org_id,
            expand_lazy_load=_lazy_load_enabled(),
        )
        if isinstance(memories, dict) and "memories" in memories:
            result["memories"] = memories["memories"]
            result["candidate_memories"] = memories.get("candidate_memories", [])
            result["suppressed_memories"] = memories.get("suppressed_memories", [])
            result["lazy_load_memories"] = memories.get("lazy_load_memories", [])
            result["attention_decision"] = memories.get("attention_decision")
            result["attention_explain"] = memories.get("attention_explain")
        elif isinstance(memories, list):
            result["memories"] = memories
        result["memory_status"] = "found" if result["memories"] else "empty"
    except Exception as e:
        logger.warning(f"Brain recall unavailable (embed server down?): {e}")
        result["brain_available"] = False
        result["memory_status"] = "unavailable"

    # ── Pre-fetch guardrails (deterministic — NOT optional) ──
    try:
        from brain.app.mcp.server import async_tool_brain_guardrails
        guardrails = await async_tool_brain_guardrails(skill=skill_name)
        if isinstance(guardrails, dict):
            # Collect all guardrail items into a flat list
            items = []
            for g in guardrails.get("guardrails", []):
                items.append(f"[failure] {g.get('skill', '')}: {g.get('failure', '')[:150]}")
            for w in guardrails.get("warnings", []):
                if isinstance(w, str):
                    items.append(f"[warning] {w[:150]}")
                elif isinstance(w, dict):
                    items.append(f"[warning] {str(w.get('content', ''))[:150]}")
            for p in guardrails.get("pitfalls", []):
                items.append(f"[pitfall:{p.get('severity', 'medium')}] {p.get('text', '')[:150]}")
            result["guardrails"] = items
    except Exception as e:
        logger.debug(f"Guardrails pre-fetch failed: {e}")

    # ── Thread context (for follow-ups) ──
    if idea_id:
        try:
            from sqlalchemy import select
            from brain.platform.db.models.idea import IdeaThread
            from brain.platform.db.repositories.unit_of_work import UnitOfWork

            async with UnitOfWork() as uow:
                rows = (await uow.session.scalars(
                    select(IdeaThread)
                    .where(IdeaThread.idea_id == idea_id)
                    .order_by(IdeaThread.created_at.desc())
                    .limit(6)
                )).all()
            summary = "\n".join(f"{row.role}: {(row.content or '')[:240]}" for row in reversed(rows))
            if summary:
                result["thread_context"] = summary
        except Exception as e:
            logger.debug(f"Thread context failed: {e}")

    # ── Shadow attention decision logging (frame assembly) ──
    try:
        frame_candidates = list(result["memories"])
        attention_decision = await observe_retrieval(
            stage="frame_assembly",
            query_text=task,
            candidates=frame_candidates,
            user_id=user_id,
            org_id=org_id,
            preload_budget_tokens=memory_limit * 120,
            lazy_budget_tokens=max(0, memory_limit * 40),
        )
        result["attention_decision"] = attention_decision
        selection = AttentionController().materialize_selection(frame_candidates, attention_decision)
        result["memories"] = selection.selected
        result["suppressed_memories"] = selection.suppressed or result["suppressed_memories"]
        result["lazy_load_memories"] = selection.lazy_load_eligible or result["lazy_load_memories"]
        result["attention_explain"] = AttentionController().explain(attention_decision, frame_candidates)
    except Exception as e:
        logger.debug(f"Attention decision logging failed: {e}")

    return result


# ── Compression ──────────────────────────────────────────────

def _compress_with_gpu_server(task: str, raw_context: str, budget_tokens: int = 500) -> str | None:
    """Compress context using the unified GPU server (local, free).

    Asks the local model to distill the raw context into the most
    task-relevant information within a token budget.
    """
    budget_words = budget_tokens * 3 // 4
    prompt = (
        f"You are a context compressor. Given a task and raw context, "
        f"extract ONLY the information most relevant to the task. "
        f"Output {budget_words} words maximum. Be terse. No commentary.\n\n"
        f"TASK: {task}\n\nRAW CONTEXT:\n{raw_context}\n\n"
        f"COMPRESSED (max {budget_words} words):"
    )
    try:
        from brain.platform.gpu_client import get_client
        result = get_client().generate(
            prompt=prompt, max_tokens=budget_tokens,
            temperature=0.2, think=False, fallback_policy="auto",
        )
        return result.strip() if result else None
    except Exception as e:
        logger.warning(f"GPU server LLM compression failed: {e}")
        return None


def _heuristic_compress(raw_context: str, budget_tokens: int = 500) -> str:
    """Fallback compression: extract most important lines heuristically.

    Prioritizes lines containing: pitfalls, warnings, steps, specific commands.
    """
    budget_chars = budget_tokens * 4
    lines = raw_context.split("\n")

    # Score each line by importance signals
    scored = []
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        score = 0
        line_lower = line_stripped.lower()

        # Headers are structural
        if line_stripped.startswith("#"):
            score += 3

        # Pitfalls/warnings are critical
        if any(w in line_lower for w in ["pitfall", "warning", "never", "always", "danger", "critical"]):
            score += 5

        # Steps/procedures are useful
        if any(w in line_lower for w in ["step", "first", "then", "must", "should", "require"]):
            score += 3

        # Specific commands/paths
        if any(c in line_stripped for c in ["`", "/", ".", "()", "::"]):
            score += 2

        # Short lines are more information-dense per token
        if len(line_stripped) < 100:
            score += 1

        scored.append((score, line_stripped))

    # Take highest-scoring lines until budget
    scored.sort(key=lambda x: x[0], reverse=True)
    result_lines = []
    chars_used = 0
    for score, line in scored:
        if chars_used + len(line) > budget_chars:
            break
        result_lines.append(line)
        chars_used += len(line) + 1

    return "\n".join(result_lines)


# ── Confidence Computation ───────────────────────────────────

def _compute_confidence(
    skill: dict | None,
    heuristics: list[dict] | None,
    memories: list[dict] | None,
) -> float:
    """Compute system confidence for handling this task.

    Based on: skill maturity, heuristic coverage, memory relevance.
    """
    score = 0.3  # base confidence

    if skill:
        maturity_scores = {
            "emerging": 0.1, "developing": 0.2,
            "proficient": 0.3, "expert": 0.4,
        }
        score += maturity_scores.get(skill.get("maturity", "emerging"), 0.1)
        score += min(0.1, (skill.get("confidence", 0) or 0) * 0.1)

    if heuristics:
        # More validated heuristics = more confidence
        avg_conf = sum(h.get("confidence", 0.5) for h in heuristics) / max(len(heuristics), 1)
        score += min(0.15, avg_conf * 0.15)

    if memories:
        # Having relevant memories helps
        score += min(0.1, len(memories) * 0.03)

    return min(1.0, round(score, 3))
