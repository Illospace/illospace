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

    if not skip_harvest and harvest_session is not None:
        harvest_session(
            session_id,
            messages,
            user_id=user_id,
            org_id=effective_org_id,
            idea_id=idea_id,
            run_id=run_id,
        )
    elif not skip_harvest:
        logger.debug("Harvest skipped in sync runtime; await async_apply_agent_session_side_effects for persistence")

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
    harvest_session = harvest_session or _harvest_session
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
