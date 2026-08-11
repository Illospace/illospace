#!/usr/bin/env python3
"""Run scheduled reclamation of retained headless-worker workspaces."""

from __future__ import annotations

import asyncio
import json
import logging

from brain.kernel import config as brain_config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.workspace_reclamation import (
    WorkspaceGCResult,
    reclaim_headless_worker_workspaces,
)


async def run() -> WorkspaceGCResult:
    async with UnitOfWork() as uow:
        return await reclaim_headless_worker_workspaces(
            uow.session,  # type: ignore[arg-type]
            workspace_root=brain_config.resolve_workspace_root(),
            automatic=True,
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [workspace_gc] %(message)s",
    )
    print(json.dumps(asyncio.run(run()), sort_keys=True))


if __name__ == "__main__":
    main()
