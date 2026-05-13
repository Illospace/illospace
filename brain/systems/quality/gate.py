#!/usr/bin/env python3
"""Memory quality gate — prevents low-quality encodings from entering the brain.

All validation runs BEFORE embedding generation to avoid wasted compute.
"""

import re
import logging

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.embeddings import embed_document, vec_to_pg

logger = logging.getLogger(__name__)

# Patterns that indicate test/debug artifacts
TEST_PATTERNS = re.compile(
    r"^(test|cap test|hit test|miss test|log test|floor test|partial test|"
    r"always hit|always miss|test encode|hello world|asdf|foo bar)\s*$",
    re.IGNORECASE,
)

# Words that suggest real informational content
CONTEXT_SIGNALS = re.compile(
    r"\b(because|when|should|learned|decided|operator|illo|bug|fix|lesson|"
    r"pattern|architecture|deploy|production|customer|api|database|"
    r"frontend|backend|config|error|issue|pr|merge|branch)\b",
    re.IGNORECASE,
)


class QualityResult:
    """Result of a quality check."""

    __slots__ = ("passed", "reason", "adjusted_salience")

    def __init__(self, passed: bool, reason: str = "", adjusted_salience: float | None = None):
        self.passed = passed
        self.reason = reason
        self.adjusted_salience = adjusted_salience

    def __repr__(self):
        return f"QualityResult(passed={self.passed}, reason={self.reason!r})"


async def check_quality(
    content: str,
    salience: float = 5.0,
    memory_type: str = "episode",
    skip_duplicate_check: bool = False,
) -> QualityResult:
    """Run all quality checks on proposed memory content.

    Returns QualityResult with pass/fail, reason, and optional salience adjustment.
    """
    content_stripped = content.strip()
    content_len = len(content_stripped)

    # 1. Test/debug artifact detection
    if TEST_PATTERNS.match(content_stripped):
        return QualityResult(False, f"test/debug artifact: {content_stripped!r}")

    # 2. Minimum content length (unless high-salience intentional note)
    if content_len < 20 and salience < 8.0:
        return QualityResult(False, f"too short ({content_len} chars) with salience {salience}")

    # 3. Duplicate detection (semantic similarity)
    if not skip_duplicate_check and content_len >= 20:
        dup = await _check_near_duplicate(content_stripped)
        if dup:
            return QualityResult(False, f"near-duplicate of memory #{dup['id']} (similarity {dup['similarity']:.3f})")

    # 4. Auto-salience adjustment
    adjusted = _adjust_salience(content_stripped, salience, memory_type)

    return QualityResult(True, "passed", adjusted)


async def _check_near_duplicate(content: str, threshold: float = 0.90) -> dict | None:
    """Check if content is a near-duplicate of an existing memory."""
    try:
        emb = embed_document(content)
        emb_str = vec_to_pg(emb)
        async with UnitOfWork() as uow:
            row = (await uow.session.execute(text("""
                SELECT id, content, 1 - (semantic_embedding <=> CAST(:emb AS vector)) as similarity
                FROM memories
                WHERE NOT archived AND superseded_by IS NULL
                ORDER BY semantic_embedding <=> CAST(:emb AS vector)
                LIMIT 1
            """), {"emb": emb_str})).mappings().first()
            if row and row["similarity"] >= threshold:
                return {"id": row["id"], "similarity": row["similarity"], "content": row["content"]}
    except Exception as e:
        logger.warning("Duplicate check failed (allowing memory): %s", e)
    return None


def _adjust_salience(content: str, salience: float, memory_type: str) -> float:
    """Heuristic salience adjustment based on content quality signals."""
    adjusted = salience

    # Short content caps salience at 5 (unless already below)
    if len(content) < 50 and adjusted > 5.0:
        adjusted = 5.0

    # Content with specific technical details gets a boost
    context_matches = len(CONTEXT_SIGNALS.findall(content))
    if context_matches >= 3 and adjusted < 10.0:
        adjusted = min(adjusted + 1.0, 10.0)

    return adjusted


async def sweep_low_quality(dry_run: bool = True) -> list[dict]:
    """Find low-quality memories for nightly review.

    Returns list of flagged memories with reason.
    """
    flagged = []
    async with UnitOfWork() as uow:
        # Short + low salience
        rows = (await uow.session.execute(text("""
            SELECT id, content, salience, memory_type, LENGTH(content) as len
            FROM memories
            WHERE NOT archived AND superseded_by IS NULL
              AND LENGTH(content) < 30 AND salience < 7
        """))).mappings().all()
        for row in rows:
            flagged.append({
                "id": row["id"], "content": row["content"][:80],
                "reason": f"short ({row['len']} chars) + low salience ({row['salience']})",
                "action": "review",
            })

        # Near-duplicate clusters
        dup_rows = (await uow.session.execute(text("""
            SELECT m1.id as id1, m2.id as id2,
                   m1.content as c1, m2.content as c2,
                   1 - (m1.semantic_embedding <=> m2.semantic_embedding) as sim
            FROM memories m1
            JOIN memories m2 ON m2.id > m1.id
            WHERE NOT m1.archived AND NOT m2.archived
              AND m1.superseded_by IS NULL AND m2.superseded_by IS NULL
              AND 1 - (m1.semantic_embedding <=> m2.semantic_embedding) > 0.85
            ORDER BY sim DESC
            LIMIT 20
        """))).mappings().all()
        for row in dup_rows:
            flagged.append({
                "id": row["id2"], "duplicate_of": row["id1"],
                "similarity": float(row["sim"]),
                "content": row["c2"][:80],
                "reason": f"near-duplicate of #{row['id1']} (sim={row['sim']:.3f})",
                "action": "archive_or_merge",
            })

        if not dry_run:
            # Auto-archive the short+low-salience ones
            short_ids = [f["id"] for f in flagged if f["action"] == "review"]
            if short_ids:
                await uow.session.execute(
                    text("UPDATE memories SET archived = true WHERE id = ANY(:ids)"),
                    {"ids": short_ids},
                )

    return flagged
