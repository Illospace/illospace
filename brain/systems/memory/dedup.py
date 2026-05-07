"""Result set deduplication — remove near-duplicate memories from retrieval.

Uses pairwise cosine similarity on embeddings to detect duplicates, keeping
the result with higher salience when two items are too similar.
"""

from __future__ import annotations

import logging
from typing import Sequence

import numpy as np

logger = logging.getLogger(__name__)


def deduplicate_results(
    results: list[dict],
    threshold: float = 0.85,
    *,
    backfill_candidates: list[dict] | None = None,
) -> list[dict]:
    """Remove near-duplicate memories from a retrieval result set.

    For each pair of results whose cosine similarity exceeds *threshold*,
    the item with lower salience is dropped.  Removed slots can optionally
    be backfilled from *backfill_candidates* (if provided and not themselves
    duplicates of the kept results).

    Args:
        results: Retrieved memory dicts, each expected to have ``embedding``
            and ``salience`` keys.
        threshold: Cosine similarity above which two results are considered
            duplicates.  Defaults to 0.85.
        backfill_candidates: Extra candidates to fill slots freed by dedup.

    Returns:
        Deduplicated list of result dicts.
    """
    if len(results) <= 1:
        return list(results)

    # Track which indices to keep
    n = len(results)
    removed: set[int] = set()

    for i in range(n):
        if i in removed:
            continue
        for j in range(i + 1, n):
            if j in removed:
                continue
            sim = _cosine_similarity(
                results[i].get("embedding"),
                results[j].get("embedding"),
            )
            if sim is not None and sim > threshold:
                # Drop the one with lower salience
                sal_i = results[i].get("salience", 0) or 0
                sal_j = results[j].get("salience", 0) or 0
                drop = j if sal_i >= sal_j else i
                removed.add(drop)
                logger.debug(
                    "Dedup: dropping index %d (sim=%.3f, salience=%.2f) "
                    "in favour of index %d (salience=%.2f)",
                    drop,
                    sim,
                    results[drop].get("salience", 0),
                    i if drop == j else j,
                    results[i if drop == j else j].get("salience", 0),
                )

    kept = [r for idx, r in enumerate(results) if idx not in removed]

    # Backfill if we removed items and have candidates
    slots_freed = len(results) - len(kept)
    if slots_freed > 0 and backfill_candidates:
        for candidate in backfill_candidates:
            if slots_freed <= 0:
                break
            # Ensure candidate isn't a duplicate of kept results
            is_dup = False
            for k in kept:
                sim = _cosine_similarity(
                    k.get("embedding"),
                    candidate.get("embedding"),
                )
                if sim is not None and sim > threshold:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(candidate)
                slots_freed -= 1

    return kept


def _cosine_similarity(a: Sequence[float] | None, b: Sequence[float] | None) -> float | None:
    """Compute cosine similarity between two embedding vectors.

    Returns None if either embedding is None or zero-length.
    """
    if a is None or b is None:
        return None

    a_arr = np.asarray(a, dtype=np.float64)
    b_arr = np.asarray(b, dtype=np.float64)

    if a_arr.size == 0 or b_arr.size == 0:
        return None

    norm_a = np.linalg.norm(a_arr)
    norm_b = np.linalg.norm(b_arr)

    if norm_a == 0 or norm_b == 0:
        return None

    return float(np.dot(a_arr, b_arr) / (norm_a * norm_b))
