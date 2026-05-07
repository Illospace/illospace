"""Cortex thought encoding — extract insights from ideas and encode to brain memory.

Moved from dashboard/cortex_api.py to break the dashboard dependency.
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)


def encode_thought_to_brain(idea_id: str, force: bool = False):
    """Encode a thought to brain memory via Qwen extraction.

    Triggers: archive, agent-completed, resolved status, nightly digest.
    Skips if already encoded unless force=True.
    """
    try:
        with UnitOfWork() as uow:
            idea = uow.session.execute(text(
                "SELECT id, title, display_title, agent_details, encoded_at, user_id, org_id FROM ideas WHERE id = :id"
            ), {"id": idea_id}).mappings().first()
            if not idea:
                return
            if idea.get("encoded_at") and not force:
                logger.debug(f"Thought {idea_id[:8]} already encoded, skipping")
                return
            threads = uow.session.execute(text(
                "SELECT role, content FROM idea_threads WHERE idea_id = :id ORDER BY created_at"
            ), {"id": idea_id}).mappings().all()

        title = (idea.get("display_title") or idea.get("title", ""))[:80]
        agent_details = idea.get("agent_details") or []
        has_replies = len(threads) > 0
        has_agent_work = bool(agent_details) or any(t["role"] == "assistant" for t in threads)

        # Build thread summary for Qwen
        thread_text = f"Title: {title}\n"
        for t in threads[:20]:
            thread_text += f"[{t['role']}]: {str(t['content'])[:200]}\n"
        if agent_details:
            thread_text += f"Agent work: {json.dumps(agent_details)[:300]}\n"
        thread_text = thread_text[:1500]

        # Determine hints for Qwen
        if has_agent_work:
            hint = "This thought had agent work done on it."
        elif has_replies:
            hint = "This thought had discussion/replies."
        else:
            hint = "This thought had no interaction."

        prompt = f"""{hint}

Given this cortex thought and its conversation history, extract:
1. Key insight or decision (1-2 sentences)
2. Type: lesson | decision | pattern | task_completed
3. Salience: 1-10
If the thought has no meaningful content to remember, return exactly: SKIP

Thought:
{thread_text}"""

        from brain.platform.gpu_client import get_client
        full_prompt = (
            "You are a memory extraction assistant. Output ONLY the requested format.\n\n"
            + prompt
        )
        content = get_client().generate(
            prompt=full_prompt, max_tokens=100,
            temperature=0.3, think=False, fallback_policy="auto",
        )
        content = (content or "").strip()
        if not content or content.upper() == "SKIP":
            logger.info(f"Cortex encode SKIP for idea {idea_id[:8]}")
            # Mark as encoded even on SKIP to avoid re-processing
            try:
                with UnitOfWork() as uow2:
                    uow2.session.execute(text(
                        "UPDATE ideas SET encoded_at = NOW() WHERE id = :id"
                    ), {"id": idea_id})
            except Exception:
                pass
            return

        # Parse extraction
        lines = [l.strip() for l in content.split("\n") if l.strip()]
        insight = lines[0] if lines else content
        # Clean numbered prefixes
        for prefix in ["1.", "1:", "Key insight:", "Key insight or decision:"]:
            if insight.lower().startswith(prefix.lower()):
                insight = insight[len(prefix):].strip()

        memory_type = "noted"
        salience = 5.0
        for line in lines:
            ll = line.lower()
            if "type:" in ll:
                for t in ["lesson", "decision", "pattern", "task_completed"]:
                    if t in ll:
                        memory_type = t
                        break
            if "salience:" in ll:
                m = re.search(r"salience:\s*(\d+)", ll)
                if m:
                    salience = float(m.group(1))

        # Apply journey-based bounds
        if has_agent_work and salience < 6:
            salience = 6.0
        elif has_replies and salience < 5:
            salience = 5.0
        elif not has_replies and salience > 5:
            salience = 4.0

        from brain.app.cli.memory import add_memory
        from brain.platform.db.repositories.memory_write_context import MemoryWriteContext
        summary = f"[Cortex: {title}] {insight[:400]}"
        write_context = MemoryWriteContext(
            user_id=idea["user_id"],
            org_id=idea.get("org_id"),
            visibility="org" if idea.get("org_id") else "private",
            source="cortex",
            idea_id=idea_id,
            confidence=0.6,
            evidence={
                "encoder": "cortex.encode_thought_to_brain",
                "force": force,
            },
        )
        add_memory(
            content=summary,
            memory_type=memory_type,
            salience=salience,
            emotion="neutral",
            source="cortex",
            tags=["cortex", idea_id[:8]],
            write_context=write_context,
        )
        logger.info(f"Encoded thought {idea_id[:8]} to brain (type={memory_type}, salience={salience})")

        # Mark as encoded to prevent double-encoding
        try:
            with UnitOfWork() as uow2:
                uow2.session.execute(text(
                    "UPDATE ideas SET encoded_at = NOW() WHERE id = :id"
                ), {"id": idea_id})
        except Exception:
            pass  # non-critical — worst case is re-encoding

    except Exception as e:
        logger.warning(f"Failed to encode thought to brain: {e}")
