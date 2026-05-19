"""Cortex helpers — shared utilities, constants, and presence tracking."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, text

from brain.app.mentions import extract_mention_token_list
from brain.app.api.authorization import can_manage_run, require_org_context
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User
from brain.platform.db.repositories.ideas import IdeaRepository
from brain.platform.db.repositories.skills import SkillRepository

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parents[4] / "uploads"
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "avif"}
VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm"}
TEXT_EXTENSIONS = {"txt", "md", "csv", "json", "log", "text", "tsv", "xml", "yaml", "yml"}
HTML_EXTENSIONS = {"html", "htm"}
DOCUMENT_EXTENSIONS = {"doc", "docx", "odt", "pdf", "ppt", "pptx", "rtf", "xls", "xlsx"}
ARCHIVE_EXTENSIONS = {"7z", "rar", "zip"}
ALLOWED_EXTENSIONS = {
    *ARCHIVE_EXTENSIONS,
    *DOCUMENT_EXTENSIONS,
    *HTML_EXTENSIONS,
    *IMAGE_EXTENSIONS,
    *TEXT_EXTENSIONS,
    *VIDEO_EXTENSIONS,
}
MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_VIDEO_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
UPLOAD_FALLBACK_CONTENT_TYPES = {
    "7z": "application/x-7z-compressed",
    "avif": "image/avif",
    "csv": "text/csv",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "gif": "image/gif",
    "htm": "text/html",
    "html": "text/html",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "json": "application/json",
    "log": "text/plain",
    "m4v": "video/mp4",
    "md": "text/markdown",
    "mov": "video/quicktime",
    "mp4": "video/mp4",
    "odt": "application/vnd.oasis.opendocument.text",
    "pdf": "application/pdf",
    "png": "image/png",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "rar": "application/vnd.rar",
    "rtf": "application/rtf",
    "text": "text/plain",
    "tsv": "text/tab-separated-values",
    "txt": "text/plain",
    "webm": "video/webm",
    "webp": "image/webp",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xml": "application/xml",
    "yaml": "application/x-yaml",
    "yml": "application/x-yaml",
    "zip": "application/zip",
}

_IMPLICIT_FEEDBACK_RULES = [
    ("memory_failure", ("does not remember", "forgot context", "not leverage memories", "doesn't remember", "remember anything")),
    ("shallow_reasoning", ("did not think", "too quick", "shallow", "obviously dumb", "did not leverage", "didn't leverage")),
    ("wrong_autonomy", ("couldn't you do it", "why couldn't you do it", "shouldn't you do it", "send to main chat", "can't execute from this")),
    ("dead_code", ("dead code", "never loaded", "not wired", "wrong path", "parallel module structure")),
    ("action_paralysis", ("struggles to act", "can't get anything done", "stuck", "not acting", "action paralysis")),
]


# ── Helpers ────────────────────────────────────────────────────

def _row_to_dict(row):
    """Convert a SQLAlchemy Row or mapping to a JSON-safe dict."""
    if row is None:
        return None
    if hasattr(row, "_mapping"):
        d = dict(row._mapping)
    elif hasattr(row, "__dict__"):
        d = {k: v for k, v in row.__dict__.items() if not k.startswith("_")}
    else:
        d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            d[k] = str(v)
    return d


def _rows_to_list(rows):
    return [_row_to_dict(r) for r in rows]


async def _validate_idea_org_orm(session, idea_id, org_id):
    """Check idea belongs to org, return Idea or None."""
    return await IdeaRepository(session).a_get_for_org(idea_id, org_id)


# Legacy alias used by older tests
_validate_idea_org = _validate_idea_org_orm


def _caller_is_service_principal(user: dict | None) -> bool:
    return bool(user and user.get("principal_type") == "service")


def _require_worker_principal(user: dict | None) -> None:
    if not _caller_is_service_principal(user) or not can_manage_run(user):
        raise HTTPException(status_code=403, detail="Worker service principal required")


async def _get_idea_for_user(session, idea_id: str, user: dict | None) -> Idea | None:
    repo = IdeaRepository(session)
    if _caller_is_service_principal(user):
        return await repo.a_get(idea_id)
    org_id = require_org_context(user or {})
    org_user_ids = select(User.id).where(User.org_id == str(org_id))
    stmt = select(Idea).where(
        Idea.id == idea_id,
        or_(
            Idea.org_id == str(org_id),
            and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
        ),
    )
    return (await session.scalars(stmt)).first()


async def _require_idea_for_user(
    session,
    idea_id: str,
    user: dict | None,
    *,
    detail: str = "Idea not found",
) -> Idea:
    idea = await _get_idea_for_user(session, idea_id, user)
    if idea is None:
        raise HTTPException(status_code=404, detail=detail)
    return idea


_a_get_idea_for_user = _get_idea_for_user
_a_require_idea_for_user = _require_idea_for_user


def _parse_message_type(content, role="user"):
    if role in ("assistant", "illo"):
        return "agent_response"
    if (content or "").strip():
        return "trigger"
    return "discuss"


def _extract_mentions(content):
    return extract_mention_token_list(content or "")


def _infer_feedback_tags(content: str) -> list[str]:
    text_content = (content or "").lower()
    tags = []
    for tag, phrases in _IMPLICIT_FEEDBACK_RULES:
        if any(phrase in text_content for phrase in phrases):
            tags.append(tag)
    return tags


async def _record_implicit_feedback(session, idea_id: str, content: str, tags: list[str]) -> None:
    if not tags:
        return
    try:
        stmt = (
            select(AgentRun)
            .where(AgentRun.thread_id == idea_id)
            .order_by(
                func.coalesce(
                    AgentRun.completed_at,
                    AgentRun.started_at,
                    AgentRun.created_at,
                ).desc()
            )
            .limit(1)
        )
        run = (await session.scalars(stmt)).first()
        if run:
            metadata = dict(run.metadata_ or {})
            existing_tags = list(metadata.get("implicit_feedback_tags") or [])
            metadata["implicit_feedback_tags"] = list(dict.fromkeys([*existing_tags, *tags]))
            metadata["implicit_feedback_summary"] = content[:500]
            run.metadata_ = metadata
    except Exception:
        logger.exception("Failed to persist implicit feedback for idea %s", idea_id)


async def _create_feedback_triggers(session, skill_used: str, task_summary: str, note: str):
    from datetime import datetime as dt

    repo = SkillRepository(session)
    skill = await repo.a_get_by_name(skill_used)
    if skill:
        triggers = list(skill.triggers or [])
        triggers.append({
            "direction": "negative",
            "pattern": task_summary[:200],
            "confidence": 0.85,
            "source": "user_correction",
            "created_at": dt.now().isoformat(),
        })
        skill.triggers = triggers

    if note:
        all_skills = await repo.a_list_active()
        for s in all_skills:
            if s.name.lower() in note.lower():
                s_triggers = list(s.triggers or [])
                s_triggers.append({
                    "direction": "positive",
                    "pattern": task_summary[:200],
                    "confidence": 0.85,
                    "source": "user_correction",
                    "created_at": dt.now().isoformat(),
                })
                s.triggers = s_triggers
                break


# ── Presence tracking (in-memory) ──────────────────────────────
_presence_store: dict = {}
_presence_lock = threading.Lock()
_PRESENCE_TIMEOUT_S = 30


def _presence_join(idea_id, user_id, name, color):
    with _presence_lock:
        if idea_id not in _presence_store:
            _presence_store[idea_id] = {}
        _presence_store[idea_id][user_id] = {
            "user_id": user_id,
            "name": name,
            "color": color,
            "last_heartbeat": time.time(),
        }


def _presence_leave(idea_id, user_id):
    with _presence_lock:
        if idea_id in _presence_store:
            _presence_store[idea_id].pop(user_id, None)
            if not _presence_store[idea_id]:
                del _presence_store[idea_id]


def _presence_get(idea_id):
    with _presence_lock:
        viewers = _presence_store.get(idea_id, {})
        return [{"user_id": v["user_id"], "name": v["name"], "color": v["color"]} for v in viewers.values()]


def _presence_cleanup():
    now = time.time()
    with _presence_lock:
        for idea_id in list(_presence_store.keys()):
            for uid in list(_presence_store[idea_id].keys()):
                if now - _presence_store[idea_id][uid]["last_heartbeat"] > _PRESENCE_TIMEOUT_S:
                    del _presence_store[idea_id][uid]
            if not _presence_store[idea_id]:
                del _presence_store[idea_id]
