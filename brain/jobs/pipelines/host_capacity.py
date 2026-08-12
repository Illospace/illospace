#!/usr/bin/env python3
"""Record host and workspace capacity through the active storage policy."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from brain.kernel import config as brain_config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.host_capacity import record_host_capacity


async def run() -> dict[str, Any]:
    async with UnitOfWork() as uow:
        return await record_host_capacity(
            uow.session,  # type: ignore[arg-type]
            workspace_root=brain_config.resolve_workspace_root(),
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [host_capacity] %(message)s",
    )
    print(json.dumps(asyncio.run(run()), sort_keys=True))


if __name__ == "__main__":
    main()
