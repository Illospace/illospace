#!/usr/bin/env python3
"""
Memory Quality Gates — every memory write goes through here.

Prevents:
1. Near-duplicates (cosine similarity >0.85)
2. Empty or trivially short content
3. Raw HTML / garbage content
4. Unbounded salience from external sources

Every rejection is logged so we can audit what was filtered.
"""

import json
import logging
import os
import sys
from datetime import datetime

from sqlalchemy import text

from brain.platform.db.repositories.memory_visibility import (
    MemoryVisibilityContext,
    memory_visibility_sql,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.embeddings import embed_document, vec_to_pg
from brain.systems.runtime_settings.memory import async_get_embedding_runtime_config

logger = logging.getLogger(__name__)

# ── Thresholds ──
DEDUP_SIMILARITY_THRESHOLD = 0.85
MIN_CONTENT_LENGTH = 20
MAX_EXTERNAL_SALIENCE = 6      # External knowledge capped
HTML_TAG_DENSITY_THRESHOLD = 0.01

# Rejection log
REJECTION_LOG = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "quality-rejections.jsonl")


def _log_rejection(content: str, reason: str, details: dict = None):
    """Log a rejected memory for audit."""
    os.makedirs(os.path.dirname(REJECTION_LOG), exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
        "content_preview": content[:200],
        "details": details or {},
    }
    with open(REJECTION_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    logger.info(f"Memory rejected: {reason} — {content[:60]}")


def check_content_quality(content: str) -> tuple[bool, str]:
    """Validate content is meaningful text, not garbage."""
    if not content or not content.strip():
        return False, "Empty content"

    if len(content.strip()) < MIN_CONTENT_LENGTH:
        return False, f"Content too short ({len(content.strip())} chars, min {MIN_CONTENT_LENGTH})"

    # Check for raw HTML
    html_density = content.count('<') / max(len(content), 1)
    if html_density > HTML_TAG_DENSITY_THRESHOLD:
        return False, f"Content appears to be raw HTML (tag density: {html_density:.3f})"

    # Check for meaningless content
    stripped = content.strip().lower()
    meaningless = ["test", "test task", "test task test", "hello", "ping", "ok", "yes", "no"]
    if stripped in meaningless:
        return False, f"Content is trivially meaningless: '{stripped}'"

    return True, ""


async def check_duplicate(
    content: str,
    embedding=None,
    window_days: int = 7,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    allow_global: bool = False,
) -> tuple[bool, dict]:
    """Check if content is a near-duplicate of an existing recent memory.

    Returns (is_duplicate, details) where details includes the similar memory if found.
    """
    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=allow_global,
    )
    vis_clause, vis_params = memory_visibility_sql(visibility_context, alias="")

    async with UnitOfWork() as uow:
        if embedding is None:
            runtime_config = await async_get_embedding_runtime_config(uow.session, include_secret=True)
            embedding = embed_document(content, runtime_config=runtime_config)

        emb_str = vec_to_pg(embedding)
        row = (await uow.session.execute(text("""
            SELECT id, content,
                   1 - (semantic_embedding <=> CAST(:emb AS vector)) as similarity
            FROM memories
            WHERE NOT archived
            AND semantic_embedding IS NOT NULL
            AND created_at > NOW() - (CAST(:window_days AS integer) * INTERVAL '1 day')
            {vis_clause}
            ORDER BY semantic_embedding <=> CAST(:emb AS vector)
            LIMIT 1
        """.format(vis_clause=vis_clause)), {
            "emb": emb_str,
            "window_days": window_days,
            **vis_params,
        })).first()

        if row and row[2] > DEDUP_SIMILARITY_THRESHOLD:
            return True, {
                "similar_id": row[0],
                "similar_content": row[1][:200],
                "similarity": float(row[2]),
            }

    return False, {}


def cap_salience(salience: float, source: str = "conversation") -> float:
    """Cap salience based on source. External knowledge always < our own work."""
    if source in ("research", "external", "curiosity"):
        return min(salience, MAX_EXTERNAL_SALIENCE)
    return min(max(salience, 1.0), 10.0)


async def validate_memory(
    content: str,
    salience: float = 5.0,
    source: str = "conversation",
    skip_dedup: bool = False,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    allow_global: bool = False,
) -> tuple[bool, str, dict]:
    """
    Full quality gate for a new memory.

    Returns (accepted, reason, details).
    If accepted=False, the memory should not be stored.
    """
    # Step 1: Content quality
    ok, reason = check_content_quality(content)
    if not ok:
        _log_rejection(content, reason)
        return False, reason, {}

    # Step 2: Dedup check
    if not skip_dedup:
        is_dupe, dupe_details = await check_duplicate(
            content,
            user_id=user_id,
            org_id=org_id,
            allow_global=allow_global,
        )
        if is_dupe:
            reason = (
                f"Near-duplicate of memory #{dupe_details['similar_id']} "
                f"(similarity: {dupe_details['similarity']:.3f})"
            )
            _log_rejection(content, reason, dupe_details)
            return False, reason, dupe_details

    # Step 3: Salience cap
    capped_salience = cap_salience(salience, source)
    details = {}
    if capped_salience != salience:
        details["salience_capped"] = {"original": salience, "capped": capped_salience}

    return True, "", details
