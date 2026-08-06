"""Run every registered knowledge connector sequentially."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.knowledge.connectors import (
    DomainRecordsConnector,
    GitHubConnector,
    MemoryConnector,
    SkillsConnector,
    SlackKnowledgeConnector,
)
from brain.systems.knowledge.connectors.base import KnowledgeConnector
from brain.systems.knowledge.service import sync_connector

logger = logging.getLogger(__name__)

CONNECTOR_FACTORIES: tuple[Callable[[], KnowledgeConnector], ...] = (
    DomainRecordsConnector,
    GitHubConnector,
    SlackKnowledgeConnector,
    MemoryConnector,
    SkillsConnector,
)


async def run_knowledge_index_sync(
    connectors: Sequence[KnowledgeConnector] | None = None,
) -> dict[str, Any]:
    registered = list(connectors) if connectors is not None else [factory() for factory in CONNECTOR_FACTORIES]
    results: list[dict[str, Any]] = []
    for connector in registered:
        try:
            async with UnitOfWork() as uow:
                result = await sync_connector(uow.session, connector)
                payload = result.to_dict()
        except Exception as exc:
            logger.exception(
                "Knowledge index sync crashed for source %s",
                getattr(connector, "source_key", "unknown"),
            )
            payload = {
                "source": str(getattr(connector, "source_key", "unknown")),
                "status": "failed",
                "stats": {
                    "ingested": 0,
                    "skipped": 0,
                    "failed": 1,
                    "truncated": 0,
                },
                "exception_type": type(exc).__name__,
                "error": str(exc),
            }
        results.append(payload)
        logger.info(
            "Knowledge index source stats: %s",
            json.dumps(payload, default=str, sort_keys=True),
        )
    return {
        "job": "knowledge_index_sync",
        "ok": all(result.get("status") != "failed" for result in results),
        "results": results,
    }


async def async_main() -> int:
    result = await run_knowledge_index_sync()
    print(json.dumps(result, default=str, sort_keys=True))
    return 0 if result["ok"] else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CONNECTOR_FACTORIES", "run_knowledge_index_sync"]
