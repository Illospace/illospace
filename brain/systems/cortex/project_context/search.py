"""Search Project Context profiles and attachments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.idea import Idea, IdeaProjectAttachment, ProjectProfile, ProjectProfileAccess
from brain.platform.db.models.org import User
from brain.systems.cortex.project_context.access import project_profile_visible_predicate
from brain.systems.cortex.thread_links import thread_link_payload


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _compact_text(text: str | None, *, limit: int = 160) -> str | None:
    normalized = " ".join((text or "").split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."


def project_context_resource_summary(context: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        return {"count": 0, "items": []}
    resources = [item for item in (context.get("resources") or []) if isinstance(item, Mapping)]
    return {
        "count": len(resources),
        "items": [
            {
                "id": item.get("id"),
                "kind": item.get("kind") or item.get("type"),
                "label": item.get("label") or item.get("name"),
                "path": item.get("path"),
                "repo": item.get("repo"),
                "uri": item.get("uri"),
            }
            for item in resources[:10]
        ],
    }


def _search_terms(value: str | None) -> list[str]:
    normalized = " ".join(str(value or "").lower().split())
    if not normalized:
        return []
    return [term for term in normalized.split(" ") if term][:12]


def _match_all_terms(terms: Sequence[str], *columns: Any) -> Any | None:
    if not terms:
        return None
    return and_(
        *[
            or_(*[func.lower(column).like(f"%{term}%") for column in columns])
            for term in terms
        ]
    )


def _thread_reference_snapshot(
    *,
    thread_id: Any,
    title: Any,
    preview_summary: Any = None,
    preview_source: Any = None,
    preview_updated_at: Any = None,
) -> dict[str, Any]:
    return {
        "type": "thread_reference",
        "object_type": "thread",
        "object_id": str(thread_id),
        "thread_id": str(thread_id),
        "status": "available",
        "title": str(title or "Untitled thread"),
        "preview_summary": preview_summary,
        "preview_source": preview_source,
        "preview_updated_at": _iso(preview_updated_at),
        **thread_link_payload(thread_id),
    }


def _profile_result(profile: ProjectProfile, user: User | None) -> dict[str, Any]:
    return {
        "type": "project_context_profile",
        "project_profile_id": str(profile.id),
        "id": str(profile.id),
        "slug": profile.slug,
        "name": profile.name,
        "description": _compact_text(profile.description, limit=520),
        "active": bool(profile.active),
        "visibility": profile.visibility,
        "owner_user_id": str(profile.user_id) if profile.user_id is not None else None,
        "owner_name": user.name if user else None,
        "resources": project_context_resource_summary(profile.project_context),
        "created_at": _iso(profile.created_at),
        "provenance": {"table": "project_profiles", "id": str(profile.id)},
    }


def _attachment_result(
    attachment: IdeaProjectAttachment,
    idea: Idea,
    profile: ProjectProfile | None,
    user: User | None,
) -> dict[str, Any]:
    links = thread_link_payload(attachment.idea_id)
    return {
        "type": "project_context_attachment",
        "id": int(attachment.id),
        "idea_id": str(attachment.idea_id),
        "thread_id": str(attachment.idea_id),
        **links,
        "thread_reference": _thread_reference_snapshot(
            thread_id=attachment.idea_id,
            title=idea.display_title or idea.title,
            preview_summary=idea.preview_summary,
            preview_source=idea.preview_source,
            preview_updated_at=idea.preview_updated_at,
        ),
        "idea_title": idea.display_title or idea.title,
        "project_profile_id": str(attachment.project_profile_id) if attachment.project_profile_id else None,
        "project_slug": profile.slug if profile else None,
        "project_name": profile.name if profile else None,
        "status": attachment.status,
        "attached_by": str(attachment.attached_by) if attachment.attached_by else None,
        "attached_by_name": user.name if user else None,
        "resources": project_context_resource_summary(attachment.snapshot),
        "created_at": _iso(attachment.created_at),
        "provenance": {"table": "idea_project_attachments", "id": int(attachment.id)},
    }


async def search_project_contexts(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str | None,
    query: str | None = None,
    limit: int = 10,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Search Project Context profiles and attachments visible to a user."""

    text = str(query or "").strip()
    max_results = max(1, min(int(limit or 10), 25))
    results: list[dict[str, Any]] = []
    terms = _search_terms(text)

    profile_stmt = (
        select(ProjectProfile, User)
        .outerjoin(User, User.id == ProjectProfile.user_id)
        .where(ProjectProfile.org_id == org_id)
        .where(project_profile_visible_predicate(ProjectProfile, ProjectProfileAccess, user_id))
        .order_by(ProjectProfile.created_at.desc(), ProjectProfile.id.desc())
        .limit(max_results)
    )
    if not include_inactive:
        profile_stmt = profile_stmt.where(ProjectProfile.active.is_(True))
    profile_match = _match_all_terms(
        terms,
        ProjectProfile.slug,
        ProjectProfile.name,
        ProjectProfile.description,
        cast(ProjectProfile.project_context, String),
    )
    if profile_match is not None:
        profile_stmt = profile_stmt.where(profile_match)

    for profile, user in (await session.execute(profile_stmt)).all():
        results.append(_profile_result(profile, user))

    remaining = max_results - len(results)
    if remaining <= 0:
        return {"query": text, "results": results}

    attachment_stmt = (
        select(IdeaProjectAttachment, Idea, ProjectProfile, User)
        .join(Idea, Idea.id == IdeaProjectAttachment.idea_id)
        .outerjoin(ProjectProfile, ProjectProfile.id == IdeaProjectAttachment.project_profile_id)
        .outerjoin(User, User.id == IdeaProjectAttachment.attached_by)
        .where(Idea.org_id == org_id)
        .where(Idea.archived_at.is_(None))
        .where(IdeaProjectAttachment.status != "invalid")
        .order_by(IdeaProjectAttachment.created_at.desc(), IdeaProjectAttachment.id.desc())
        .limit(remaining)
    )
    attachment_match = _match_all_terms(
        terms,
        Idea.title,
        Idea.display_title,
        ProjectProfile.slug,
        ProjectProfile.name,
        cast(IdeaProjectAttachment.snapshot, String),
    )
    if attachment_match is not None:
        attachment_stmt = attachment_stmt.where(attachment_match)

    for attachment, idea, profile, user in (await session.execute(attachment_stmt)).all():
        results.append(_attachment_result(attachment, idea, profile, user))

    return {"query": text, "results": results}
