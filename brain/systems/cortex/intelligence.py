"""Cortex Intelligence — connection detection, gravity, similarity.

Moved from dashboard/cortex_intelligence.py to break the dashboard dependency.
Pure business logic — no web framework imports.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import numpy as np

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.memory.embeddings import embed_query, vec_to_pg

log = logging.getLogger(__name__)

LINK_THRESHOLD = 0.6


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    dot = np.dot(a, b)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(dot / (na * nb))


def _parse_embedding(emb_str: str) -> np.ndarray:
    """Parse pgvector string to numpy array."""
    return np.array([float(x) for x in emb_str.strip('[]').split(',')], dtype=np.float32)


def _parse_ideas_with_embeddings(ideas) -> list[dict]:
    parsed = []
    for idea in ideas:
        parsed.append({
            'id': str(idea['id']),
            'title': idea.get('title') or '',
            'emb': _parse_embedding(idea['embedding']),
        })
    return parsed


async def async_detect_connections(session: AsyncSession, threshold: float = LINK_THRESHOLD) -> dict:
    """Run similarity search across active ideas using the async DB path."""
    ideas = (
        await session.execute(text("""
            SELECT id, title, embedding
            FROM ideas
            WHERE status != 'archived' AND embedding IS NOT NULL
        """))
    ).mappings().all()
    parsed = _parse_ideas_with_embeddings(ideas)

    conn_rows = (
        await session.execute(text("SELECT source_id, target_id FROM idea_connections"))
    ).mappings().all()
    existing = {(str(r['source_id']), str(r['target_id'])) for r in conn_rows}
    existing |= {(b, a) for a, b in existing}

    created = 0
    pairs_checked = 0
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            pairs_checked += 1
            a, b = parsed[i], parsed[j]
            if (a['id'], b['id']) in existing:
                continue

            sim = _cosine_sim(a['emb'], b['emb'])
            if sim > threshold:
                await session.execute(text("""
                    INSERT INTO idea_connections (id, source_id, target_id, type, weight, reason, created_at)
                    VALUES (:id, :source_id, :target_id, 'similarity', :weight, :reason, NOW())
                    ON CONFLICT DO NOTHING
                """), {
                    "id": str(uuid.uuid4()), "source_id": a['id'], "target_id": b['id'],
                    "weight": sim,
                    "reason": f'Auto-detected: {sim:.2f} cosine similarity',
                })
                created += 1
                existing.add((a['id'], b['id']))
                log.info("Connection: '%s' <-> '%s' (%.2f)", a['title'][:30], b['title'][:30], sim)

    log.info("Connection detection: %s new from %s pairs checked", created, pairs_checked)
    return {'connections_created': created, 'pairs_checked': pairs_checked}


async def detect_connections(session: AsyncSession, threshold: float = LINK_THRESHOLD) -> dict:
    """Run similarity search across active ideas using native async DB access."""
    return await async_detect_connections(session, threshold)


async def async_similarity_matrix(session: AsyncSession) -> dict:
    """Return pairwise similarities for active ideas using async DB access."""
    ideas = (
        await session.execute(text("""
            SELECT id, embedding FROM ideas
            WHERE archived_at IS NULL AND embedding IS NOT NULL
        """))
    ).mappings().all()

    parsed = []
    for idea in ideas:
        parsed.append({'id': str(idea['id']), 'emb': _parse_embedding(idea['embedding'])})

    pairs = []
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            sim = _cosine_sim(parsed[i]['emb'], parsed[j]['emb'])
            if sim > 0.4:
                pairs.append({'a': parsed[i]['id'], 'b': parsed[j]['id'], 'sim': round(sim, 3)})

    return {'pairs': pairs}


async def similarity_matrix(session: AsyncSession) -> dict:
    """Return pairwise similarities for active ideas using native async DB access."""
    return await async_similarity_matrix(session)


def _smart_defaults(ideas: list[dict], user_id: str | None) -> list[dict]:
    """Compute gravity scores heuristically when no query is provided."""
    now = datetime.now(timezone.utc)
    scores = []
    for idea in ideas:
        g = 0.35
        status = idea['status']
        is_mine = user_id and str(idea.get('user_id')) == str(user_id)

        if status in ('working', 'active'):
            g = 0.85 if is_mine else 0.55
        elif status == 'needs_input':
            g = 0.90 if is_mine else 0.70
        elif status == 'unread_reply':
            g = 0.80 if is_mine else 0.60
        elif status == 'queued':
            g = 0.50 if is_mine else 0.35
        elif status == 'emerged':
            g = 0.30
        elif status in ('resolved', 'stale'):
            g = 0.15

        updated = idea.get('updated_at')
        if updated:
            if isinstance(updated, str):
                updated = datetime.fromisoformat(updated.replace('Z', '+00:00'))
            hours_ago = max(0, (now - updated).total_seconds() / 3600)
            if hours_ago < 24:
                g = min(1.0, g + 0.15 * (1.0 - hours_ago / 24))

        scores.append({'id': str(idea['id']), 'gravity': round(min(1.0, g), 3)})
    return scores


async def compute_gravity(
    session: AsyncSession,
    query_text: str = "",
    user_filter: list[str] | None = None,
    status_filter: list[str] | None = None,
    focus_idea_id: str | None = None,
    loaded_idea_ids: list[str] | None = None,
    current_user_id: str | None = None,
) -> list[dict]:
    """Compute per-idea gravity scores using native async DB access."""
    return await async_compute_gravity(
        session,
        query_text=query_text,
        user_filter=user_filter,
        status_filter=status_filter,
        focus_idea_id=focus_idea_id,
        loaded_idea_ids=loaded_idea_ids,
        current_user_id=current_user_id,
    )


async def async_compute_gravity(
    session: AsyncSession,
    query_text: str = "",
    user_filter: list[str] | None = None,
    status_filter: list[str] | None = None,
    focus_idea_id: str | None = None,
    loaded_idea_ids: list[str] | None = None,
    current_user_id: str | None = None,
) -> list[dict]:
    """Compute per-idea gravity scores with native async DB access."""
    user_filter = user_filter or []
    status_filter = status_filter or []
    loaded_idea_ids = [str(i) for i in (loaded_idea_ids or []) if i]

    all_ideas = [dict(r) for r in (
        await session.execute(text("""
            SELECT id, title, user_id, status, updated_at, salience_score,
                   CASE WHEN embedding IS NOT NULL THEN true ELSE false END AS has_embedding
            FROM ideas WHERE archived_at IS NULL
        """))
    ).mappings().all()]

    if loaded_idea_ids:
        loaded_set = set(loaded_idea_ids)
        all_ideas = [idea for idea in all_ideas if str(idea['id']) in loaded_set]

    query_emb = None
    if focus_idea_id:
        row = (
            await session.execute(
                text("SELECT embedding FROM ideas WHERE id = :id AND embedding IS NOT NULL"),
                {"id": focus_idea_id},
            )
        ).mappings().first()
        if row:
            query_emb = row['embedding']
    elif query_text:
        try:
            from brain.systems.runtime_settings.memory import async_get_embedding_runtime_config

            runtime = await async_get_embedding_runtime_config(session, include_secret=True)
            emb_arr = embed_query(query_text, runtime_config=runtime)
            query_emb = vec_to_pg(emb_arr)
        except Exception as e:
            log.warning("Failed to embed gravity query: %s", e)

    if query_emb is None and not query_text and not focus_idea_id:
        scores = _smart_defaults(all_ideas, current_user_id)
    elif query_emb is not None:
        params = {"emb": query_emb}
        query_sql = """
            SELECT id, 1 - (embedding <=> CAST(:emb AS vector)) AS similarity
            FROM ideas
            WHERE archived_at IS NULL AND embedding IS NOT NULL
        """
        if loaded_idea_ids:
            query_sql += " AND id = ANY(CAST(:idea_ids AS uuid[]))"
            params['idea_ids'] = loaded_idea_ids
        sim_rows = (await session.execute(text(query_sql), params)).mappings().all()
        sim_map = {str(r['id']): float(r['similarity']) for r in sim_rows}

        scores = []
        for idea in all_ideas:
            idea_id = str(idea['id'])
            g = sim_map.get(idea_id, 0.15)
            g = max(0.0, min(1.0, (g - 0.2) / 0.6))
            scores.append({'id': idea_id, 'gravity': round(g, 3)})
    else:
        query_lower = query_text.lower()
        scores = []
        for idea in all_ideas:
            title_match = query_lower in (idea.get('title') or '').lower()
            g = 0.60 if title_match else 0.25
            scores.append({'id': str(idea['id']), 'gravity': round(g, 3)})

    if user_filter or status_filter:
        idea_map = {str(i['id']): i for i in all_ideas}
        user_set = set(user_filter) if user_filter else None
        status_set = set(status_filter) if status_filter else None
        for score in scores:
            idea = idea_map.get(score['id'])
            if not idea:
                continue
            if user_set and str(idea.get('user_id', '')) not in user_set:
                score['gravity'] = 0.0
            if status_set and idea.get('status') not in status_set:
                score['gravity'] = 0.0

    return scores
