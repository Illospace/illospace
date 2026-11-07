"""Saved tool policies. The caller owns the write transaction."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select

from brain.platform.db.models.org import Org
from brain.platform.db.models.tool_policy import ToolPolicy, ToolPolicyChangeAudit

MAX_DISABLED_TOOLS = 512
MAX_TOOL_NAME_LENGTH = 200


class ToolPolicyConflict(ValueError):
    pass


def validate_disabled_tools(names: list[str]) -> list[str]:
    if not isinstance(names, list) or len(names) > MAX_DISABLED_TOOLS:
        raise ValueError(
            f"disabled_tools must contain at most {MAX_DISABLED_TOOLS} names"
        )
    clean = set()
    for name in names:
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > MAX_TOOL_NAME_LENGTH
        ):
            raise ValueError(
                f"Tool names must contain 1 to {MAX_TOOL_NAME_LENGTH} characters"
            )
        clean.add(name.strip())
    return sorted(clean)


def _scope(org_id: str, scope_type: str, scope_key: str):
    return (
        ToolPolicy.org_id == org_id,
        ToolPolicy.scope_type == scope_type,
        ToolPolicy.scope_key == scope_key,
    )


async def read_tool_policy(
    session, *, org_id: str, scope_type: str, scope_key: str = ""
):
    return await session.scalar(
        select(ToolPolicy).where(*_scope(org_id, scope_type, scope_key))
    )


async def load_tool_policies(org_id: str, skill_names: set[str]) -> list[ToolPolicy]:
    from brain.platform.db import SessionFactory

    async with SessionFactory() as session:
        return list(
            await session.scalars(
                select(ToolPolicy).where(
                    ToolPolicy.org_id == org_id,
                    or_(
                        ToolPolicy.scope_type == "organization",
                        (
                            (ToolPolicy.scope_type == "skill")
                            & ToolPolicy.scope_key.in_(skill_names)
                        ),
                    ),
                )
            )
        )


async def apply_tool_policy_change(
    session,
    *,
    org_id: str,
    scope_type: str,
    scope_key: str = "",
    disabled_tools: list[str],
    expected_version: int,
    actor_id: str,
    actor_type: str,
) -> ToolPolicy:
    names = validate_disabled_tools(disabled_tools)
    if scope_type not in {"organization", "skill"} or (
        scope_type == "organization"
    ) != (scope_key == ""):
        raise ValueError("Invalid policy scope")
    if len(scope_key) > 50:
        raise ValueError("Skill names must contain at most 50 characters")
    # Lock the organization, including the first write when no policy row exists.
    # This serializes version allocation without an insert race.
    await session.execute(select(Org.id).where(Org.id == org_id).with_for_update())
    policy = await session.scalar(
        select(ToolPolicy)
        .where(
            *_scope(org_id, scope_type, scope_key),
        )
        .execution_options(populate_existing=True)
    )
    version = policy.version if policy else 0
    if expected_version != version:
        raise ToolPolicyConflict("The tool policy changed. Reload it before saving.")
    before = list(policy.disabled_tools) if policy else []
    now = datetime.now(timezone.utc)
    if policy is None:
        policy = ToolPolicy(org_id=org_id, scope_type=scope_type, scope_key=scope_key)
        session.add(policy)
    policy.disabled_tools = names
    policy.version = version + 1
    policy.updated_by = actor_id
    policy.updated_at = now
    session.add(
        ToolPolicyChangeAudit(
            org_id=org_id,
            scope_type=scope_type,
            scope_key=scope_key,
            version=version + 1,
            actor_id=actor_id,
            actor_type=actor_type,
            before_disabled_tools=before,
            after_disabled_tools=names,
            changed_at=now,
        )
    )
    await session.flush()
    return policy


async def list_tool_policy_history(
    session, *, org_id: str, scope_type: str, scope_key: str = "", limit: int = 100
):
    return list(
        await session.scalars(
            select(ToolPolicyChangeAudit)
            .where(
                ToolPolicyChangeAudit.org_id == org_id,
                ToolPolicyChangeAudit.scope_type == scope_type,
                ToolPolicyChangeAudit.scope_key == scope_key,
            )
            .order_by(ToolPolicyChangeAudit.version.desc())
            .limit(limit)
        )
    )
