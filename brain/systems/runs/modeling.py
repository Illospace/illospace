"""AgentRun model routing and cost helpers."""

from __future__ import annotations

import sys

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.providers.model_policy import (
    calculate_model_cost,
    get_default_model,
    resolve_skill_model,
)


def resolve_model(
    skill_name: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    preferred_provider: str | None = None,
) -> tuple[str, str]:
    """Resolve model and thinking level from skill runtime settings."""
    try:
        return resolve_skill_model(
            skill_name,
            user_id=user_id,
            org_id=org_id,
            preferred_provider=preferred_provider,
        )
    except Exception as exc:
        print(f"Warning: resolve_model failed for skill '{skill_name}': {exc}", file=sys.stderr)
        return (
            get_default_model(
                provider=preferred_provider,
                user_id=user_id,
                org_id=org_id,
            ),
            "medium",
        )


async def get_skill_runtime_settings() -> list[dict]:
    """Return all non-archived skills with their runtime settings."""
    try:
        async with UnitOfWork() as uow:
            rows = (await uow.session.execute(text(
                "SELECT name, thinking_tier, maturity, confidence, "
                "use_count, success_count, version "
                "FROM skills WHERE NOT archived ORDER BY name"
            ))).mappings().all()
            return [dict(row) for row in rows]
    except Exception as exc:
        print(f"Warning: get_skill_runtime_settings failed: {exc}", file=sys.stderr)
        return []


def calculate_cost(
    model: str,
    tokens_input: int,
    tokens_output: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Calculate estimated cost for a model run based on token usage."""
    return calculate_model_cost(
        model,
        tokens_input,
        tokens_output,
        cache_read=cache_read,
        cache_write=cache_write,
    )


__all__ = ["calculate_cost", "get_skill_runtime_settings", "resolve_model"]
