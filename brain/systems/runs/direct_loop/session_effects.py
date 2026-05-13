"""Post-loop session side effects for the agent runtime."""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from brain.systems.sessions.harvest import _harvest_session
from brain.systems.sessions import _save_session, async_save_session

logger = logging.getLogger("agent")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _memory_org_for_user(user_id: str | None, org_id: str | None = None) -> str | None:
    del user_id
    return org_id


async def async_memory_org_for_user(user_id: str | None, org_id: str | None = None) -> str | None:
    if org_id or not user_id:
        return org_id
    try:
        from brain.platform.db.models.org import User
        from brain.platform.db.repositories.unit_of_work import UnitOfWork

        async with UnitOfWork() as uow:
            user = await uow.session.get(User, user_id)
            return user.org_id if user else None
    except Exception:
        return None


def _auto_encode_if_needed(
    tool_calls_made: list[str],
    output: str,
    session_id: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    idea_id: str | None = None,
    run_id: int | None = None,
) -> None:
    """Auto-encode an episode if the agent acted but never called brain_encode."""
    del user_id, org_id, idea_id, run_id

    if "brain_encode" in tool_calls_made:
        return
    if not any(t in tool_calls_made for t in ("write_file", "edit_file", "exec_command")):
        return
    if not output or len(output) <= 50:
        return
    logger.debug("Auto-encode skipped in sync runtime; await async_auto_encode_if_needed for persistence")


async def async_auto_encode_if_needed(
    tool_calls_made: list[str],
    output: str,
    session_id: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    idea_id: str | None = None,
    run_id: int | None = None,
) -> None:
    """Auto-encode an episode using native async memory writes."""

    if "brain_encode" in tool_calls_made:
        return
    if not any(t in tool_calls_made for t in ("write_file", "edit_file", "exec_command")):
        return
    if not output or len(output) <= 50:
        return
    if not user_id:
        logger.debug("Auto-encode skipped: missing user context")
        return
    try:
        from brain.app.cli.memory import add_memory
        from brain.platform.db.repositories.memory_write_context import MemoryWriteContext

        resolved_org_id = await async_memory_org_for_user(user_id, org_id)
        write_context = MemoryWriteContext(
            user_id=user_id,
            org_id=resolved_org_id,
            visibility="org" if resolved_org_id else "private",
            source="agent_auto_encode",
            session_id=session_id,
            idea_id=idea_id,
            run_id=run_id,
            confidence=0.45,
            evidence={"auto_encode": True, "tool_calls": tool_calls_made[-8:]},
        )
        await add_memory(
            content=f"[auto-encoded] {output[:300]}",
            memory_type="episode", salience=4.0,
            source="agent_auto_encode", tags=["auto_encoded", session_id[:20]],
            write_context=write_context,
        )
    except Exception as exc:
        logger.debug("Auto-encode failed: %s", exc)


async def _async_harvest_session(
    session_id: str,
    messages: list[dict],
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    idea_id: str | None = None,
    run_id: int | None = None,
) -> None:
    """Extract and persist structured memories from a conversation."""
    try:
        from datetime import date

        from brain.app.cli.memory import add_memory
        from brain.platform.db.repositories.memory_write_context import MemoryWriteContext
        from brain.platform.providers.model_policy import get_model_for_tier
        from brain.systems.memory.harvest import extract_harvest_items
        from brain.systems.memory.narratives import extract_topic_tags, link_session_to_narratives

        if not messages or len(messages) < 2:
            return
        if not user_id:
            logger.debug("Harvest skipped for session %s: missing user context", session_id)
            return

        harvest_model = get_model_for_tier(
            "low",
            include_provider_prefix=True,
            user_id=user_id,
            org_id=org_id,
        )
        items = extract_harvest_items(
            messages,
            model=harvest_model,
            user_id=user_id,
            org_id=org_id,
        )
        if not items:
            logger.debug("Harvest: no items extracted for session %s", session_id)
            return

        stored_count = 0
        for item in items:
            try:
                write_context = MemoryWriteContext(
                    user_id=user_id,
                    org_id=org_id,
                    visibility=item.visibility_for(org_id),
                    source="harvest",
                    session_id=session_id,
                    idea_id=idea_id,
                    run_id=run_id,
                    confidence=item.confidence,
                    evidence=item.evidence_payload(),
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
            except Exception as exc:
                logger.debug("Harvest: failed to store item: %s", exc)

        topic_tags = extract_topic_tags([item for item in items if not item.raw_episode])
        if topic_tags:
            try:
                await link_session_to_narratives(
                    session_id=session_id,
                    session_date=date.today(),
                    session_summary=items[0].content[:300],
                    topic_tags=topic_tags,
                    org_id=org_id,
                    user_id=user_id,
                )
            except Exception as exc:
                logger.debug("Harvest: narrative linking failed: %s", exc)

        logger.info(
            "Harvest: session %s - %d items stored, %d topic tags linked",
            session_id, stored_count, len(topic_tags),
        )
    except Exception as exc:
        logger.debug("Harvest failed for session %s: %s", session_id, exc)


def apply_agent_session_side_effects(
    *,
    session_id: str,
    messages: list[dict],
    output: str,
    system_prompt: str | None,
    tokens,
    tool_calls_made: list[str],
    user_id: str | None = None,
    metadata: dict | None = None,
    agent_context=None,
    idea_id: str | None = None,
    run_id: int | None = None,
    skip_harvest: bool = False,
    persist_session: bool = True,
    memory_org_for_user: Callable[..., str | None] | None = None,
    auto_encode_if_needed: Callable[..., None] | None = None,
    harvest_session: Callable[..., None] | None = None,
    save_session: Callable[..., None] | None = None,
) -> str | None:
    """Run post-loop auto-encode, harvest, and session persistence effects."""

    metadata = metadata or {}
    memory_org_for_user = memory_org_for_user or _memory_org_for_user
    auto_encode_if_needed = auto_encode_if_needed or _auto_encode_if_needed
    harvest_session = harvest_session or _harvest_session
    save_session = save_session or _save_session

    effective_org_id = (
        getattr(agent_context, "org_id", None)
        or metadata.get("org_id")
        or memory_org_for_user(user_id)
    )

    auto_encode_if_needed(
        tool_calls_made,
        output,
        session_id,
        user_id=user_id,
        org_id=effective_org_id,
        idea_id=idea_id,
        run_id=run_id,
    )

    if not skip_harvest:
        harvest_session(
            session_id,
            messages,
            user_id=user_id,
            org_id=effective_org_id,
            idea_id=idea_id,
            run_id=run_id,
        )

    if persist_session:
        save_session(
            session_id, messages, system_prompt,
            tokens.input, tokens.output, tokens.cache_read, tokens.cache_creation,
        )

    return effective_org_id


async def async_apply_agent_session_side_effects(
    *,
    session_id: str,
    messages: list[dict],
    output: str,
    system_prompt: str | None,
    tokens,
    tool_calls_made: list[str],
    user_id: str | None = None,
    metadata: dict | None = None,
    agent_context=None,
    idea_id: str | None = None,
    run_id: int | None = None,
    skip_harvest: bool = False,
    persist_session: bool = True,
    memory_org_for_user: Callable[..., Any] | None = None,
    auto_encode_if_needed: Callable[..., Any] | None = None,
    harvest_session: Callable[..., Any] | None = None,
    save_session: Callable[..., Any] | None = None,
) -> str | None:
    """Run post-loop auto-encode, harvest, and session persistence effects asynchronously."""

    metadata = metadata or {}
    memory_org_for_user = memory_org_for_user or async_memory_org_for_user
    auto_encode_if_needed = auto_encode_if_needed or async_auto_encode_if_needed
    harvest_session = harvest_session or _async_harvest_session
    save_session = save_session or async_save_session

    effective_org_id = (
        getattr(agent_context, "org_id", None)
        or metadata.get("org_id")
        or await _maybe_await(memory_org_for_user(user_id))
    )

    await _maybe_await(auto_encode_if_needed(
        tool_calls_made,
        output,
        session_id,
        user_id=user_id,
        org_id=effective_org_id,
        idea_id=idea_id,
        run_id=run_id,
    ))

    if not skip_harvest:
        await _maybe_await(harvest_session(
            session_id,
            messages,
            user_id=user_id,
            org_id=effective_org_id,
            idea_id=idea_id,
            run_id=run_id,
        ))

    if persist_session:
        await _maybe_await(save_session(
            session_id, messages, system_prompt,
            tokens.input, tokens.output, tokens.cache_read, tokens.cache_creation,
        ))

    return effective_org_id
