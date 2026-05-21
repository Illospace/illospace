"""Scheduled cleanup for expired Project draft workspaces."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.project_context.draft_lifecycle import (
    cleanup_expired_project_draft_workspaces,
)


async def run_project_draft_cleanup(
    *,
    now: datetime | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Delete archived-thread Project drafts whose unpublished grace period expired."""

    clock = now or datetime.now(timezone.utc)
    async with UnitOfWork() as uow:
        payload = await cleanup_expired_project_draft_workspaces(
            uow.session,
            now=clock,
            limit=limit,
        )
    return {
        "pipeline": "project_draft_cleanup",
        "cleaned_at": clock.isoformat(),
        **payload,
    }


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Clean up expired Project draft workspaces.")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args(argv)
    payload = asyncio.run(run_project_draft_cleanup(limit=args.limit))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return payload


if __name__ == "__main__":
    main()
