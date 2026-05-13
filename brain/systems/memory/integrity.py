"""Memory integrity checks — run nightly after consolidation.

Detects orphan memories, stale embeddings, DAG inconsistencies,
and near-duplicate memories.  Results are logged and persisted
to MemoryHealthRepository.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class IntegrityResult:
    """Outcome of a single integrity check."""

    check_type: str
    status: str  # "passed", "warning", "failed"
    details: dict = field(default_factory=dict)
    auto_repaired: int = 0


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_orphan_memories(
    memories: list[dict],
    *,
    salience_threshold: float = 2.0,
) -> IntegrityResult:
    """Flag memories with no edges, low access, and low salience.

    Each dict in *memories* must have keys: ``id``, ``edge_count``,
    ``access_count``, ``salience``.
    """
    orphan_ids = [
        m["id"]
        for m in memories
        if m.get("edge_count", 0) == 0
        and m.get("access_count", 0) <= 1
        and m.get("salience", 0) < salience_threshold
    ]

    if orphan_ids:
        return IntegrityResult(
            check_type="orphan_memories",
            status="warning",
            details={"orphan_ids": orphan_ids, "count": len(orphan_ids)},
        )
    return IntegrityResult(check_type="orphan_memories", status="passed")


def check_embedding_staleness(memories: list[dict]) -> IntegrityResult:
    """Find memories where updated_at > embedded_at.

    Each dict must have keys: ``id``, ``updated_at``, ``embedded_at``.
    """
    stale_ids = [
        m["id"]
        for m in memories
        if m.get("updated_at") is not None
        and m.get("embedded_at") is not None
        and m["updated_at"] > m["embedded_at"]
    ]

    if stale_ids:
        return IntegrityResult(
            check_type="embedding_staleness",
            status="warning",
            details={"stale_ids": stale_ids, "count": len(stale_ids)},
        )
    return IntegrityResult(check_type="embedding_staleness", status="passed")


def check_dag_consistency(
    summaries: list[dict],
    existing_child_ids: set[int],
) -> IntegrityResult:
    """Verify all child_ids referenced by summaries actually exist.

    Each dict in *summaries* must have key ``child_ids`` (list[int]).
    """
    missing: set[int] = set()
    for s in summaries:
        for cid in s.get("child_ids", []):
            if cid not in existing_child_ids:
                missing.add(cid)

    if missing:
        return IntegrityResult(
            check_type="dag_consistency",
            status="warning",
            details={"missing_child_ids": sorted(missing), "count": len(missing)},
        )
    return IntegrityResult(check_type="dag_consistency", status="passed")


def check_duplicates(
    memory_pairs: list[tuple[int, int, float]],
    *,
    threshold: float = 0.92,
) -> IntegrityResult:
    """Flag memory pairs with cosine similarity above threshold.

    Each tuple is ``(id_a, id_b, cosine_similarity)``.
    """
    flagged = [(a, b, sim) for a, b, sim in memory_pairs if sim > threshold]

    if flagged:
        return IntegrityResult(
            check_type="duplicates",
            status="warning",
            details={
                "flagged_pairs": [(a, b) for a, b, _ in flagged],
                "count": len(flagged),
            },
        )
    return IntegrityResult(check_type="duplicates", status="passed")


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------


async def run_all_checks(*, org_id: str | None = None) -> list[IntegrityResult]:
    """Run all integrity checks and persist results.

    Actual data gathering is deferred to integration wiring — this stub
    logs the intent and persists each result to MemoryHealthRepository.
    """
    from brain.platform.db.models.memory_health import MemoryHealthLog
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    logger.info("Running memory integrity checks (org_id=%s)", org_id)

    # Stub: no data gathered yet — individual checks run at integration time.
    results: list[IntegrityResult] = []

    async with UnitOfWork() as uow:
        for result in results:
            uow.session.add(MemoryHealthLog(
                check_type=result.check_type,
                status=result.status,
                details=result.details,
                org_id=org_id,
            ))

    return results
