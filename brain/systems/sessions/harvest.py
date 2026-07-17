"""Post-session harvest: extract structured memories from conversations.

Runs after the agent loop completes to extract lessons, patterns, and
episodes from the conversation and link them to project narratives.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("agent")


async def _harvest_session(
    session_id: str,
    messages: list[dict],
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    idea_id: str | None = None,
    run_id: int | None = None,
) -> None:
    """Extract structured memories from conversation via the harvest pipeline.

    Runs the active LLM-based harvest extraction, stores each item as an
    episodic memory, and links extracted topics to project narratives.

    Harvest failure never breaks the session close path.
    """
    try:
        from brain.systems.memory.harvest import extract_harvest_items
        from brain.systems.memory.narratives import extract_topic_tags, link_session_to_narratives
        from brain.app.cli.memory import add_memory
        from brain.platform.db.repositories.memory_write_context import MemoryWriteContext
        from brain.platform.providers.model_policy import get_default_model
        from datetime import date

        if not messages or len(messages) < 2:
            return
        if not user_id:
            logger.debug("Harvest skipped for session %s: missing user context", session_id)
            return

        harvest_model = get_default_model(
            include_provider_prefix=True,
            user_id=user_id,
            org_id=org_id,
        )

        # Extract harvest items from conversation
        items = extract_harvest_items(
            messages,
            model=harvest_model,
            user_id=user_id,
            org_id=org_id,
        )
        if not items:
            logger.debug("Harvest: no items extracted for session %s", session_id)
            return

        # Store each harvest item as an episodic memory
        stored_count = 0
        for item in items:
            try:
                evidence = item.evidence_payload()
                write_context = MemoryWriteContext(
                    user_id=user_id,
                    org_id=org_id,
                    visibility=item.visibility_for(org_id),
                    source="harvest",
                    session_id=session_id,
                    idea_id=idea_id,
                    run_id=run_id,
                    confidence=item.confidence,
                    evidence=evidence,
                )
                await add_memory(
                    content=item.content,
                    memory_type=item.memory_type,
                    salience=item.salience,
                    source="harvest",
                    source_session=session_id,
                    tags=item.topic_tags,
                    write_context=write_context,
                    scope=item.storage_scope(org_id),
                    memory_tier=item.memory_tier,
                    harvest_type=item.harvest_type,
                    harvest_confidence=item.confidence,
                    topic_tags=item.topic_tags,
                )
                stored_count += 1
            except Exception as e:
                logger.debug("Harvest: failed to store item: %s", e)

        # Link session to narratives via extracted topic tags
        topic_tags = extract_topic_tags([item for item in items if not item.raw_episode])
        if topic_tags:
            try:
                # Build a brief session summary from the first harvest item
                session_summary = items[0].content[:300]
                await link_session_to_narratives(
                    session_id=session_id,
                    session_date=date.today(),
                    session_summary=session_summary,
                    topic_tags=topic_tags,
                    org_id=org_id,
                    user_id=user_id,
                )
            except Exception as e:
                logger.debug("Harvest: narrative linking failed: %s", e)

        logger.info(
            "Harvest: session %s — %d items stored, %d topic tags linked",
            session_id, stored_count, len(topic_tags),
        )

    except Exception as e:
        # Never break session close due to harvest failure
        logger.debug("Harvest failed for session %s: %s", session_id, e)


def _extract_text(messages: list[dict]) -> str:
    """Extract final text output from the last assistant message."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, str):
                return content
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif hasattr(block, "type") and block.type == "text":
                    texts.append(block.text)
            if texts:
                return "\n".join(texts)
    return ""
