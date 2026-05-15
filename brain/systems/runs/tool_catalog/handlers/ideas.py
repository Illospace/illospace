"""Cortex idea/thread management tool handlers."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from brain.systems.runs.tool_catalog.handlers.common import *


_IDEA_STATUSES = {
    "emerged",
    "queued",
    "active",
    "working",
    "needs_input",
    "unread_reply",
    "blocked",
    "failed",
    "resolved",
    "stale",
    "paused",
    "done",
    "archived",
}

_RUN_ADMISSION_CREATE_STATUSES = {"queued", "working"}


def _idea_tool_context() -> tuple[str | None, str | None, str | None]:
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    actor_user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    idea_id = (
        getattr(_agent_context, "idea_id", None)
        or execution_metadata.get("idea_id")
        or execution_metadata.get("thread_id")
    )
    return (
        str(org_id) if org_id else None,
        str(actor_user_id) if actor_user_id else None,
        str(idea_id) if idea_id else None,
    )


def _idea_actor(*, org_id: str | None, actor_user_id: str | None) -> dict[str, Any]:
    return {
        "id": actor_user_id,
        "org_id": org_id,
        "role": "owner",
        "principal_type": "human",
    }


def _caller_is_service_actor(actor: dict[str, Any]) -> bool:
    from brain.app.api.routers.cortex._helpers import _caller_is_service_principal

    return bool(_caller_is_service_principal(actor))


def _actor_org_context(actor: dict[str, Any]) -> str:
    from brain.app.api.authorization import require_org_context

    return str(require_org_context(actor))


async def _require_idea_for_actor(session, idea_id: str, actor: dict[str, Any]):
    from brain.app.api.routers.cortex._helpers import _a_require_idea_for_user

    return await _a_require_idea_for_user(session, idea_id, actor)


def _target_idea_id(idea_id: str | None, thread_id: str | None, context_idea_id: str | None) -> str | None:
    return str(idea_id or thread_id or context_idea_id).strip() or None


async def _serialize_idea(idea, session) -> dict[str, Any]:
    from brain.app.api.routers.cortex._ideas import _idea_read_with_author

    return (await _idea_read_with_author(idea, session)).model_dump(mode="json")


def _current_tool_run_id() -> int | None:
    run = getattr(_agent_context, "run", None)
    run_id = getattr(run, "run_id", None) or getattr(run, "id", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    run_id = run_id or execution_metadata.get("run_id")
    try:
        return int(run_id) if run_id is not None else None
    except Exception:
        return None


def _created_idea_seed_content(
    *,
    title: str,
    description: str | None,
    thread_message: str | None,
) -> str:
    explicit = " ".join(str(thread_message or "").split())
    if explicit:
        return explicit
    body = str(description or "").strip()
    if body:
        return body
    return str(title or "").strip()


def _created_idea_run_message(idea, seed_content: str) -> str:
    title = str(getattr(idea, "title", "") or "")
    idea_id = str(getattr(idea, "id", "") or "")
    return f'[Idea: "{title}" | {idea_id}]\n\n{seed_content[:2000]}'


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


async def _seed_created_idea_thread(
    session,
    *,
    idea,
    seed_content: str,
    actor_user_id: str | None,
    owner_user_id: str | None,
    parent_id: str | None,
    origin_ref: str | None,
) -> Any | None:
    from brain.platform.db.models.idea import IdeaThread

    if not seed_content:
        return None
    source_run_id = _current_tool_run_id()
    thread_msg = IdeaThread(
        idea_id=str(idea.id),
        role="illo",
        content=seed_content,
        user_id=None,
        message_type="agent_response",
        metadata_={
            "source": "manage_idea.create",
            "author": "illo",
            "requested_by_user_id": actor_user_id,
            "owner_user_id": owner_user_id,
            "created_by_run_id": source_run_id,
            "parent_idea_id": parent_id,
            "origin_ref": origin_ref,
        },
    )
    session.add(thread_msg)
    await session.flush()
    return thread_msg


async def _admit_created_idea_run(
    session,
    *,
    idea,
    seed_content: str,
    actor_user_id: str | None,
    parent_id: str | None,
    origin_ref: str | None,
    thread_message_id: int | None,
):
    from brain.systems.runs.cortex import RunAdmissionRequest, async_admit_run

    source_run_id = _current_tool_run_id()
    result = await async_admit_run(
        RunAdmissionRequest(
            idea_id=str(idea.id),
            event="idea_created",
            message=_created_idea_run_message(idea, seed_content),
            user_id=actor_user_id,
            metadata={
                "created_by_tool": "manage_idea",
                "created_by_run_id": source_run_id,
                "parent_idea_id": parent_id,
                "origin_ref": origin_ref,
                "thread_message_id": thread_message_id,
            },
            source="tool:manage_idea",
            producer="agent_tool",
            idempotency_key=(
                f"manage_idea:create:{source_run_id}:{idea.id}"
                if source_run_id is not None
                else f"manage_idea:create:{idea.id}"
            ),
        ),
        session=session,
    )
    if not result.ok:
        raise RuntimeError(result.skipped_reason or "Failed to admit AgentRun for created idea")
    return result


def _scoped_ideas_stmt(org_id: str | None, actor_user_id: str | None):
    from brain.platform.db.models.idea import Idea

    stmt = select(Idea).order_by(Idea.updated_at.desc().nullslast(), Idea.created_at.desc())
    if org_id:
        return stmt.where(Idea.org_id == org_id)
    if actor_user_id:
        return stmt.where(Idea.user_id == actor_user_id)
    raise ValueError("manage_idea requires an org-scoped or user-scoped run")


async def _list_ideas(
    session,
    *,
    org_id: str | None,
    actor_user_id: str | None,
    status: str | None,
    search: str | None,
    include_archived: bool,
    limit: int | None,
) -> list[dict[str, Any]]:
    from brain.platform.db.models.idea import Idea

    capped_limit = max(1, min(int(limit or 20), 100))
    stmt = _scoped_ideas_stmt(org_id, actor_user_id)
    if status:
        if status not in _IDEA_STATUSES:
            raise ValueError(f"Unsupported idea status: {status}")
        stmt = stmt.where(Idea.status == status)
    if not include_archived and status != "archived":
        stmt = stmt.where(Idea.archived_at.is_(None))
    needle = " ".join(str(search or "").strip().lower().split())
    if needle:
        pattern = f"%{needle}%"
        stmt = stmt.where(
            or_(
                func.lower(Idea.title).like(pattern),
                func.lower(Idea.display_title).like(pattern),
                func.lower(Idea.description).like(pattern),
            )
        )
    ideas = (await session.scalars(stmt.limit(capped_limit))).all()
    return [await _serialize_idea(idea, session) for idea in ideas]


async def _status_change(idea, next_status: str, *, trigger: str, session) -> tuple[str, str] | None:
    from brain.platform.db.models.idea import IdeaStateLog

    if next_status not in _IDEA_STATUSES:
        raise ValueError(f"Unsupported idea status: {next_status}")
    old_status = str(idea.status or "")
    if old_status == next_status and (next_status != "archived" or idea.archived_at is not None):
        return None
    idea.status = next_status
    idea.updated_at = datetime.now(timezone.utc)
    if next_status == "archived":
        idea.archived_at = datetime.now(timezone.utc)
    session.add(
        IdeaStateLog(
            idea_id=str(idea.id),
            from_state=old_status,
            to_state=next_status,
            trigger=trigger,
        )
    )
    return old_status, next_status


async def _restore_idea(idea, *, session) -> tuple[str, str]:
    from brain.platform.db.models.idea import IdeaStateLog

    old_status = str(idea.status or "")
    idea.archived_at = None
    if old_status == "archived":
        idea.status = "emerged"
    idea.updated_at = datetime.now(timezone.utc)
    session.add(
        IdeaStateLog(
            idea_id=str(idea.id),
            from_state=old_status,
            to_state=str(idea.status or ""),
            trigger="agent_restore",
        )
    )
    return old_status, str(idea.status or "")


async def _apply_owner_handoff(idea, *, next_owner_id: str, actor: dict[str, Any], session) -> None:
    from brain.app.api.routers.cortex._ideas import _freeze_display_author_for_handoff
    from brain.platform.db.models.org import User

    target_user = await session.scalar(select(User).where(User.id == str(next_owner_id)))
    if target_user is None:
        raise HTTPException(status_code=404, detail="Target owner not found")
    if not _caller_is_service_actor(actor):
        org_id = _actor_org_context(actor)
        if str(target_user.org_id) != str(org_id):
            raise HTTPException(status_code=403, detail="Target owner is outside this org")
    if str(next_owner_id) != str(getattr(idea, "user_id", "")):
        await _freeze_display_author_for_handoff(idea, session)
    idea.user_id = str(next_owner_id)


async def _validated_create_owner_id(
    *,
    session,
    requested_owner_id: str | None,
    fallback_owner_id: str,
    actor: dict[str, Any],
) -> str:
    from brain.platform.db.models.org import User

    owner_id = str(requested_owner_id or fallback_owner_id)
    if owner_id == str(fallback_owner_id):
        return owner_id

    target_user = await session.scalar(select(User).where(User.id == owner_id))
    if target_user is None:
        raise HTTPException(status_code=404, detail="Target owner not found")
    if not _caller_is_service_actor(actor):
        org_id = _actor_org_context(actor)
        if str(target_user.org_id) != str(org_id):
            raise HTTPException(status_code=403, detail="Target owner is outside this org")
    return owner_id


async def _apply_idea_updates(
    idea,
    *,
    actor: dict[str, Any],
    session,
    title: str | None = None,
    display_title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    salience_score: float | None = None,
    position_x: float | None = None,
    position_y: float | None = None,
    position_sticky: bool | None = None,
    orbit_anchor_type: str | None = None,
    orbit_anchor_id: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    from brain.app.api.routers.cortex._ideas import _apply_orbit_anchor

    updates: dict[str, Any] = {}
    for key, value in {
        "title": title,
        "display_title": display_title,
        "description": description,
        "salience_score": salience_score,
        "position_x": position_x,
        "position_y": position_y,
        "position_sticky": position_sticky,
    }.items():
        if value is not None:
            setattr(idea, key, value)
            updates[key] = value

    if user_id is not None:
        await _apply_owner_handoff(idea, next_owner_id=user_id, actor=actor, session=session)
        updates["user_id"] = str(user_id)

    if orbit_anchor_type is not None or orbit_anchor_id is not None:
        next_anchor_type = None if str(orbit_anchor_type or "").lower() == "none" else orbit_anchor_type
        await _apply_orbit_anchor(
            idea,
            anchor_type=next_anchor_type,
            anchor_id=orbit_anchor_id,
            db=session,
            user=actor,
        )
        updates["orbit_anchor_type"] = idea.orbit_anchor_type
        updates["orbit_anchor_id"] = idea.orbit_anchor_id

    if status is not None:
        await _status_change(idea, status, trigger="agent_update", session=session)
        updates["status"] = status
        if status == "archived":
            updates["archived_at"] = idea.archived_at.isoformat() if idea.archived_at else None

    if updates:
        idea.updated_at = datetime.now(timezone.utc)
    return updates


async def _handle_manage_idea(
    action: str,
    operation: str | None = None,
    idea_id: str | None = None,
    thread_id: str | None = None,
    title: str | None = None,
    thread_message: str | None = None,
    start_run: bool | None = None,
    display_title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    salience_score: float | None = None,
    position_x: float | None = None,
    position_y: float | None = None,
    position_sticky: bool | None = None,
    orbit_anchor_type: str | None = None,
    orbit_anchor_id: str | None = None,
    parent_id: str | None = None,
    user_id: str | None = None,
    origin: str = "illo_created",
    origin_ref: str | None = None,
    search: str | None = None,
    include_archived: bool = False,
    limit: int | None = 20,
) -> str:
    normalized_action = str(action or "").strip().lower()
    if normalized_action in {"help", "schema"}:
        return _manage_tool_guide("manage_idea", operation)

    from brain.systems.cortex.events import publish_safe
    from brain.platform.db.models.idea import Idea
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    org_id, actor_user_id, context_idea_id = _idea_tool_context()
    actor = _idea_actor(org_id=org_id, actor_user_id=actor_user_id)
    target_idea_id = _target_idea_id(idea_id, thread_id, context_idea_id)
    event: tuple[str, dict[str, Any]] | None = None

    try:
        async with UnitOfWork() as uow:
            if normalized_action == "list":
                ideas = await _list_ideas(
                    uow.session,
                    org_id=org_id,
                    actor_user_id=actor_user_id,
                    status=status,
                    search=search,
                    include_archived=include_archived,
                    limit=limit,
                )
                return json.dumps({"ideas": ideas}, default=str)

            if normalized_action == "create":
                if not actor_user_id:
                    return json.dumps({"error": "create requires a user-scoped run"})
                if not title:
                    return json.dumps({"error": "create requires: title"})
                requested_status = status or "emerged"
                if requested_status not in _IDEA_STATUSES:
                    return json.dumps({"error": f"Unsupported idea status: {requested_status}"})
                should_start_run = _optional_bool(start_run)
                if should_start_run is None:
                    should_start_run = requested_status in _RUN_ADMISSION_CREATE_STATUSES
                initial_status = "emerged" if should_start_run or requested_status in _RUN_ADMISSION_CREATE_STATUSES else requested_status
                owner_user_id = await _validated_create_owner_id(
                    session=uow.session,
                    requested_owner_id=user_id,
                    fallback_owner_id=actor_user_id,
                    actor=actor,
                )
                idea = Idea(
                    title=title,
                    description=description,
                    status=initial_status,
                    origin=origin or "illo_created",
                    origin_ref=origin_ref,
                    parent_id=parent_id,
                    user_id=owner_user_id,
                    org_id=org_id,
                )
                if salience_score is not None:
                    idea.salience_score = salience_score
                if position_x is not None:
                    idea.position_x = position_x
                if position_y is not None:
                    idea.position_y = position_y
                uow.session.add(idea)
                await uow.session.flush()
                if orbit_anchor_type is not None or orbit_anchor_id is not None:
                    await _apply_idea_updates(
                        idea,
                        actor=actor,
                        session=uow.session,
                        orbit_anchor_type=orbit_anchor_type,
                        orbit_anchor_id=orbit_anchor_id,
                    )
                    await uow.session.flush()
                seed_content = _created_idea_seed_content(
                    title=title,
                    description=description,
                    thread_message=thread_message,
                )
                seed_thread = await _seed_created_idea_thread(
                    uow.session,
                    idea=idea,
                    seed_content=seed_content,
                    actor_user_id=actor_user_id,
                    owner_user_id=owner_user_id,
                    parent_id=parent_id,
                    origin_ref=origin_ref,
                )
                run_result = None
                if should_start_run:
                    run_result = await _admit_created_idea_run(
                        uow.session,
                        idea=idea,
                        seed_content=seed_content,
                        actor_user_id=actor_user_id,
                        parent_id=parent_id,
                        origin_ref=origin_ref,
                        thread_message_id=getattr(seed_thread, "id", None),
                    )
                serialized = await _serialize_idea(idea, uow.session)
                event = ("idea_created", {"idea_id": str(idea.id), "title": idea.title})
                result = {
                    "idea": serialized,
                    "created": True,
                    "thread_message_id": getattr(seed_thread, "id", None),
                    "run_id": getattr(run_result, "run_id", None),
                    "run_started": run_result is not None,
                }

            else:
                if not target_idea_id:
                    return json.dumps({"error": f"{normalized_action or 'action'} requires: idea_id when no current Cortex thread is bound"})
                idea = await _require_idea_for_actor(uow.session, target_idea_id, actor)

                if normalized_action == "get":
                    return json.dumps({"idea": await _serialize_idea(idea, uow.session)}, default=str)

                if normalized_action == "archive":
                    change = await _status_change(idea, "archived", trigger="agent_archive", session=uow.session)
                    await uow.session.flush()
                    serialized = await _serialize_idea(idea, uow.session)
                    event = ("idea_archived", {"idea_id": str(idea.id), "idea": serialized})
                    result = {
                        "ok": True,
                        "archived": True,
                        "idea": serialized,
                        "status_change": {"old_status": change[0], "new_status": change[1]} if change else None,
                    }

                elif normalized_action == "restore":
                    old_status, new_status = await _restore_idea(idea, session=uow.session)
                    await uow.session.flush()
                    serialized = await _serialize_idea(idea, uow.session)
                    event = ("idea_restored", {"idea_id": str(idea.id), "idea": serialized})
                    result = {
                        "ok": True,
                        "restored": True,
                        "idea": serialized,
                        "status_change": {"old_status": old_status, "new_status": new_status},
                    }

                elif normalized_action == "set_status":
                    if not status:
                        return json.dumps({"error": "set_status requires: status"})
                    change = await _status_change(idea, status, trigger="agent_set_status", session=uow.session)
                    await uow.session.flush()
                    serialized = await _serialize_idea(idea, uow.session)
                    if status == "archived":
                        event = ("idea_archived", {"idea_id": str(idea.id), "idea": serialized})
                    elif change:
                        event = (
                            "status_change",
                            {"idea_id": str(idea.id), "old_status": change[0], "new_status": change[1]},
                        )
                    result = {
                        "ok": True,
                        "idea": serialized,
                        "status_change": {"old_status": change[0], "new_status": change[1]} if change else None,
                    }

                elif normalized_action == "mark_read":
                    old_status = str(idea.status or "")
                    if old_status == "unread_reply":
                        await _status_change(idea, "needs_input", trigger="user_read", session=uow.session)
                    idea.read_at = datetime.now(timezone.utc)
                    if actor_user_id:
                        await uow.notifications.mark_read_for_idea(user_id=actor_user_id, idea_id=str(idea.id))
                    await uow.session.flush()
                    serialized = await _serialize_idea(idea, uow.session)
                    if old_status == "unread_reply":
                        event = (
                            "status_change",
                            {"idea_id": str(idea.id), "old_status": old_status, "new_status": "needs_input"},
                        )
                    result = {"ok": True, "marked_read": True, "idea": serialized}

                elif normalized_action == "update":
                    updates = await _apply_idea_updates(
                        idea,
                        actor=actor,
                        session=uow.session,
                        title=title,
                        display_title=display_title,
                        description=description,
                        status=status,
                        salience_score=salience_score,
                        position_x=position_x,
                        position_y=position_y,
                        position_sticky=position_sticky,
                        orbit_anchor_type=orbit_anchor_type,
                        orbit_anchor_id=orbit_anchor_id,
                        user_id=user_id,
                    )
                    if not updates:
                        return json.dumps({"error": "update requires at least one field to change"})
                    await uow.session.flush()
                    serialized = await _serialize_idea(idea, uow.session)
                    if updates.get("status") == "archived":
                        event = ("idea_archived", {"idea_id": str(idea.id), "idea": serialized})
                    else:
                        event = ("idea_updated", {"idea_id": str(idea.id), "fields": updates})
                    result = {"ok": True, "idea": serialized, "fields": updates}

                else:
                    return json.dumps({"error": f"Unknown action: {action}"})

        if event is not None:
            publish_safe(event[0], event[1])
        return json.dumps(result, default=str)
    except HTTPException as exc:
        return json.dumps({"error": exc.detail, "status_code": exc.status_code})
    except Exception as exc:
        logger.exception("manage_idea failed: %s", exc)
        return json.dumps({"error": str(exc)})


__all__ = [name for name in globals() if not name.startswith("__")]
