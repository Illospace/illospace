"""Reconstructive-memory consolidation facade.

The flat episodic→semantic→procedural consolidation pipeline was retired with
the legacy ``memories`` table. Source-backed reconstructive memory represents
summary/procedure/policy knowledge as typed ``MemoryNode`` rows with evidence
edges to sources and spans.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SYSTEM_USER_ID = "system"


@dataclass(frozen=True)
class ConsolidationScope:
    user_id: str | None = None
    org_id: str | None = None
    visibility: str = "private"

    @property
    def key(self) -> str:
        return f"{self.visibility}:{self.org_id or '-'}:{self.user_id or '-'}"


async def cluster_episodes(*args: Any, **kwargs: Any) -> list[list[int]]:
    del args, kwargs
    return []


async def extract_semantic(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None


async def crystallize_procedural(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    return None


async def apply_forgetting_curve(*args: Any, **kwargs: Any) -> dict[str, int | bool | str]:
    del args, kwargs
    return {
        "episodic_decayed": 0,
        "semantic_decayed": 0,
        "archived": 0,
        "retired": True,
        "memory_system": "reconstructive",
    }


async def run_dag_compaction(*args: Any, **kwargs: Any) -> dict[str, int | bool | str]:
    del args, kwargs
    return {
        "leaf_passes": 0,
        "cascade_passes": 0,
        "summaries_created": 0,
        "retired": True,
        "memory_system": "reconstructive",
    }


async def run_consolidation(*args: Any, **kwargs: Any) -> dict[str, Any]:
    del args, kwargs
    return {
        "clusters_found": 0,
        "semantic_created": 0,
        "procedures_created": 0,
        "forgetting": await apply_forgetting_curve(),
        "dag": await run_dag_compaction(),
        "scopes_processed": 0,
        "retired": True,
        "memory_system": "reconstructive",
    }
