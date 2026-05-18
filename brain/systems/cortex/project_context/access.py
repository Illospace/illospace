"""Project Context profile visibility policy."""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import exists, or_


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
