"""Project Context profile visibility policy."""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import delete, exists, func, or_, select


PROJECT_VISIBILITY_PUBLIC = "public"
PROJECT_VISIBILITY_PRIVATE = "private"
VALID_PROJECT_VISIBILITIES = {PROJECT_VISIBILITY_PUBLIC, PROJECT_VISIBILITY_PRIVATE}


def normalize_project_visibility(value: str | None, *, default: str = PROJECT_VISIBILITY_PRIVATE) -> str:
    visibility = str(value or default).strip().lower()
    if visibility not in VALID_PROJECT_VISIBILITIES:
        allowed = ", ".join(sorted(VALID_PROJECT_VISIBILITIES))
        raise ValueError(f"Invalid project visibility {visibility!r}; expected one of: {allowed}")
    return visibility


def project_profile_visibility(profile: Any, *, default: str = PROJECT_VISIBILITY_PUBLIC) -> str:
    try:
        value = getattr(profile, "visibility")
    except Exception:
        value = None
    try:
        return normalize_project_visibility(value, default=default)
    except ValueError:
        return default


def project_profile_visible_predicate(ProjectProfile: Any, ProjectProfileAccess: Any, user_id: str | None):
    conditions = [ProjectProfile.visibility == PROJECT_VISIBILITY_PUBLIC]
    if user_id:
        conditions.extend([
            ProjectProfile.user_id == user_id,
            exists().where(
                ProjectProfileAccess.project_profile_id == ProjectProfile.id,
                ProjectProfileAccess.shared_with_user_id == user_id,
            ),
        ])
    return or_(*conditions)


def is_project_profile_visible(
    profile: Any,
    *,
    user_id: str | None,
    shared_user_ids: Iterable[str] = (),
) -> bool:
    visibility = project_profile_visibility(profile)
    if visibility == PROJECT_VISIBILITY_PUBLIC:
        return True
    if not user_id:
        return False
    if str(getattr(profile, "user_id", "") or "") == str(user_id):
        return True
    return str(user_id) in {str(item) for item in shared_user_ids}


def can_manage_project_profile(profile: Any, user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    if str(user.get("principal_type") or "").lower() == "service" or str(user.get("id") or "").startswith("service:"):
        return True
    user_id = str(user.get("id") or "")
    if user_id and str(getattr(profile, "user_id", "") or "") == user_id:
        return True
    role = str(user.get("role") or "").lower()
    return not getattr(profile, "user_id", None) and role in {"owner", "admin"}


def _clean_shared_usernames(usernames: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for username in usernames or []:
        value = str(username or "").strip()
        key = value.lower()
        if not value or key in seen:
            continue
        cleaned.append(value)
        seen.add(key)
    return cleaned


async def resolve_project_access_users(
    session: Any,
    org_id: str | None,
    usernames: list[str] | None,
) -> list[Any]:
    from brain.platform.db.models.org import User

    cleaned = _clean_shared_usernames(usernames)
    if not cleaned:
        return []
    if not org_id:
        raise ValueError("Project sharing requires an org-scoped user")

    lookup_keys = {item.lower() for item in cleaned}
    users = (
        await session.scalars(
            select(User).where(
                User.org_id == org_id,
                func.lower(User.name).in_(lookup_keys),
            )
        )
    ).all()
    users_by_key: dict[str, list[Any]] = {}
    for user in users:
        key = str(getattr(user, "name", "") or "").strip().lower()
        if key and key in lookup_keys:
            users_by_key.setdefault(key, []).append(user)
    missing = [username for username in cleaned if username.lower() not in users_by_key]
    if missing:
        raise ValueError(f"Unknown project share users: {', '.join(missing)}")
    ambiguous = [username for username in cleaned if len(users_by_key.get(username.lower(), [])) > 1]
    if ambiguous:
        raise ValueError(f"Ambiguous project share users: {', '.join(ambiguous)}")

    ordered: list[Any] = []
    seen_ids: set[str] = set()
    for username in cleaned:
        matches = users_by_key.get(username.lower()) or []
        if not matches:
            continue
        matched = matches[0]
        matched_id = str(matched.id)
        if matched_id in seen_ids:
            continue
        ordered.append(matched)
        seen_ids.add(matched_id)
    return ordered


async def sync_project_access_list(
    session: Any,
    profile: Any,
    *,
    org_id: str | None,
    shared_usernames: list[str] | None,
    actor_user_id: str | None,
) -> None:
    from brain.platform.db.models.idea import ProjectProfileAccess

    users = await resolve_project_access_users(session, org_id, shared_usernames)
    owner_user_id = str(getattr(profile, "user_id", None) or "")
    target_user_ids = [
        str(user.id)
        for user in users
        if str(user.id) != owner_user_id
    ]
    target_user_ids = list(dict.fromkeys(target_user_ids))

    if target_user_ids:
        await session.execute(
            delete(ProjectProfileAccess).where(
                ProjectProfileAccess.project_profile_id == profile.id,
                ProjectProfileAccess.shared_with_user_id.not_in(target_user_ids),
            )
        )
    else:
        await session.execute(
            delete(ProjectProfileAccess).where(ProjectProfileAccess.project_profile_id == profile.id)
        )

    existing_ids = {
        str(row.shared_with_user_id)
        for row in (
            await session.scalars(
                select(ProjectProfileAccess).where(ProjectProfileAccess.project_profile_id == profile.id)
            )
        ).all()
    }
    for user_id in target_user_ids:
        if user_id in existing_ids:
            continue
        session.add(
            ProjectProfileAccess(
                project_profile_id=profile.id,
                shared_with_user_id=user_id,
                shared_by_user_id=actor_user_id,
            )
        )


async def require_idea_for_project_actor(
    session: Any,
    idea_id: str,
    actor: dict[str, Any] | None,
    *,
    detail: str = "Idea not found",
) -> Any:
    from brain.platform.db.models.idea import Idea
    from brain.platform.db.models.org import User

    actor = actor or {}
    principal_type = str(actor.get("principal_type") or "").lower()
    actor_id = str(actor.get("id") or "")
    if principal_type == "service" or actor_id.startswith("service:"):
        idea = await session.get(Idea, idea_id)
    else:
        org_id = str(actor.get("org_id") or "")
        org_user_ids = select(User.id).where(User.org_id == org_id)
        idea = (
            await session.scalars(
                select(Idea).where(
                    Idea.id == idea_id,
                    (
                        (Idea.org_id == org_id)
                        | (Idea.org_id.is_(None) & Idea.user_id.in_(org_user_ids))
                    ),
                )
            )
        ).first()
    if idea is None:
        raise ValueError(detail)
    return idea
