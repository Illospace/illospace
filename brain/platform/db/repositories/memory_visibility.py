"""Canonical memory visibility policy shared by API, recall, and tools."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import and_, false, func, or_, true

VISIBLE_SHARED_MEMORY = ("team", "org")
VALID_MEMORY_VISIBILITIES = ("private", "team", "org")


@dataclass(frozen=True)
class MemoryVisibilityContext:
    """Viewer context used to decide which memories may be read or mutated."""

    user_id: str | None = None
    org_id: str | None = None
    allow_global: bool = False
    principal_type: str | None = None
    role: str | None = None

    @classmethod
    def from_user(cls, user: Mapping[str, Any] | None) -> "MemoryVisibilityContext":
        if not user:
            return cls()
        principal_type = str(user.get("principal_type") or "human")
        user_id = _text(user.get("id"))
        allow_global = bool(
            user.get("internal")
            or principal_type == "service"
            or user_id in {"system", "service:internal-api"}
        )
        return cls(
            user_id=user_id,
            org_id=_text(user.get("org_id")),
            allow_global=allow_global,
            principal_type=principal_type,
            role=_text(user.get("role")),
        )

    @classmethod
    def system(cls) -> "MemoryVisibilityContext":
        return cls(user_id="system", allow_global=True, principal_type="service")

    @property
    def has_scope(self) -> bool:
        return bool(self.allow_global or self.user_id or self.org_id)


def memory_visibility_predicate(model: Any, context: MemoryVisibilityContext | None):
    """Return a SQLAlchemy predicate for memories visible to ``context``."""
    context = context or MemoryVisibilityContext()
    if context.allow_global:
        return true()

    visibility = func.coalesce(model.visibility, "private")
    clauses = []
    if context.user_id:
        clauses.append(
            and_(
                model.user_id == context.user_id,
                visibility == "private",
            )
        )
    if context.org_id:
        clauses.append(
            and_(
                model.org_id == context.org_id,
                visibility.in_(VISIBLE_SHARED_MEMORY),
            )
        )
    if not clauses:
        return false()
    return or_(*clauses)


def memory_is_visible(memory: Any, context: MemoryVisibilityContext | None) -> bool:
    """In-memory equivalent of ``memory_visibility_predicate`` for loaded rows."""
    context = context or MemoryVisibilityContext()
    if context.allow_global:
        return True
    visibility = str(getattr(memory, "visibility", None) or "private")
    if visibility == "private":
        return bool(context.user_id and str(getattr(memory, "user_id", "")) == context.user_id)
    if visibility in VISIBLE_SHARED_MEMORY:
        return bool(context.org_id and str(getattr(memory, "org_id", "")) == context.org_id)
    return False


def require_memory_visible(memory: Any, context: MemoryVisibilityContext | None) -> Any:
    if not memory_is_visible(memory, context):
        raise LookupError("Memory not visible")
    return memory


def memory_visibility_sql(
    context: MemoryVisibilityContext | None,
    *,
    alias: str = "m",
    user_param: str = "vis_user_id",
    org_param: str = "vis_org_id",
) -> tuple[str, dict[str, str]]:
    """Return a raw SQL fragment and params for legacy vector SQL paths."""
    context = context or MemoryVisibilityContext()
    if context.allow_global:
        return "", {}

    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: dict[str, str] = {}
    if context.user_id:
        clauses.append(
            f"({prefix}user_id = :{user_param} AND COALESCE({prefix}visibility, 'private') = 'private')"
        )
        params[user_param] = context.user_id
    if context.org_id:
        clauses.append(
            f"({prefix}org_id = :{org_param} AND COALESCE({prefix}visibility, 'private') IN ('team', 'org'))"
        )
        params[org_param] = context.org_id
    if not clauses:
        return " AND FALSE", {}
    return " AND (" + " OR ".join(clauses) + ")", params


def normalize_memory_visibility(value: str | None, *, fallback: str = "private") -> str:
    visibility = str(value or fallback).strip().lower()
    if visibility not in VALID_MEMORY_VISIBILITIES:
        return fallback
    return visibility


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
