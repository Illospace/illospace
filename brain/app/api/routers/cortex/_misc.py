"""Cortex miscellaneous endpoints — events, connections, webhook, delegation, GPU, branches, titles, upload, audit."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, load_only

from brain.app.api.auth import get_current_user
from brain.app.api.routers.cortex._helpers import (
    ALLOWED_EXTENSIONS,
    MAX_VIDEO_UPLOAD_SIZE,
    MAX_UPLOAD_SIZE,
    UPLOAD_DIR,
    UPLOAD_FALLBACK_CONTENT_TYPES,
    VIDEO_EXTENSIONS,
    _caller_is_service_principal,
    _require_idea_for_user,
    _row_to_dict,
)
from brain.app.api.routers.cortex._router import router
from brain.systems.runs.cortex.analytics import RunAuditNotFound, build_idea_audit_summary
from brain.systems.runs.cortex import supersede_runs_for_idea
from brain.platform.db.models.agent_run import AgentRunArtifactRow
from brain.platform.db.models.run import AgentRun, CortexEvent
from brain.platform.db.models.idea import Idea, IdeaConnection, IdeaStateLog, IdeaThread
from brain.app.api.routers.ws import ws_manager
from brain.app.api.authorization import require_org_context
from brain.platform.db.repositories.ideas import IdeaConnectionRepository
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.title_generation import (
    generate_and_store_idea_display_title,
    generate_display_title,
)
from brain.systems.services.runtime_introspection import get_provider_auth_status

logger = logging.getLogger(__name__)


# ── Events logging ─────────────────────────────────────────────

@router.post("/events", status_code=201)
async def log_events(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    data = await request.json()
    events = data if isinstance(data, list) else [data]
    if not events:
        raise HTTPException(status_code=400, detail="At least one event is required")
    with UnitOfWork() as uow:
        for ev in events:
            et = ev.get("event_type")
            if not et:
                continue
            event_obj = CortexEvent(
                event_type=et,
                idea_id=ev.get("idea_id"),
                target_id=ev.get("target_id"),
                session_id=ev.get("session_id"),
                duration_ms=ev.get("duration_ms"),
                metadata_=ev.get("metadata"),
            )
            uow.session.add(event_obj)
    return {"ok": True, "count": len(events)}


# ── Connections (ORM) ──────────────────────────────────────────

def list_connections_payload(
    idea_id: str | None = None,
    *,
    db: Session,
    user: dict[str, Any],
) -> list[dict[str, Any]]:
    repo = IdeaConnectionRepository(db)
    if idea_id:
        _require_idea_for_user(db, idea_id, user)
        if _caller_is_service_principal(user):
            rows = repo.list_by_idea(idea_id)
        else:
            rows = repo.list_by_idea_for_org(idea_id, require_org_context(user))
    else:
        if _caller_is_service_principal(user):
            rows = repo.list_all_active()
        else:
            rows = repo.list_all_active_for_org(require_org_context(user))
    return [_row_to_dict(r) for r in rows]


@router.get("/connections")
def list_connections_all(idea_id: str | None = None, user: dict[str, Any] = Depends(get_current_user)):
    with UnitOfWork() as uow:
        return list_connections_payload(idea_id, db=uow.session, user=user)


@router.post("/connections", status_code=201)
async def create_connection(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    data = await request.json()
    source_id, target_id = data.get("source_id"), data.get("target_id")
    if not source_id or not target_id:
        raise HTTPException(status_code=400, detail="source_id and target_id are required")
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="Cannot connect an idea to itself")
    broadcast_org_id: str | None = None
    result: dict[str, Any] | None = None
    with UnitOfWork() as uow:
        src = _require_idea_for_user(uow.session, source_id, user, detail="One or both ideas not found")
        tgt = _require_idea_for_user(uow.session, target_id, user, detail="One or both ideas not found")
        if not src or not tgt:
            raise HTTPException(status_code=404, detail="One or both ideas not found")
        if src.org_id:
            broadcast_org_id = str(src.org_id)
        elif not _caller_is_service_principal(user):
            broadcast_org_id = require_org_context(user)
        try:
            conn = IdeaConnection(
                source_id=source_id,
                target_id=target_id,
                type=data.get("type", "manual"),
                weight=float(data.get("weight", 1.0)),
                reason=data.get("reason"),
            )
            uow.session.add(conn)
            uow.session.flush()
            result = _row_to_dict(conn)
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise HTTPException(status_code=400, detail="Connection already exists")
            raise
    await ws_manager.broadcast_product_event(
        "connection_created",
        {"connection": result},
        org_id=broadcast_org_id,
    )
    return result


@router.delete("/connections/{conn_id}")
async def delete_connection(conn_id: str, user: dict[str, Any] = Depends(get_current_user)):
    broadcast_org_id: str | None = None
    with UnitOfWork() as uow:
        if _caller_is_service_principal(user):
            conn = uow.session.get(IdeaConnection, conn_id)
        else:
            broadcast_org_id = require_org_context(user)
            conn = IdeaConnectionRepository(uow.session).get_for_org(
                conn_id,
                broadcast_org_id,
            )
        if not conn:
            raise HTTPException(status_code=404, detail=f"Connection {conn_id} not found")
        if broadcast_org_id is None:
            source = uow.session.get(Idea, conn.source_id)
            target = uow.session.get(Idea, conn.target_id)
            source_org_id = getattr(source, "org_id", None)
            target_org_id = getattr(target, "org_id", None)
            if source_org_id:
                broadcast_org_id = str(source_org_id)
            elif target_org_id:
                broadcast_org_id = str(target_org_id)
        uow.session.delete(conn)
    await ws_manager.broadcast_product_event(
        "connection_deleted",
        {"connection_id": conn_id},
        org_id=broadcast_org_id,
    )
    return {"ok": True, "id": conn_id}


# ── Webhook / Internal ─────────────────────────────────────────

@router.post("/webhook/reply")
async def webhook_reply(request: Request):
    from brain.app.api.config import INTERNAL_BEARER_TOKENS
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] not in INTERNAL_BEARER_TOKENS:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    idea_id = data.get("idea_id", "").strip()
    content = data.get("content", "").strip()
    if not idea_id:
        raise HTTPException(status_code=400, detail="idea_id is required")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    with UnitOfWork() as uow:
        idea = uow.session.get(Idea, idea_id)
        if not idea:
            raise HTTPException(status_code=404, detail=f"Idea {idea_id} not found")
        attachments_data = data.get("attachments", [])
        thread_msg = IdeaThread(
            idea_id=idea_id,
            role="illo",
            content=content,
            attachments=attachments_data,
        )
        uow.session.add(thread_msg)
        uow.session.flush()
        msg = _row_to_dict(thread_msg)
    return JSONResponse(content=msg, status_code=201)


# ── GPU health ─────────────────────────────────────────────────

@router.get("/system/gpu-server/health")
def gpu_server_health(user: dict[str, Any] = Depends(get_current_user)):
    from brain.platform.gpu_client import get_client
    try:
        health = get_client().health()
        return health
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "down", "error": str(e)})


@router.post("/system/gpu-server/workers/{worker_name}/restart")
def restart_gpu_worker(worker_name: str, user: dict[str, Any] = Depends(get_current_user)):
    """Restart a GPU server worker (embedding or llm)."""
    import httpx
    try:
        from brain.kernel.config import GPU_SERVER_URL
        # Unload then load
        httpx.post(f"{GPU_SERVER_URL}/models/{worker_name}/unload", timeout=15)
        resp = httpx.post(f"{GPU_SERVER_URL}/models/{worker_name}/load", timeout=120)
        return resp.json()
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


# ── Branch detection / splitting ───────────────────────────────

@router.post("/ideas/{idea_id}/detect-branches")
def detect_branches(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    with UnitOfWork() as uow:
        stmt = (
            select(IdeaThread.content, IdeaThread.role)
            .where(IdeaThread.idea_id == idea_id)
            .order_by(IdeaThread.created_at)
        )
        messages = uow.session.execute(stmt).all()

    if len(messages) < 8:
        return {"branches": [], "should_split": False, "reason": "Thread too short for meaningful split"}

    thread_text = "\n".join(f"[{m.role}] {m.content[:200]}" for m in messages)

    prompt = f"""Analyze this conversation thread and identify distinct sub-topics that have diverged from the original topic. Return ONLY valid JSON, no other text.

Thread:
{thread_text}

Return format:
{{"branches": [{{"topic": "short topic title", "summary": "one sentence", "message_indices": [0, 3, 5]}}], "should_split": true/false}}

Rules:
- Only suggest splitting if topics are genuinely distinct (not just related points)
- Minimum 3 messages per branch to be worth splitting
- Maximum 4 branches
- should_split = true only if 2+ distinct topics found"""

    try:
        from brain.platform.gpu_client import get_client
        content = get_client().generate(
            prompt=prompt, max_tokens=500,
            temperature=0.3, think=False, fallback_policy="auto",
        )
        parsed = json.loads(content or "{}")
        return parsed
    except Exception as e:
        logger.warning(f"Branch detection failed: {e}")
        return {"branches": [], "should_split": False, "error": str(e)}


@router.post("/ideas/{idea_id}/split")
async def split_idea(idea_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user)):
    data = await request.json()
    branches = data.get("branches", [])

    from brain.systems.cortex.events import publish

    with UnitOfWork() as uow:
        parent = uow.session.get(Idea, idea_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Idea not found")

        stmt = (
            select(IdeaThread)
            .where(IdeaThread.idea_id == idea_id)
            .order_by(IdeaThread.created_at)
        )
        all_messages = uow.session.scalars(stmt).all()

        created_ids = []
        for branch in branches:
            child_id = str(uuid.uuid4())
            angle = len(created_ids) * (2 * math.pi / max(len(branches), 1))
            offset = 120
            px = (parent.position_x or 0) + offset * math.cos(angle)
            py = (parent.position_y or 0) + offset * math.sin(angle)

            child = Idea(
                id=child_id,
                title=branch['topic'],
                status='active',
                origin='split',
                parent_id=idea_id,
                salience_score=parent.salience_score,
                position_x=px,
                position_y=py,
                user_id=user.get("id"),
                org_id=user.get("org_id"),
            )
            uow.session.add(child)

            indices = set(branch.get('message_indices', []))
            for idx, msg in enumerate(all_messages):
                if idx in indices:
                    uow.session.add(IdeaThread(
                        idea_id=child_id,
                        role=msg.role,
                        content=msg.content,
                        attachments=msg.attachments or [],
                    ))

            uow.session.add(IdeaConnection(
                id=str(uuid.uuid4()),
                source_id=idea_id,
                target_id=child_id,
                type='parent',
                weight=1.0,
                reason='Split from parent thought',
            ))

            created_ids.append(child_id)

        parent.status = 'resolved'
        parent.active_agents = 0
        uow.session.add(IdeaStateLog(
            idea_id=idea_id,
            from_state=parent.status,
            to_state='resolved',
            trigger='thought_split',
        ))

    supersede_runs_for_idea(
        idea_id,
        reason="Parent split into branches",
        producer="api.split",
    )
    publish("thought_split", {"parent_id": idea_id, "children": created_ids})
    publish("status_change", {"idea_id": idea_id, "new_status": "resolved"})
    return {"ok": True, "children": created_ids}


# ── Timeline data ──────────────────────────────────────────────

@router.get("/timeline-data")
def timeline_data(
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    with UnitOfWork() as uow:
        range_result = uow.session.execute(
            select(func.min(Idea.created_at), func.max(func.greatest(Idea.created_at, Idea.updated_at)))
        ).fetchone()
        if not range_result or not range_result[0]:
            return {"range": None, "ideas": []}
        range_start = range_result[0].isoformat() if isinstance(range_result[0], datetime) else range_result[0]
        range_end = range_result[1].isoformat() if isinstance(range_result[1], datetime) else range_result[1]

        idea_ids: list[str] | None = None
        if limit is not None:
            idea_ids = list(
                uow.session.scalars(
                    select(Idea.id)
                    .order_by(Idea.created_at.desc())
                    .limit(limit)
                ).all()
            )
            if not idea_ids:
                return {"range": {"start": range_start, "end": range_end}, "ideas": []}

        stmt = (
            select(Idea)
            .options(load_only(
                Idea.id,
                Idea.title,
                Idea.display_title,
                Idea.status,
                Idea.origin,
                Idea.salience_score,
                Idea.created_at,
                Idea.archived_at,
                Idea.position_x,
                Idea.position_y,
            ))
            .order_by(Idea.created_at)
        )
        if idea_ids is not None:
            stmt = stmt.where(Idea.id.in_(idea_ids))
        ideas_raw = uow.session.scalars(stmt).all()

        stmt = (
            select(IdeaStateLog)
            .options(load_only(
                IdeaStateLog.idea_id,
                IdeaStateLog.to_state,
                IdeaStateLog.changed_at,
                IdeaStateLog.trigger,
            ))
            .order_by(IdeaStateLog.changed_at)
        )
        if idea_ids is not None:
            stmt = stmt.where(IdeaStateLog.idea_id.in_(idea_ids))
        transitions_raw = uow.session.scalars(stmt).all()

        trans_by_idea = {}
        for t in transitions_raw:
            iid = str(t.idea_id)
            if iid not in trans_by_idea:
                trans_by_idea[iid] = []
            trans_by_idea[iid].append({
                "to_state": t.to_state,
                "at": t.changed_at.isoformat() if isinstance(t.changed_at, datetime) else t.changed_at,
                "trigger": t.trigger,
            })

        ideas = []
        for r in ideas_raw:
            iid = str(r.id)
            ideas.append({
                "id": iid,
                "title": r.title,
                "display_title": r.display_title,
                "status": r.status,
                "origin": r.origin,
                "salience_score": r.salience_score,
                "created_at": r.created_at.isoformat() if isinstance(r.created_at, datetime) else r.created_at,
                "archived_at": r.archived_at.isoformat() if isinstance(r.archived_at, datetime) else None,
                "position_x": r.position_x,
                "position_y": r.position_y,
                "transitions": trans_by_idea.get(iid, []),
            })

        return {"range": {"start": range_start, "end": range_end}, "ideas": ideas}


# ── Auth status ────────────────────────────────────────────────

@router.get("/auth/status")
def auth_status(
    user: dict[str, Any] = Depends(get_current_user),
    provider: str | None = None,
):
    """Provider-specific auth status for the active runtime."""
    from brain.platform.providers.model_policy import resolve_default_provider

    provider = (
        provider
        or resolve_default_provider(user_id=user.get("id"), org_id=user.get("org_id"))
    ).strip().lower()
    if provider not in {"anthropic", "openai"}:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")

    user_id = user.get("id")
    org_id = user.get("org_id")
    return get_provider_auth_status(user_id=user_id, org_id=org_id, provider=provider)


# ── Upload ─────────────────────────────────────────────────────

@router.post("/upload")
async def upload_file(file: UploadFile = File(...), user: dict[str, Any] = Depends(get_current_user)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Empty filename")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type .{ext} not allowed")
    data = await file.read()
    max_size = MAX_VIDEO_UPLOAD_SIZE if ext in VIDEO_EXTENSIONS else MAX_UPLOAD_SIZE
    if len(data) > max_size:
        raise HTTPException(status_code=400, detail=f"File too large ({len(data)} bytes)")
    filename = f"{uuid.uuid4().hex}.{ext}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / filename).write_bytes(data)
    fallback_type = UPLOAD_FALLBACK_CONTENT_TYPES.get(ext, "application/octet-stream")
    content_type = file.content_type if file.content_type and file.content_type != "application/octet-stream" else fallback_type
    return {
        "url": f"/static/uploads/{filename}",
        "filename": file.filename,
        "type": content_type,
        "size": len(data),
    }


@router.post("/generate-title")
async def generate_title(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    data = await request.json()
    text_content = data.get("text", "").strip()
    if not text_content:
        raise HTTPException(status_code=400, detail="text is required")
    title = generate_display_title(
        text_content,
        user_id=user.get("id"),
        org_id=user.get("org_id"),
    )
    if not title:
        raise HTTPException(status_code=400, detail="Title generation failed")
    return {"title": title}


@router.post("/backfill-titles")
def backfill_titles(user: dict[str, Any] = Depends(get_current_user)):
    org_id = user.get("org_id")
    user_id = user.get("id")
    with UnitOfWork() as uow:
        stmt = select(Idea).where(Idea.display_title.is_(None), Idea.archived_at.is_(None))
        if org_id:
            stmt = stmt.where(Idea.org_id == org_id)
        elif user_id:
            stmt = stmt.where(Idea.user_id == user_id)
        rows = uow.session.scalars(stmt).all()
        # Collect id/title before leaving the session
        ideas_to_process = [(r.id, r.title) for r in rows]

    count = 0
    for idea_id, idea_title in ideas_to_process:
        result = generate_and_store_idea_display_title(
            str(idea_id),
            user_id=user_id,
            org_id=org_id,
            raw_title=idea_title,
        )
        if result.updated:
            count += 1
    return {"ok": True, "generated": count, "total": len(ideas_to_process)}


# ── Audit endpoints ───────────────────────────────────────────


@router.get("/ideas/{idea_id}/audit")
def idea_audit(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    """Aggregate metrics for ALL runs on an idea — pure SQL, no LLM."""
    with UnitOfWork() as uow:
        try:
            return build_idea_audit_summary(uow.session, idea_id)
        except RunAuditNotFound:
            raise HTTPException(status_code=404, detail="No runs for this idea")


@router.post("/ideas/{idea_id}/audit/analyze")
async def idea_audit_analyze(
    idea_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Trigger a self-critique run on the idea's conversation audit."""
    from brain.systems.runs.cortex import RunAdmissionRequest, admit_run

    # Build a metrics summary to include in the run message
    with UnitOfWork() as uow:
        runs = uow.session.scalars(
            select(AgentRun)
            .where(AgentRun.thread_id == idea_id)
            .order_by(AgentRun.started_at.asc().nullslast())
        ).all()
        if not runs:
            raise HTTPException(status_code=404, detail="No runs for this idea")

        total_cost = 0.0
        total_tokens = 0
        skills_used = set()
        failed = sum(1 for d in runs if d.status == "failed")
        all_misses = []
        for d in runs:
            metadata = d.metadata_ if isinstance(d.metadata_, dict) else {}
            usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
            routing = metadata.get("routing") if isinstance(metadata.get("routing"), dict) else {}
            total_cost += float(usage.get("estimated_cost") or 0)
            total_tokens += int(usage.get("tokens_total") or 0)
            if routing.get("selected_skill"):
                skills_used.add(routing["selected_skill"])
            misses = metadata.get("cognitive_misses")
            if isinstance(misses, list):
                all_misses.extend(misses)

        # Get thread messages for context
        thread_rows = uow.session.execute(
            text("""
                SELECT role, content FROM idea_threads
                WHERE idea_id = :idea_id
                ORDER BY created_at ASC
                LIMIT 50
            """),
            {"idea_id": idea_id},
        ).fetchall()
        thread_text = "\n".join(
            f"[{r.role}] {r.content[:500]}" for r in thread_rows
        )

        worker_lines: list[str] = []
        worker_text = "  (worker data is available in run events/artifacts)"

    summary = (
        f"AUDIT SUMMARY for idea {idea_id}:\n"
        f"- {len(runs)} runs, {failed} failed\n"
        f"- Total tokens: {total_tokens:,}, Est cost: ${total_cost:.4f}\n"
        f"- Skills used: {', '.join(skills_used) or 'none'}\n"
        f"- Cognitive misses: {len(all_misses)} ({', '.join(all_misses[:5])})\n"
        f"- Workers spawned: {len(worker_lines)}\n"
        f"{worker_text}\n\n"
        f"CONVERSATION THREAD:\n{thread_text}\n\n"
        f"Please analyze this conversation for:\n"
        f"1. Wasted tokens or unnecessary runs\n"
        f"2. Skill mismatches (wrong skill chosen)\n"
        f"3. Worker efficiency — are workers too expensive or failing?\n"
        f"4. Patterns that should become lessons or guardrails\n"
        f"5. Concrete proposals: encode_lesson, add_guardrail, update_skill, or propose_code"
    )

    uid = user.get("id")
    # Force the audit skill via /audit prefix
    admission = admit_run(
        RunAdmissionRequest(
            idea_id=idea_id,
            event="audit_analyze",
            message=f"/audit {summary}",
            priority=2,
            user_id=uid,
            metadata={"run_profile": "deep", "recipe": "deep", "source": "audit"},
        )
    )

    if not admission.ok or admission.run_id is None:
        raise HTTPException(
            status_code=409,
            detail="Idea already has an active run — wait or cancel it first",
        )

    return {"ok": True, "run_id": admission.run_id}


@router.get("/ideas/{idea_id}/audit/analysis-result")
async def idea_audit_analysis_result(
    idea_id: str,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Fetch the latest self-critique analysis result for an idea.

    Returns the run status and, if completed, the thread message content
    associated with the audit_analyze run.
    """
    with UnitOfWork() as uow:
        # Find the latest audit_analyze run for this idea
        d = uow.session.scalars(
            select(AgentRun)
            .where(
                AgentRun.thread_id == idea_id,
                AgentRun.input_message.like("/audit%"),
            )
            .order_by(AgentRun.created_at.desc())
            .limit(1)
        ).first()

        if not d:
            return {"found": False}

        result: dict[str, Any] = {
            "found": True,
            "run_id": d.id,
            "status": d.status,
            "started_at": d.started_at.isoformat() if d.started_at else None,
            "completed_at": d.completed_at.isoformat() if d.completed_at else None,
            "error": (d.metadata_ or {}).get("error") if isinstance(d.metadata_, dict) else None,
        }

        if d.status in ("completed", "failed"):
            # Look for the thread message posted by this run via metadata.run_id
            msg = uow.session.scalars(
                select(IdeaThread)
                .where(
                    IdeaThread.idea_id == idea_id,
                    IdeaThread.role == "illo",
                    text("metadata->>'run_id' = :did"),
                )
                .params(did=str(d.id))
                .order_by(IdeaThread.created_at.desc())
                .limit(1)
            ).first()

            if msg:
                result["content"] = msg.content
                result["message_id"] = msg.id
            else:
                # Fallback: grab the most recent illo message near run completion
                fallback = uow.session.scalars(
                    select(IdeaThread)
                    .where(
                        IdeaThread.idea_id == idea_id,
                        IdeaThread.role == "illo",
                    )
                    .order_by(IdeaThread.created_at.desc())
                    .limit(1)
                ).first()
                if fallback and d.completed_at and d.started_at and fallback.created_at >= d.started_at:
                    result["content"] = fallback.content
                    result["message_id"] = fallback.id

        return result


@router.post("/audit/apply")
async def audit_apply(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Apply an audit proposal: encode_lesson, add_guardrail, update_skill, or propose_code."""
    body = await request.json()
    action_type = body.get("type")
    payload = body.get("payload", {})

    if action_type not in ("encode_lesson", "add_guardrail", "update_skill", "propose_code"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type '{action_type}'. Must be one of: encode_lesson, add_guardrail, update_skill, propose_code",
        )

    if action_type == "encode_lesson":
        content = payload.get("content")
        if not content or len(content) < 20:
            raise HTTPException(status_code=400, detail="Lesson content must be >= 20 chars")
        from brain.app.cli.memory import add_memory
        from brain.platform.db.repositories.memory_write_context import MemoryWriteContext
        org_id = user.get("org_id")
        write_context = MemoryWriteContext(
            user_id=user["id"],
            org_id=org_id,
            visibility="org" if org_id else "private",
            source="audit",
            confidence=payload.get("confidence"),
            evidence={"audit_action_payload": payload},
        )
        result = add_memory(
            content=content,
            memory_type=payload.get("memory_type", "lesson"),
            salience=payload.get("salience", 7.0),
            source="audit",
            write_context=write_context,
        )
        if result.get("rejected"):
            raise HTTPException(status_code=422, detail=f"Memory rejected: {result.get('reason')}")
        return {"ok": True, "action": "encode_lesson", "memory_id": result.get("id")}

    elif action_type == "add_guardrail":
        skill_name = payload.get("skill_name")
        text_val = payload.get("text")
        severity = payload.get("severity", "medium")
        if not skill_name or not text_val:
            raise HTTPException(status_code=400, detail="skill_name and text are required")
        from brain.platform.db.repositories.skills import SkillRepository
        with UnitOfWork() as uow:
            repo = SkillRepository(uow.session)
            try:
                repo.add_guardrail(skill_name, text_val, severity)
            except LookupError:
                raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")
        return {"ok": True, "action": "add_guardrail", "skill": skill_name}

    elif action_type == "update_skill":
        skill_name = payload.get("skill_name")
        new_procedure = payload.get("procedure")
        if not skill_name or not new_procedure:
            raise HTTPException(status_code=400, detail="skill_name and procedure are required")
        from brain.platform.db.repositories.skills import SkillRepository
        with UnitOfWork() as uow:
            repo = SkillRepository(uow.session)
            try:
                skill = repo.get_by_name_or_raise(skill_name)
                repo.update_full(skill.id, procedure=new_procedure)
            except LookupError:
                raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")
        return {"ok": True, "action": "update_skill", "skill": skill_name}

    elif action_type == "propose_code":
        title = payload.get("title")
        body_text = payload.get("body", "")
        if not title:
            raise HTTPException(status_code=400, detail="title is required for propose_code")
        repo = os.environ.get("ILLO_GITHUB_REPO", "").strip()
        if not repo:
            raise HTTPException(
                status_code=400,
                detail="ILLO_GITHUB_REPO must be configured before propose_code can create issues",
            )
        import subprocess
        result = subprocess.run(
            ["gh", "issue", "create",
             "--repo", repo,
             "--title", title,
             "--body", body_text,
             "--label", "audit-proposal"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create issue: {result.stderr.strip()}",
            )
        issue_url = result.stdout.strip()
        return {"ok": True, "action": "propose_code", "issue_url": issue_url}


@router.post("/audit/eval")
async def audit_eval(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    """
    Given an audit proposal, evaluate it against 3 recent benchmarks
    using a counterfactual LLM judge.
    Returns: per-benchmark scores + aggregate.
    """
    body = await request.json()
    proposal = body.get("proposal", body)

    proposal_type = proposal.get("type", "unknown")
    proposal_desc = proposal.get("description", "")
    proposal_rec = proposal.get("recommendation", "")
    skill_name = proposal.get("skill_name") or proposal.get("payload", {}).get("skill_name")

    # Fetch 3 recent completed runs with output_artifact
    with UnitOfWork() as uow:
        stmt = (
            select(AgentRun, AgentRunArtifactRow)
            .join(AgentRunArtifactRow, AgentRunArtifactRow.run_id == AgentRun.id)
            .where(
                AgentRun.status == "completed",
                AgentRunArtifactRow.artifact_type == "final_answer",
                AgentRunArtifactRow.text.isnot(None),
            )
            .order_by(AgentRun.completed_at.desc().nullslast(), AgentRunArtifactRow.created_at.desc())
            .limit(12)
        )
        rows = uow.session.execute(stmt).all()
        benchmarks = []
        for run, artifact in rows:
            metadata = run.metadata_ if isinstance(run.metadata_, dict) else {}
            routing = metadata.get("routing") if isinstance(metadata.get("routing"), dict) else {}
            if skill_name and routing.get("selected_skill") != skill_name:
                continue
            benchmarks.append((run, artifact))
            if len(benchmarks) >= 3:
                break

        if not benchmarks:
            raise HTTPException(
                status_code=404,
                detail="No completed runs with output artifacts found for evaluation",
            )

        benchmark_data = []
        for d, artifact in benchmarks:
            metadata = d.metadata_ if isinstance(d.metadata_, dict) else {}
            routing = metadata.get("routing") if isinstance(metadata.get("routing"), dict) else {}
            usage = metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}
            benchmark_data.append({
                "run_id": d.id,
                "task_summary": d.input_message or routing.get("selected_skill") or "unknown",
                "output_artifact": (artifact.text or "")[:2000],
                "tokens_total": usage.get("tokens_total") or 0,
                "attempts": usage.get("attempts") or 1,
                "skill_used": routing.get("selected_skill"),
            })

    # Judge each benchmark with a provider-neutral text completion.
    results = []

    for bm in benchmark_data:
        judge_prompt = f"""You are evaluating whether a proposed agent skill change would have improved a past run.

TASK SUMMARY:
{bm['task_summary']}

ACTUAL OUTPUT PRODUCED:
{bm['output_artifact']}

ACTUAL COST: {bm['tokens_total']} tokens · {bm['attempts']} attempts

PROPOSED CHANGE ({proposal_type}):
{proposal_desc}
{proposal_rec}

EVALUATE:
1. Did the actual output fully solve the task on the first try? (yes/partial/no)
2. Would this change have made the output more complete or correct? (yes/no/neutral)
3. Would this change have reduced token waste? (yes/no/neutral)
4. Risk: could this change cause a regression? (high/medium/low)

Score 0-10 (10 = clear improvement, 0 = clear regression).
Respond as JSON only: {{"score": N, "task_solved": "yes|partial|no", "output_better": "yes|no|neutral", "less_waste": "yes|no|neutral", "regression_risk": "high|medium|low", "reasoning": "one sentence"}}"""

        try:
            from brain.platform.integrations.completions import simple_text_completion
            from brain.platform.providers.model_policy import get_model_for_tier

            raw = simple_text_completion(
                judge_prompt,
                model=get_model_for_tier("low", include_provider_prefix=True),
                max_tokens=300,
            ) or ""
            # Extract JSON from response
            json_match = re.search(r'\{[^}]+\}', raw, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
            else:
                parsed = json.loads(raw)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Judge parse failed for run #{bm['run_id']}: {e}")
            parsed = {
                "score": 5,
                "task_solved": "partial",
                "output_better": "neutral",
                "less_waste": "neutral",
                "regression_risk": "medium",
                "reasoning": f"Judge evaluation failed: {str(e)[:100]}",
            }

        results.append({
            "run_id": bm["run_id"],
            "task_summary": bm["task_summary"],
            "tokens": bm["tokens_total"],
            "attempts": bm["attempts"],
            "score": parsed.get("score", 5),
            "task_solved": parsed.get("task_solved", "partial"),
            "output_better": parsed.get("output_better", "neutral"),
            "less_waste": parsed.get("less_waste", "neutral"),
            "regression_risk": parsed.get("regression_risk", "medium"),
            "reasoning": parsed.get("reasoning", ""),
        })

    avg_score = sum(r["score"] for r in results) / len(results) if results else 0
    if avg_score >= 6:
        recommendation = "apply"
    elif avg_score >= 3:
        recommendation = "caution"
    else:
        recommendation = "reject"

    return {
        "benchmarks": results,
        "avg_score": round(avg_score, 1),
        "recommendation": recommendation,
    }
