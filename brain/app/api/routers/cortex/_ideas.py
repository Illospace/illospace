"""Cortex ideas CRUD and notification endpoints."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.mentions import (
    TEAM_MENTION_WITHOUT_ILLO_SKIP_REASON,
    classify_mention_intent,
)
from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db
from brain.app.api.routers.cortex._helpers import (
    _a_require_idea_for_user as _require_idea_for_user,
    _caller_is_service_principal,
)
from brain.app.api.routers.cortex._router import router
from brain.app.api.routers.ws import ws_manager
from brain.app.api.schemas.ideas import (
    IdeaConnectionRead,
    IdeaCreate,
    IdeaRead,
    IdeaStatusUpdate,
    IdeaUpdate,
    ThreadMessageCreate,
    ThreadMessageRead,
    VisualBlockRead,
)
from brain.platform.db.models.idea import IdeaThread, VisualBlock
from brain.platform.db.models.org import User
from brain.platform.db.models.workspace_pin import WorkspacePin
from brain.platform.db.repositories.ideas import (
    IdeaConnectionRepository,
    IdeaRepository,
    IdeaThreadRepository,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.thought_lifecycle import (
    ThoughtStatusCommand,
    ThreadMessageCommand,
    post_thread_message,
    transition_thought_status,
)
from brain.systems.cortex.project_context.resolution import merge_project_context_metadata
from brain.systems.cortex.thread_links import thread_link_payload
from brain.systems.cortex.title_generation import generate_and_store_idea_display_title
from brain.systems.runs.cortex.read_models import (
    public_failures_for_run_ids,
    public_run_linked_message,
    run_id_from_public_message_metadata,
)


logger = logging.getLogger(__name__)


async def _last_human_thread_author(idea_id: str, db: AsyncSession):
    result = await db.execute(
        select(User.name, User.color)
        .join(IdeaThread, IdeaThread.user_id == User.id)
        .where(IdeaThread.idea_id == idea_id)
        .where(IdeaThread.role == "user")
        .where(IdeaThread.user_id.is_not(None))
        .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
        .limit(1)
    )
    return result.one_or_none()


def _fallback_display_author_id(idea) -> str | None:
    details = getattr(idea, "agent_details", None)
    if isinstance(details, dict):
        author_id = details.get("display_author_user_id")
        if author_id:
            return str(author_id)
    owner_id = getattr(idea, "user_id", None)
    return str(owner_id) if owner_id else None


async def _user_author_row(db: AsyncSession, user_id: str | None):
    if not user_id:
        return None
    result = await db.execute(
        select(User.name, User.color)
        .where(User.id == str(user_id))
        .limit(1)
    )
    return result.one_or_none()


def _row_value(row, key: str, index: int = 0):
    if isinstance(row, dict):
        return row.get(key)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping[key]
    if hasattr(row, key):
        return getattr(row, key)
    try:
        return row[index]
    except Exception:
        return None


def _apply_author_hint(payload: dict[str, Any], hint: dict[str, str | None] | None) -> None:
    if not hint:
        return
    author_name = hint.get("author_name")
    author_color = hint.get("author_color")
    if isinstance(author_name, str):
        payload["author_name"] = author_name
    if isinstance(author_color, str):
        payload["author_color"] = author_color


def _project_context_for_idea(idea) -> dict[str, Any] | None:
    details = getattr(idea, "agent_details", None)
    if not isinstance(details, dict):
        return None
    project_context = details.get("project_context") or details.get("project_context_snapshot")
    return project_context if isinstance(project_context, dict) else None


def _apply_thread_link_fields(payload: dict[str, Any], idea_id: Any) -> None:
    links = thread_link_payload(idea_id)
    payload["thread_route"] = links["thread_route"]
    payload["thread_url"] = links["thread_url"]


def _thread_message_read_payload(
    message: IdeaThread,
    failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    metadata = message.metadata_ if isinstance(message.metadata_, dict) else {}
    content, metadata = public_run_linked_message(message.content, metadata, failure)
    return {
        "id": message.id,
        "idea_id": str(message.idea_id) if message.idea_id else None,
        "role": message.role,
        "content": content,
        "attachments": message.attachments or [],
        "metadata": metadata,
        "object_references": metadata.get("object_references") or [],
        "thread_references": metadata.get("thread_references") or [],
        "user_id": str(message.user_id) if message.user_id else None,
        "created_at": message.created_at,
    }


def _product_event_org_id(idea, user: dict[str, Any]) -> str | None:
    org_id = getattr(idea, "org_id", None)
    if org_id:
        return str(org_id)
    if _caller_is_service_principal(user):
        return None
    return require_org_context(user)


async def _freeze_display_author_for_handoff(idea, db: AsyncSession) -> None:
    """Keep the pre-handoff display author when no thread author exists yet."""
    if await _last_human_thread_author(idea.id, db) is not None:
        return

    details = getattr(idea, "agent_details", None)
    next_details = dict(details) if isinstance(details, dict) else {}
    if next_details.get("display_author_user_id"):
        return

    owner_id = getattr(idea, "user_id", None)
    if owner_id:
        next_details["display_author_user_id"] = str(owner_id)
        idea.agent_details = next_details


def _thread_reply_should_invoke_illo(thread_message: str) -> bool:
    return classify_mention_intent(thread_message or "").should_invoke_illo


def _message_should_invoke_illo(message: str) -> bool:
    return classify_mention_intent(message or "").should_invoke_illo


def _mention_skip_response() -> dict[str, Any]:
    return {
        "ok": True,
        "route": "none",
        "skipped_reason": TEAM_MENTION_WITHOUT_ILLO_SKIP_REASON,
    }


async def _latest_user_thread_metadata(db: AsyncSession, idea_id: str) -> dict[str, Any]:
    from brain.systems.cortex.project_context.resolution import latest_user_thread_metadata

    return await latest_user_thread_metadata(db, idea_id)


async def _effective_notify_metadata(db: AsyncSession, idea_id: str, metadata: Any) -> dict[str, Any]:
    return merge_project_context_metadata(await _latest_user_thread_metadata(db, idea_id), metadata)


async def _author_hints_for_ideas(
    ideas: list[Any],
    db: AsyncSession,
) -> dict[str, dict[str, str | None]]:
    idea_ids = [str(idea.id) for idea in ideas if getattr(idea, "id", None)]
    if not idea_ids:
        return {}

    ranked_authors = (
        select(
            IdeaThread.idea_id.label("idea_id"),
            User.name.label("author_name"),
            User.color.label("author_color"),
            func.row_number()
            .over(
                partition_by=IdeaThread.idea_id,
                order_by=(IdeaThread.created_at.desc(), IdeaThread.id.desc()),
            )
            .label("rank"),
        )
        .join(User, IdeaThread.user_id == User.id)
        .where(
            IdeaThread.idea_id.in_(idea_ids),
            IdeaThread.role == "user",
            IdeaThread.user_id.is_not(None),
        )
        .subquery()
    )
    author_result = await db.execute(
        select(
            ranked_authors.c.idea_id,
            ranked_authors.c.author_name,
            ranked_authors.c.author_color,
        ).where(ranked_authors.c.rank == 1)
    )
    author_rows = author_result.all()

    hints: dict[str, dict[str, str | None]] = {}
    for row in author_rows:
        idea_id = _row_value(row, "idea_id", 0)
        if idea_id is None:
            continue
        hints[str(idea_id)] = {
            "author_name": _row_value(row, "author_name", 1),
            "author_color": _row_value(row, "author_color", 2),
        }

    fallback_by_idea: dict[str, str] = {}
    for idea in ideas:
        idea_id = str(getattr(idea, "id", "") or "")
        if not idea_id or idea_id in hints:
            continue
        fallback_user_id = _fallback_display_author_id(idea)
        if fallback_user_id:
            fallback_by_idea[idea_id] = fallback_user_id

    fallback_user_ids = sorted(set(fallback_by_idea.values()))
    if fallback_user_ids:
        user_result = await db.execute(
            select(User.id, User.name, User.color).where(User.id.in_(fallback_user_ids))
        )
        user_rows = user_result.all()
        users_by_id = {
            str(_row_value(row, "id", 0)): {
                "author_name": _row_value(row, "name", 1),
                "author_color": _row_value(row, "color", 2),
            }
            for row in user_rows
            if _row_value(row, "id", 0) is not None
        }
        for idea_id, user_id in fallback_by_idea.items():
            hint = users_by_id.get(str(user_id))
            if hint:
                hints[idea_id] = hint

    return hints


async def _idea_read_with_author(
    idea,
    db: AsyncSession,
    *,
    author_hint: dict[str, str | None] | None = None,
    author_hint_loaded: bool = False,
) -> IdeaRead:
    """Serialize display color from the last human interaction.

    The persisted owner/orbit target is still idea.user_id. author_color is a
    presentation hint and intentionally can differ from owner after a handoff.
    """
    payload = IdeaRead.model_validate(idea).model_dump()
    _apply_thread_link_fields(payload, getattr(idea, "id", ""))
    payload["project_context"] = _project_context_for_idea(idea)
    if author_hint_loaded:
        _apply_author_hint(payload, author_hint)
        return IdeaRead.model_validate(payload)

    last_author = await _last_human_thread_author(idea.id, db)
    if last_author is not None:
        author_name = getattr(last_author, "name", None)
        author_color = getattr(last_author, "color", None)
        if isinstance(author_name, str):
            payload["author_name"] = author_name
        if isinstance(author_color, str):
            payload["author_color"] = author_color
    else:
        owner = await _user_author_row(db, _fallback_display_author_id(idea))
        if owner is not None:
            owner_name = getattr(owner, "name", None)
            owner_color = getattr(owner, "color", None)
            if isinstance(owner_name, str):
                payload["author_name"] = owner_name
            if isinstance(owner_color, str):
                payload["author_color"] = owner_color
    return IdeaRead.model_validate(payload)


async def _ideas_read_with_author(ideas, db: AsyncSession) -> list[IdeaRead]:
    idea_list = list(ideas)
    author_hints = await _author_hints_for_ideas(idea_list, db)
    return [
        await _idea_read_with_author(
            idea,
            db,
            author_hint=author_hints.get(str(getattr(idea, "id", ""))),
            author_hint_loaded=True,
        )
        for idea in idea_list
    ]


async def _apply_orbit_anchor(
    idea,
    *,
    anchor_type: str | None,
    anchor_id: str | None,
    db: AsyncSession,
    user: dict[str, Any],
) -> None:
    normalized_type = str(anchor_type).strip().lower() if anchor_type else None
    normalized_id = str(anchor_id).strip() if anchor_id else None

    if not normalized_type and not normalized_id:
        idea.orbit_anchor_type = None
        idea.orbit_anchor_id = None
        return

    if normalized_type in {None, "", "user"}:
        if normalized_id:
            target_user = await db.scalar(select(User).where(User.id == normalized_id))
            if target_user is None:
                raise HTTPException(status_code=404, detail="Target orbit user not found")
            if not _caller_is_service_principal(user):
                org_id = require_org_context(user)
                if str(target_user.org_id) != str(org_id):
                    raise HTTPException(status_code=403, detail="Target orbit user is outside this org")
        idea.orbit_anchor_type = "user" if normalized_id else None
        idea.orbit_anchor_id = normalized_id
        return

    if normalized_type == "pin":
        if not normalized_id:
            raise HTTPException(status_code=400, detail="Pin orbit anchor requires orbit_anchor_id")
        org_id = None if _caller_is_service_principal(user) else require_org_context(user)
        stmt = select(WorkspacePin).where(
            WorkspacePin.id == normalized_id,
            WorkspacePin.archived_at.is_(None),
        )
        if org_id is not None:
            stmt = stmt.where(WorkspacePin.org_id == org_id)
        pin = await db.scalar(stmt)
        if pin is None:
            raise HTTPException(status_code=404, detail="Pin orbit anchor not found")
        idea.orbit_anchor_type = "pin"
        idea.orbit_anchor_id = normalized_id
        return

    raise HTTPException(status_code=400, detail="Unsupported orbit anchor type")

# ═══════════════════════════════════════════════════════════════
# ORM-based CRUD endpoints
# ═══════════════════════════════════════════════════════════════

async def list_ideas_payload(
    status: str | None = None,
    db: AsyncSession | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> list[IdeaRead]:
    assert db is not None, "list_ideas_payload requires an explicit async session"
    repo = IdeaRepository(db)
    if _caller_is_service_principal(user):
        ideas = await repo.a_list_by_status(status) if status else await repo.a_list_active()
        return await _ideas_read_with_author(list(ideas), db)

    org_id = require_org_context(user)
    ideas = (
        await repo.a_list_by_status_for_org(status, org_id)
        if status
        else await repo.a_list_active_for_org(org_id)
    )
    return await _ideas_read_with_author(list(ideas), db)


@router.get("/ideas", response_model=list[IdeaRead])
async def list_ideas(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await list_ideas_payload(status=status, db=db, user=user)


@router.get("/ideas/archived", response_model=list[IdeaRead])
async def list_archived_ideas(
    limit: int = 12,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    capped_limit = max(1, min(int(limit or 12), 50))
    repo = IdeaRepository(db)
    if _caller_is_service_principal(user):
        ideas = await repo.a_list_archived(limit=capped_limit)
        return await _ideas_read_with_author(list(ideas), db)

    org_id = require_org_context(user)
    ideas = await repo.a_list_archived_for_org(org_id, limit=capped_limit)
    return await _ideas_read_with_author(list(ideas), db)


@router.delete("/ideas/archived")
async def empty_archived_ideas(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    repo = IdeaRepository(db)
    if _caller_is_service_principal(user):
        deleted = await repo.a_hard_delete_archived()
        event_org_id = None
    else:
        org_id = require_org_context(user)
        deleted = await repo.a_hard_delete_archived_for_org(org_id)
        event_org_id = str(org_id)
    await db.commit()
    if deleted:
        await ws_manager.broadcast_product_event(
            "idea_archive_emptied",
            {"deleted": deleted},
            org_id=event_org_id,
        )
    return {"deleted": deleted}


@router.post("/ideas", response_model=IdeaRead, status_code=201)
async def create_idea(
    body: IdeaCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    repo = IdeaRepository(db)
    org_id = user.get("org_id") or None
    if not _caller_is_service_principal(user):
        org_id = require_org_context(user)
    kwargs = dict(
        title=body.title,
        description=body.description,
        status=body.status,
        origin=body.origin,
        origin_ref=body.origin_ref,
        parent_id=body.parent_id,
        user_id=user["id"],
        org_id=org_id,
    )
    if body.salience_score is not None:
        kwargs["salience_score"] = body.salience_score
    if body.position_x is not None:
        kwargs["position_x"] = body.position_x
    if body.position_y is not None:
        kwargs["position_y"] = body.position_y
    idea = await repo.a_create(**kwargs)
    if body.orbit_anchor_type is not None or body.orbit_anchor_id is not None:
        await _apply_orbit_anchor(
            idea,
            anchor_type=body.orbit_anchor_type,
            anchor_id=body.orbit_anchor_id,
            db=db,
            user=user,
        )
    await db.flush()
    created = await _idea_read_with_author(idea, db)
    event = {
        "idea_id": str(idea.id),
        "title": idea.title,
        "user_id": str(user.get("id")) if user.get("id") else None,
        "org_id": str(org_id) if org_id else None,
        "event_org_id": _product_event_org_id(idea, user),
    }
    await db.commit()
    background_tasks.add_task(
        generate_and_store_idea_display_title,
        idea_id=event["idea_id"],
        raw_title=event["title"],
        user_id=event["user_id"],
        org_id=event["org_id"],
    )
    await ws_manager.broadcast_product_event(
        "idea_created",
        {"idea_id": event["idea_id"], "title": event["title"]},
        org_id=event["event_org_id"],
    )
    return created


@router.put("/ideas/positions")
async def update_positions_batch(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Batch update idea positions (called when D3 simulation settles)."""
    data = await request.json()
    positions = data.get("positions", [])
    if not positions:
        return {"ok": True, "updated": 0}
    org_id = None if _caller_is_service_principal(user) else require_org_context(user)
    repo = IdeaRepository(db)
    updated = 0
    for p in positions:
        idea_id = p.get("id")
        if not idea_id:
            continue
        idea = await repo.a_get(idea_id) if org_id is None else await repo.a_get_for_org(idea_id, org_id)
        if idea and idea.archived_at is None:
            idea.position_x = p.get("x")
            idea.position_y = p.get("y")
            updated += 1
    await db.commit()
    return {"ok": True, "updated": updated}


@router.get("/ideas/{idea_id}", response_model=IdeaRead)
async def get_idea(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    idea = await _require_idea_for_user(db, idea_id, user)
    return await _idea_read_with_author(idea, db)


async def _do_update_idea(
    idea_id: str,
    body: IdeaUpdate,
    db: AsyncSession,
    user: dict[str, Any],
):
    idea = await _require_idea_for_user(db, idea_id, user)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    # Handle status change with state logging
    if "status" in updates:
        new_status = updates.pop("status")
        await transition_thought_status(
            db,
            idea=idea,
            command=ThoughtStatusCommand(
                to_status=new_status,
                trigger="idea_update",
                actor=user,
            ),
        )
    # user_id is the Cortex owner. A handoff must stay inside the caller org and
    # must not implicitly archive, move orbit anchors, or recolor the thread.
    if "user_id" in updates:
        next_owner_id = updates.pop("user_id")
        if next_owner_id is not None:
            target_user = await db.scalar(select(User).where(User.id == str(next_owner_id)))
            if target_user is None:
                raise HTTPException(status_code=404, detail="Target owner not found")
            if not _caller_is_service_principal(user):
                org_id = require_org_context(user)
                if str(target_user.org_id) != str(org_id):
                    raise HTTPException(status_code=403, detail="Target owner is outside this org")
            if str(next_owner_id) != str(getattr(idea, "user_id", "")):
                await _freeze_display_author_for_handoff(idea, db)
            idea.user_id = str(next_owner_id)

    if "orbit_anchor_type" in updates or "orbit_anchor_id" in updates:
        next_anchor_type = updates.pop("orbit_anchor_type", idea.orbit_anchor_type)
        next_anchor_id = updates.pop("orbit_anchor_id", idea.orbit_anchor_id)
        await _apply_orbit_anchor(
            idea,
            anchor_type=next_anchor_type,
            anchor_id=str(next_anchor_id) if next_anchor_id is not None else None,
            db=db,
            user=user,
        )

    for key, value in updates.items():
        setattr(idea, key, value)
    await db.flush()
    return idea


@router.patch("/ideas/{idea_id}", response_model=IdeaRead)
async def update_idea(
    idea_id: str,
    body: IdeaUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    updates = body.model_dump(exclude_unset=True)
    idea = await _do_update_idea(idea_id, body, db, user)
    updated = await _idea_read_with_author(idea, db)
    event_org_id = _product_event_org_id(idea, user)
    await db.commit()
    await ws_manager.broadcast_product_event(
        "idea_updated",
        {"idea_id": idea_id, "fields": updates},
        org_id=event_org_id,
    )
    return updated


@router.put("/ideas/{idea_id}", response_model=IdeaRead)
async def update_idea_put(
    idea_id: str,
    body: IdeaUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    updates = body.model_dump(exclude_unset=True)
    idea = await _do_update_idea(idea_id, body, db, user)
    updated = await _idea_read_with_author(idea, db)
    event_org_id = _product_event_org_id(idea, user)
    await db.commit()
    await ws_manager.broadcast_product_event(
        "idea_updated",
        {"idea_id": idea_id, "fields": updates},
        org_id=event_org_id,
    )
    return updated


@router.delete("/ideas/{idea_id}")
async def archive_idea(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    idea = await _require_idea_for_user(db, idea_id, user)
    await transition_thought_status(
        db,
        idea=idea,
        command=ThoughtStatusCommand(
            to_status="archived",
            trigger="user_archive",
            actor=user,
        ),
    )
    archived = await _idea_read_with_author(idea, db)
    project_draft_cleanup: dict[str, Any] | None = None
    try:
        from brain.systems.cortex.project_context.draft_lifecycle import (
            apply_project_draft_cleanup_for_thread,
        )

        project_draft_cleanup = await apply_project_draft_cleanup_for_thread(
            db,
            idea_id,
            archived_at=idea.archived_at,
        )
    except Exception as exc:
        logger.warning("project_draft_archive_cleanup_failed: %s", exc)
    event_org_id = _product_event_org_id(idea, user)
    await db.commit()
    await ws_manager.broadcast_product_event(
        "idea_archived",
        {"idea_id": idea_id, "idea": archived.model_dump(mode="json")},
        org_id=event_org_id,
    )
    return {"ok": True, "project_draft_cleanup": project_draft_cleanup}


@router.post("/ideas/{idea_id}/restore", response_model=IdeaRead)
async def restore_idea(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    idea = await _require_idea_for_user(db, idea_id, user)
    await transition_thought_status(
        db,
        idea=idea,
        command=ThoughtStatusCommand(
            to_status="emerged" if str(getattr(idea, "status", "")) == "archived" else str(idea.status),
            trigger="user_restore",
            actor=user,
        ),
    )
    restored = await _idea_read_with_author(idea, db)
    event_org_id = _product_event_org_id(idea, user)
    await db.commit()
    await ws_manager.broadcast_product_event(
        "idea_restored",
        {"idea_id": idea_id, "idea": restored.model_dump(mode="json")},
        org_id=event_org_id,
    )
    return restored


@router.patch("/ideas/{idea_id}/status", response_model=IdeaRead)
async def update_idea_status(
    idea_id: str,
    body: IdeaStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    idea = await _require_idea_for_user(db, idea_id, user)
    await transition_thought_status(
        db,
        idea=idea,
        command=ThoughtStatusCommand(
            to_status=body.status,
            trigger=body.trigger,
            actor=user,
        ),
    )
    updated = await _idea_read_with_author(idea, db)
    event_org_id = _product_event_org_id(idea, user)
    await db.commit()
    await ws_manager.broadcast_product_event(
        "status_change",
        {"idea_id": idea_id, "new_status": body.status},
        org_id=event_org_id,
    )
    return updated


@router.get("/ideas/{idea_id}/threads", response_model=list[ThreadMessageRead])
async def list_threads(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _require_idea_for_user(db, idea_id, user)
    messages = await IdeaThreadRepository(db).a_list_by_idea(idea_id)
    run_ids = {
        run_id
        for run_id in (
            (
                run_id_from_public_message_metadata(message.metadata_)
                if str(message.role or "").lower() in {"illo", "assistant"}
                else None
            )
            for message in messages
        )
        if run_id is not None
    }
    failures = await public_failures_for_run_ids(db, run_ids, thread_id=idea_id)
    return [
        ThreadMessageRead.model_validate(
            _thread_message_read_payload(
                message,
                failures.get(
                    run_id_from_public_message_metadata(message.metadata_)
                    if str(message.role or "").lower() in {"illo", "assistant"}
                    else None
                ),
            )
        )
        for message in messages
    ]


@router.get("/ideas/{idea_id}/visual-blocks", response_model=list[VisualBlockRead])
async def list_visual_blocks(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return all visual blocks for an idea, ordered by creation time."""
    await _require_idea_for_user(db, idea_id, user)
    rows = await db.scalars(
        select(VisualBlock)
        .where(VisualBlock.idea_id == idea_id)
        .order_by(VisualBlock.created_at)
    )
    return rows.all()


@router.post("/ideas/{idea_id}/threads", response_model=ThreadMessageRead, status_code=201)
async def create_thread_message(
    idea_id: str,
    body: ThreadMessageCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    idea = await _require_idea_for_user(db, idea_id, user)
    result = await post_thread_message(
        db,
        idea=idea,
        command=ThreadMessageCommand(
            idea_id=idea_id,
            role=body.role,
            content=body.content,
            actor={
                "user_id": user.get("id"),
                "org_id": user.get("org_id"),
                "name": user.get("name"),
                "color": user.get("color"),
            },
        ),
    )
    message_payload = result.message_payload
    response = ThreadMessageRead.model_validate(message_payload)
    event_org_id = _product_event_org_id(idea, user)
    await db.commit()
    await ws_manager.broadcast_product_event(
        "thread_message",
        {"idea_id": idea_id, "message": message_payload},
        org_id=event_org_id,
    )
    return response


@router.get("/ideas/{idea_id}/connections", response_model=list[IdeaConnectionRead])
async def list_connections(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _require_idea_for_user(db, idea_id, user)
    return await IdeaConnectionRepository(db).a_list_by_idea(idea_id)


# ═══════════════════════════════════════════════════════════════
# Migrated endpoints (formerly raw SQL, now ORM via UnitOfWork)
# ═══════════════════════════════════════════════════════════════

@router.post("/notify")
async def notify_illo(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    """Notify Illo about a Cortex event — enqueue for run."""
    body = await request.json()
    event = body.get("event", "")
    idea_id = body.get("idea_id", "")
    thread_message = body.get("thread_message", "")
    metadata = body.get("metadata")
    priority = 1 if body.get("urgent") else 0

    from brain.app.triggers.adapters.internal import build_cortex_notify_trigger
    from brain.app.triggers.router import async_route_trigger

    if event == "idea_created":
        async with UnitOfWork() as uow:
            idea = await _require_idea_for_user(uow.session, idea_id, user)
            if idea.status in ("emerged", "queued"):
                await transition_thought_status(
                    uow.session,
                    idea=idea,
                    command=ThoughtStatusCommand(
                        to_status="active",
                        trigger="notify_idea_created",
                        actor=user,
                    ),
                )
            effective_metadata = await _effective_notify_metadata(uow.session, idea_id, metadata)
            effective_thread_message = thread_message or str(effective_metadata.get("thread_message") or "")
            if not _message_should_invoke_illo(effective_thread_message):
                return _mention_skip_response()
            trigger = build_cortex_notify_trigger(
                event="idea_created",
                idea_id=idea_id,
                idea=idea,
                user=user,
                thread_message=effective_thread_message,
                metadata=effective_metadata,
                priority=priority,
            )
            result = await async_route_trigger(trigger, session=uow.session)
            return result.to_response()

    elif event == "thread_reply":
        explicit_queue = isinstance(metadata, dict) and bool(
            metadata.get("queued_after_run") or metadata.get("queued_after_run_id")
        )
        if not explicit_queue and not _thread_reply_should_invoke_illo(thread_message):
            return _mention_skip_response()

        async with UnitOfWork() as uow:
            idea = await _require_idea_for_user(uow.session, idea_id, user)
            effective_metadata = await _effective_notify_metadata(uow.session, idea_id, metadata)
            if isinstance(metadata, dict) and metadata.get("interactive_mode"):
                effective_metadata["interactive_mode"] = metadata.get("interactive_mode")
            trigger = build_cortex_notify_trigger(
                event="thread_reply",
                idea_id=idea_id,
                idea=idea,
                user=user,
                thread_message=thread_message,
                metadata=metadata if isinstance(metadata, dict) else None,
                effective_metadata=effective_metadata if isinstance(effective_metadata, dict) else None,
                priority=priority,
            )
            result = await async_route_trigger(trigger, session=uow.session)
            return result.to_response()

    return {"ok": True}
