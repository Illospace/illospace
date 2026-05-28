"""Cycle actor identity and workspace access policy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select

from brain.platform.db.models.cycle import Cycle
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User


@dataclass(frozen=True)
class CycleActor:
    user_id: str
    org_id: str | None = None
    principal_type: str = "user"
    source_id: str | None = None

    @classmethod
    def from_user_payload(cls, user: dict[str, Any]) -> "CycleActor":
        return cls(
            user_id=str(user["id"]),
            org_id=str(user["org_id"]) if user.get("org_id") else None,
            principal_type=user.get("principal_type") or "user",
            source_id=str(user["id"]),
        )

    @property
    def is_service_principal(self) -> bool:
        return self.principal_type == "service"

    @property
    def source_type(self) -> str:
        return self.principal_type or "user"

    @property
    def revision_source_id(self) -> str:
        return str(self.source_id or self.user_id)


def cycle_scope_conditions(actor: CycleActor) -> list[Any]:
    if actor.is_service_principal:
        return [Cycle.deleted_at.is_(None)]
    if actor.org_id:
        org_user_ids = select(User.id).where(User.org_id == actor.org_id)
        return [
            or_(
                Cycle.org_id == actor.org_id,
                and_(Cycle.org_id.is_(None), Cycle.user_id.in_(org_user_ids)),
            ),
            Cycle.deleted_at.is_(None),
        ]
    return [Cycle.user_id == actor.user_id, Cycle.deleted_at.is_(None)]


def target_idea_scope_conditions(idea_id: str, actor: CycleActor) -> list[Any]:
    conditions: list[Any] = [Idea.id == idea_id]
    if actor.is_service_principal:
        return conditions
    conditions.append(cycle_owned_idea_condition(actor.user_id, actor.org_id))
    return conditions


def cycle_target_idea_scope_condition(cycle: Cycle) -> Any:
    return cycle_owned_idea_condition(cycle.user_id, cycle.org_id)


def cycle_owned_idea_condition(user_id: str, org_id: str | None) -> Any:
    if org_id:
        org_user_ids = select(User.id).where(User.org_id == org_id)
        return or_(
            Idea.org_id == org_id,
            and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
        )
    return Idea.user_id == user_id
