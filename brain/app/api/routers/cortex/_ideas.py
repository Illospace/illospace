"""Cortex ideas CRUD and notification endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.app.mentions import (
    TEAM_MENTION_WITHOUT_ILLO_SKIP_REASON,
    classify_mention_intent,
)
from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db
from brain.app.api.db_utils import run_db
from brain.app.api.routers.cortex._helpers import (
    _caller_is_service_principal,
    _require_idea_for_user,
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
from brain.platform.db.models.idea import IdeaStateLog, IdeaThread, VisualBlock
from brain.platform.db.models.org import User
from brain.platform.db.models.workspace_pin import WorkspacePin
from brain.platform.db.repositories.ideas import (
    IdeaConnectionRepository,
    IdeaRepository,
    IdeaThreadRepository,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.title_generation import generate_and_store_idea_display_title


async def _run_db(db: AsyncSession, fn, /, *args, **kwargs):
    def _sync(sync_db: Session):
        return fn(sync_db, *args, **kwargs)

    return await run_db(db, _sync)


def _last_human_thread_author(idea_id: str, db: Session):
    return db.execute(
        select(User.name, User.color)
        .join(IdeaThread, IdeaThread.user_id == User.id)
        .where(IdeaThread.idea_id == idea_id)
        .where(IdeaThread.role == "user")
        .where(IdeaThread.user_id.is_not(None))
        .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
        .limit(1)
    ).one_or_none()


def _fallback_display_author_id(idea) -> str | None:
    details = getattr(idea, "agent_details", None)
    if isinstance(details, dict):
        author_id = details.get("display_author_user_id")
        if author_id:
            return str(author_id)
    owner_id = getattr(idea, "user_id", None)
    return str(owner_id) if owner_id else None


def _user_author_row(db: Session, user_id: str | None):
    if not user_id:
        return None
    return db.execute(
        select(User.name, User.color)
        .where(User.id == str(user_id))
        .limit(1)
    ).one_or_none()


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


def _product_event_org_id(idea, user: dict[str, Any]) -> str | None:
    org_id = getattr(idea, "org_id", None)
    if org_id:
        return str(org_id)
    if _caller_is_service_principal(user):
        return None
    return require_org_context(user)


def _freeze_display_author_for_handoff(idea, db: Session) -> None:
    """Keep the pre-handoff display author when no thread author exists yet."""
    if _last_human_thread_author(idea.id, db) is not None:
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


def _latest_user_thread_metadata(db: Session, idea_id: str) -> dict[str, Any]:
    latest = db.execute(
        select(IdeaThread.metadata_)
        .where(IdeaThread.idea_id == idea_id, IdeaThread.role == "user")
        .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return dict(latest or {}) if isinstance(latest, dict) else {}


def _effective_notify_metadata(db: Session, idea_id: str, metadata: Any) -> dict[str, Any]:
    """Merge the freshly persisted thread metadata into run metadata.

    The composer stores attachment-derived Project Context on the thread first;
    /notify may then send only execution-profile metadata. Keep the thread's
    resource snapshot unless the caller explicitly sends a replacement.
    """
    effective = _latest_user_thread_metadata(db, idea_id)
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if value is not None:
                effective[key] = value
    return effective


def _author_hints_for_ideas(ideas: list[Any], db: Session) -> dict[str, dict[str, str | None]]:
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
    author_rows = db.execute(
        select(
            ranked_authors.c.idea_id,
            ranked_authors.c.author_name,
            ranked_authors.c.author_color,
        ).where(ranked_authors.c.rank == 1)
    ).all()

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
        user_rows = db.execute(
            select(User.id, User.name, User.color).where(User.id.in_(fallback_user_ids))
        ).all()
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


def _idea_read_with_author(
    idea,
    db: Session,
    *,
    author_hint: dict[str, str | None] | None = None,
    author_hint_loaded: bool = False,
) -> IdeaRead:
    """Serialize display color from the last human interaction.

    The persisted owner/orbit target is still idea.user_id. author_color is a
    presentation hint and intentionally can differ from owner after a handoff.
    """
    payload = IdeaRead.model_validate(idea).model_dump()
    payload["project_context"] = _project_context_for_idea(idea)
    if author_hint_loaded:
        _apply_author_hint(payload, author_hint)
        return IdeaRead.model_validate(payload)

    last_author = _last_human_thread_author(idea.id, db)
    if last_author is not None:
        author_name = getattr(last_author, "name", None)
        author_color = getattr(last_author, "color", None)
        if isinstance(author_name, str):
            payload["author_name"] = author_name
        if isinstance(author_color, str):
            payload["author_color"] = author_color
    else:
        owner = _user_author_row(db, _fallback_display_author_id(idea))
        if owner is not None:
            owner_name = getattr(owner, "name", None)
            owner_color = getattr(owner, "color", None)
            if isinstance(owner_name, str):
                payload["author_name"] = owner_name
            if isinstance(owner_color, str):
                payload["author_color"] = owner_color
    return IdeaRead.model_validate(payload)


def _ideas_read_with_author(ideas, db: Session) -> list[IdeaRead]:
    idea_list = list(ideas)
    author_hints = _author_hints_for_ideas(idea_list, db)
    return [
        _idea_read_with_author(
            idea,
            db,
            author_hint=author_hints.get(str(getattr(idea, "id", ""))),
            author_hint_loaded=True,
        )
        for idea in idea_list
    ]


def _apply_orbit_anchor(
    idea,
    *,
    anchor_type: str | None,
    anchor_id: str | None,
    db: Session,
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
            target_user = db.scalar(select(User).where(User.id == normalized_id))
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
        pin = db.scalar(stmt)
        if pin is None:
            raise HTTPException(status_code=404, detail="Pin orbit anchor not found")
        idea.orbit_anchor_type = "pin"
        idea.orbit_anchor_id = normalized_id
        return

    raise HTTPException(status_code=400, detail="Unsupported orbit anchor type")

# ═══════════════════════════════════════════════════════════════
# ORM-based CRUD endpoints (existing — kept as-is)
# ═══════════════════════════════════════════════════════════════

def list_ideas_payload(
    status: str | None = None,
    db: Session | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> list[IdeaRead]:
    assert db is not None, "list_ideas_payload requires an explicit sync session"
    repo = IdeaRepository(db)
    if _caller_is_service_principal(user):
        ideas = repo.list_by_status(status) if status else repo.list_active()
        return _ideas_read_with_author(list(ideas), db)

    org_id = require_org_context(user)
    ideas = repo.list_by_status_for_org(status, org_id) if status else repo.list_active_for_org(org_id)
    return _ideas_read_with_author(list(ideas), db)


@router.get("/ideas", response_model=list[IdeaRead])
async def list_ideas(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await _run_db(
        db,
        lambda sync_db: list_ideas_payload(status=status, db=sync_db, user=user),
    )


@router.get("/ideas/archived", response_model=list[IdeaRead])
async def list_archived_ideas(
    limit: int = 12,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session) -> list[IdeaRead]:
        capped_limit = max(1, min(int(limit or 12), 50))
        repo = IdeaRepository(sync_db)
        if _caller_is_service_principal(user):
            ideas = repo.list_archived(limit=capped_limit)
            return _ideas_read_with_author(list(ideas), sync_db)

        org_id = require_org_context(user)
        ideas = repo.list_archived_for_org(org_id, limit=capped_limit)
        return _ideas_read_with_author(list(ideas), sync_db)

    return await _run_db(db, _list)


@router.delete("/ideas/archived")
async def empty_archived_ideas(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _delete(sync_db: Session) -> tuple[int, str | None]:
        repo = IdeaRepository(sync_db)
        if _caller_is_service_principal(user):
            deleted = repo.hard_delete_archived()
            sync_db.commit()
            return deleted, None

        org_id = require_org_context(user)
        deleted = repo.hard_delete_archived_for_org(org_id)
        sync_db.commit()
        return deleted, str(org_id)

    deleted, event_org_id = await _run_db(db, _delete)
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
    def _create(sync_db: Session):
        repo = IdeaRepository(sync_db)
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
        idea = repo.create(**kwargs)
        if body.orbit_anchor_type is not None or body.orbit_anchor_id is not None:
            _apply_orbit_anchor(
                idea,
                anchor_type=body.orbit_anchor_type,
                anchor_id=body.orbit_anchor_id,
                db=sync_db,
                user=user,
            )
        sync_db.flush()
        read = _idea_read_with_author(idea, sync_db)
        sync_db.commit()
        return read, {
            "idea_id": str(idea.id),
            "title": idea.title,
            "user_id": str(user.get("id")) if user.get("id") else None,
            "org_id": str(org_id) if org_id else None,
            "event_org_id": _product_event_org_id(idea, user),
        }

    created, event = await _run_db(db, _create)
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
async def update_positions_batch(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    """Batch update idea positions (called when D3 simulation settles)."""
    data = await request.json()
    positions = data.get("positions", [])
    if not positions:
        return {"ok": True, "updated": 0}
    async with UnitOfWork() as uow:
        org_id = None if _caller_is_service_principal(user) else require_org_context(user)

        def _update(sync_db: Session) -> int:
            updated = 0
            repo = IdeaRepository(sync_db)
            for p in positions:
                idea_id = p.get("id")
                if not idea_id:
                    continue
                idea = repo.get(idea_id) if org_id is None else repo.get_for_org(idea_id, org_id)
                if idea and idea.archived_at is None:
                    idea.position_x = p.get("x")
                    idea.position_y = p.get("y")
                    updated += 1
            return updated

        updated = await run_db(uow.session, _update)
    return {"ok": True, "updated": updated}


@router.get("/ideas/{idea_id}", response_model=IdeaRead)
async def get_idea(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await _run_db(
        db,
        lambda sync_db: _idea_read_with_author(
            _require_idea_for_user(sync_db, idea_id, user),
            sync_db,
        ),
    )


def _do_update_idea(
    idea_id: str,
    body: IdeaUpdate,
    db: Session,
    user: dict[str, Any],
):
    idea = _require_idea_for_user(db, idea_id, user)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    # Handle status change with state logging
    if "status" in updates:
        old_status = idea.status
        new_status = updates.pop("status")
        idea.status = new_status
        if new_status == "archived":
            idea.archived_at = datetime.now(timezone.utc)
        if old_status != new_status:
            log = IdeaStateLog(
                idea_id=idea_id,
                from_state=old_status,
                to_state=new_status,
            )
            db.add(log)
    # user_id is the Cortex owner. A handoff must stay inside the caller org and
    # must not implicitly archive, move orbit anchors, or recolor the thread.
    if "user_id" in updates:
        next_owner_id = updates.pop("user_id")
        if next_owner_id is not None:
            target_user = db.scalar(select(User).where(User.id == str(next_owner_id)))
            if target_user is None:
                raise HTTPException(status_code=404, detail="Target owner not found")
            if not _caller_is_service_principal(user):
                org_id = require_org_context(user)
                if str(target_user.org_id) != str(org_id):
                    raise HTTPException(status_code=403, detail="Target owner is outside this org")
            if str(next_owner_id) != str(getattr(idea, "user_id", "")):
                _freeze_display_author_for_handoff(idea, db)
            idea.user_id = str(next_owner_id)

    if "orbit_anchor_type" in updates or "orbit_anchor_id" in updates:
        next_anchor_type = updates.pop("orbit_anchor_type", idea.orbit_anchor_type)
        next_anchor_id = updates.pop("orbit_anchor_id", idea.orbit_anchor_id)
        _apply_orbit_anchor(
            idea,
            anchor_type=next_anchor_type,
            anchor_id=str(next_anchor_id) if next_anchor_id is not None else None,
            db=db,
            user=user,
        )

    for key, value in updates.items():
        setattr(idea, key, value)
    db.flush()
    return idea


@router.patch("/ideas/{idea_id}", response_model=IdeaRead)
async def update_idea(
    idea_id: str,
    body: IdeaUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    updates = body.model_dump(exclude_unset=True)
    def _update(sync_db: Session):
        idea = _do_update_idea(idea_id, body, sync_db, user)
        read = _idea_read_with_author(idea, sync_db)
        event_org_id = _product_event_org_id(idea, user)
        sync_db.commit()
        return read, event_org_id

    updated, event_org_id = await _run_db(db, _update)
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
    def _update(sync_db: Session):
        idea = _do_update_idea(idea_id, body, sync_db, user)
        read = _idea_read_with_author(idea, sync_db)
        event_org_id = _product_event_org_id(idea, user)
        sync_db.commit()
        return read, event_org_id

    updated, event_org_id = await _run_db(db, _update)
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
    def _archive(sync_db: Session):
        idea = _require_idea_for_user(sync_db, idea_id, user)
        old_status = idea.status
        idea.archived_at = datetime.now(timezone.utc)
        idea.status = "archived"
        sync_db.add(IdeaStateLog(idea_id=idea_id, from_state=old_status, to_state="archived", trigger="user_archive"))
        sync_db.flush()
        archived = _idea_read_with_author(idea, sync_db)
        event_org_id = _product_event_org_id(idea, user)
        sync_db.commit()
        return archived, event_org_id

    archived, event_org_id = await _run_db(db, _archive)
    await ws_manager.broadcast_product_event(
        "idea_archived",
        {"idea_id": idea_id, "idea": archived.model_dump(mode="json")},
        org_id=event_org_id,
    )
    return {"ok": True}


@router.post("/ideas/{idea_id}/restore", response_model=IdeaRead)
async def restore_idea(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _restore(sync_db: Session):
        idea = _require_idea_for_user(sync_db, idea_id, user)
        old_status = idea.status
        idea.archived_at = None
        if old_status == "archived":
            idea.status = "emerged"
        sync_db.add(IdeaStateLog(idea_id=idea_id, from_state=old_status, to_state=idea.status, trigger="user_restore"))
        sync_db.flush()
        restored = _idea_read_with_author(idea, sync_db)
        event_org_id = _product_event_org_id(idea, user)
        sync_db.commit()
        return restored, event_org_id

    restored, event_org_id = await _run_db(db, _restore)
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
    def _status(sync_db: Session):
        idea = _require_idea_for_user(sync_db, idea_id, user)
        old_status = idea.status
        idea.status = body.status
        sync_db.add(IdeaStateLog(
            idea_id=idea_id,
            from_state=old_status,
            to_state=body.status,
            trigger=body.trigger,
        ))
        read = _idea_read_with_author(idea, sync_db)
        event_org_id = _product_event_org_id(idea, user)
        sync_db.commit()
        return read, event_org_id

    updated, event_org_id = await _run_db(db, _status)
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
    def _list(sync_db: Session):
        _require_idea_for_user(sync_db, idea_id, user)
        repo = IdeaThreadRepository(sync_db)
        return repo.list_by_idea(idea_id)

    return await _run_db(db, _list)


@router.get("/ideas/{idea_id}/visual-blocks", response_model=list[VisualBlockRead])
async def list_visual_blocks(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return all visual blocks for an idea, ordered by creation time."""
    def _list(sync_db: Session):
        _require_idea_for_user(sync_db, idea_id, user)
        return sync_db.scalars(
            select(VisualBlock)
            .where(VisualBlock.idea_id == idea_id)
            .order_by(VisualBlock.created_at)
        ).all()

    return await _run_db(db, _list)


@router.post("/ideas/{idea_id}/threads", response_model=ThreadMessageRead, status_code=201)
async def create_thread_message(
    idea_id: str,
    body: ThreadMessageCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _create(sync_db: Session):
        idea = _require_idea_for_user(sync_db, idea_id, user)
        repo = IdeaThreadRepository(sync_db)
        msg = repo.add_message(
            idea_id=idea_id,
            role=body.role,
            content=body.content,
            user_id=user["id"],
        )
        sync_db.flush()
        message_payload = {
            "id": str(msg.id),
            "idea_id": idea_id,
            "role": msg.role,
            "content": msg.content,
            "user_id": str(msg.user_id) if msg.user_id is not None else None,
            "attachments": msg.attachments or [],
            "message_type": getattr(msg, "message_type", None),
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        }
        response = ThreadMessageRead.model_validate(msg)
        event_org_id = _product_event_org_id(idea, user)
        sync_db.commit()
        return response, message_payload, event_org_id

    msg, message_payload, event_org_id = await _run_db(db, _create)
    await ws_manager.broadcast_product_event(
        "thread_message",
        {"idea_id": idea_id, "message": message_payload},
        org_id=event_org_id,
    )
    return msg


@router.get("/ideas/{idea_id}/connections", response_model=list[IdeaConnectionRead])
async def list_connections(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session):
        _require_idea_for_user(sync_db, idea_id, user)
        repo = IdeaConnectionRepository(sync_db)
        return repo.list_by_idea(idea_id)

    return await _run_db(db, _list)


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
    from brain.app.triggers.router import route_trigger

    if event == "idea_created":
        async with UnitOfWork() as uow:
            def _route(sync_db: Session):
                idea = _require_idea_for_user(sync_db, idea_id, user)
                if idea.status in ("emerged", "queued"):
                    idea.status = "active"
                effective_metadata = _effective_notify_metadata(sync_db, idea_id, metadata)
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
                result = route_trigger(trigger, session=sync_db)
                return result.to_response()

            return await run_db(uow.session, _route)

    elif event == "thread_reply":
        explicit_queue = isinstance(metadata, dict) and bool(
            metadata.get("queued_after_run") or metadata.get("queued_after_run_id")
        )
        if not explicit_queue and not _thread_reply_should_invoke_illo(thread_message):
            return _mention_skip_response()

        async with UnitOfWork() as uow:
            def _route(sync_db: Session):
                idea = _require_idea_for_user(sync_db, idea_id, user)
                effective_metadata = _effective_notify_metadata(sync_db, idea_id, metadata)
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
                result = route_trigger(trigger, session=sync_db)
                return result.to_response()

            return await run_db(uow.session, _route)

    return {"ok": True}
