#!/usr/bin/env python3
"""Cortex Auto-Emergence Pipeline.

Scans multiple sources for candidate ideas, deduplicates against existing
ideas via cosine similarity, and creates new 'emerged' bubbles with
auto-links to similar ideas.

Can be called from nightly cycle or on-demand:
    python3 -m brain.jobs.pipelines.cortex_emerge
"""

import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

from brain.kernel import config
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.systems.memory.embeddings import embed_document, embed_batch, vec_to_pg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cortex_emerge] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEDUP_THRESHOLD = 0.85  # skip/merge if similarity above this
LINK_THRESHOLD = 0.6    # auto-link if similarity above this


def _get_existing_ideas_with_embeddings():
    """Return all non-archived ideas with their embeddings."""
    with open_unit_of_work(UnitOfWork) as uow:
        rows = uow.session.execute(text("""
            SELECT id, title, description, status, embedding
            FROM ideas
            WHERE status != 'archived' AND embedding IS NOT NULL
        """)).mappings().all()
    result = []
    for r in rows:
        emb_str = r['embedding']
        if emb_str:
            arr = np.array([float(x) for x in emb_str.strip('[]').split(',')], dtype=np.float32)
            result.append({**dict(r), 'emb_array': arr})
    return result


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    dot = np.dot(a, b)
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(dot / (na * nb))


def _is_duplicate(candidate_emb: np.ndarray, existing: list) -> tuple[bool, str | None]:
    """Check if candidate is a duplicate of any existing idea."""
    for idea in existing:
        sim = _cosine_sim(candidate_emb, idea['emb_array'])
        if sim > DEDUP_THRESHOLD:
            return True, str(idea['id'])
    return False, None


def _find_similar(candidate_emb: np.ndarray, existing: list, exclude_id=None) -> list:
    """Find existing ideas with similarity > LINK_THRESHOLD."""
    similar = []
    for idea in existing:
        if exclude_id and str(idea['id']) == str(exclude_id):
            continue
        sim = _cosine_sim(candidate_emb, idea['emb_array'])
        if sim > LINK_THRESHOLD:
            similar.append((str(idea['id']), sim))
    return sorted(similar, key=lambda x: -x[1])


def _create_emerged_idea(title: str, description: str, origin: str,
                         embedding: np.ndarray, similar_ids: list,
                         origin_ref: str = None):
    """Create a new idea with status='emerged' and auto-link to similar."""
    idea_id = str(uuid.uuid4())
    vec_str = vec_to_pg(embedding)

    with open_unit_of_work(UnitOfWork) as uow:
        uow.session.execute(text("""
            INSERT INTO ideas (id, title, description, status, origin, origin_ref,
                               salience_score, embedding, created_at, updated_at)
            VALUES (:id, :title, :description, 'emerged', :origin, :origin_ref,
                    5.0, :embedding, NOW(), NOW())
        """), {
            "id": idea_id, "title": title, "description": description,
            "origin": origin, "origin_ref": origin_ref, "embedding": vec_str,
        })

        # Log state
        uow.session.execute(text("""
            INSERT INTO idea_state_log (idea_id, from_state, to_state, changed_at, trigger)
            VALUES (:idea_id, NULL, 'emerged', NOW(), :trigger)
        """), {"idea_id": idea_id, "trigger": f'auto_emerge_{origin}'})

        # Log event
        uow.session.execute(text("""
            INSERT INTO cortex_events (event_type, idea_id, session_id, metadata, created_at)
            VALUES ('bubble_created', :idea_id, 'nightly_emerge', :metadata, NOW())
        """), {"idea_id": idea_id, "metadata": json.dumps({'origin': origin, 'auto': True})})

        # Create connections to similar ideas
        for sim_id, sim_score in similar_ids:
            uow.session.execute(text("""
                INSERT INTO idea_connections (id, source_id, target_id, type, weight, reason, created_at)
                VALUES (:id, :source_id, :target_id, 'similarity', :weight, :reason, NOW())
                ON CONFLICT DO NOTHING
            """), {
                "id": str(uuid.uuid4()), "source_id": idea_id, "target_id": sim_id,
                "weight": sim_score, "reason": f'Auto-linked: {sim_score:.2f} cosine similarity',
            })

    log.info(f"Created emerged idea: {title[:50]} ({origin}) with {len(similar_ids)} links")
    return idea_id


# ---------------------------------------------------------------------------
# Source: Conversation Patterns (recurring topics in memories)
# ---------------------------------------------------------------------------

def _scan_conversation_patterns() -> list[dict]:
    """Find recurring topics in recent memories (3+ mentions in 7 days)."""
    candidates = []
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            # Find content keywords that appear 3+ times in last 7 days
            uow.session.execute(text("""
                SELECT
                    unnest(string_to_array(lower(content), ' ')) as word,
                    count(*) as cnt
                FROM memories
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND content IS NOT NULL
                GROUP BY word
                HAVING count(*) >= 3
                   AND length(unnest(string_to_array(lower(content), ' '))) > 5
                ORDER BY cnt DESC
                LIMIT 20
            """))
            # This simple approach won't work well. Use a better query:
            pass
    except Exception:
        pass

    # Better approach: find memories with similar semantic content clusters
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            rows = uow.session.execute(text("""
                SELECT content, memory_type, created_at
                FROM memories
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND content IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 100
            """)).mappings().all()
            recent = [dict(r) for r in rows]

        if len(recent) < 3:
            log.info("Not enough recent memories for pattern detection")
            return []

        # Group by rough topic similarity
        texts = [r['content'][:500] for r in recent]
        if len(texts) > 50:
            texts = texts[:50]

        embs = embed_batch(texts, mode='document')

        # Find clusters: memories that are mutually similar
        clusters = []
        used = set()
        for i in range(len(embs)):
            if i in used:
                continue
            cluster = [i]
            for j in range(i + 1, len(embs)):
                if j in used:
                    continue
                if _cosine_sim(embs[i], embs[j]) > 0.7:
                    cluster.append(j)
                    used.add(j)
            if len(cluster) >= 3:
                used.add(i)
                # Summarize cluster
                sample_texts = [texts[k][:100] for k in cluster[:3]]
                title = f"Recurring pattern: {sample_texts[0][:60]}"
                desc = f"Found {len(cluster)} related memories in the last 7 days. Samples: " + \
                       "; ".join(sample_texts)
                candidates.append({
                    'title': title[:200],
                    'description': desc[:1000],
                    'origin': 'conversation',
                    'confidence': min(0.5 + len(cluster) * 0.1, 0.95),
                })
                clusters.append(cluster)

        log.info(f"Conversation patterns: found {len(candidates)} clusters from {len(recent)} memories")

    except Exception as e:
        log.warning(f"Conversation pattern scan failed: {e}")

    return candidates


# ---------------------------------------------------------------------------
# Source: Error Patterns (Rollbar/Sentry)
# ---------------------------------------------------------------------------

def _scan_error_patterns() -> list[dict]:
    """Check for error pattern integrations. Skip gracefully if none."""
    # Check if there's a sentry/rollbar config
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            row = uow.session.execute(text("""
                SELECT EXISTS(
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'error_events'
                ) as has_errors
            """)).mappings().first()
            if not row['has_errors']:
                log.info("No error_events table -- skipping error pattern scan")
                return []
    except Exception:
        log.info("Error pattern scan: no integration found, skipping")
        return []

    return []


# ---------------------------------------------------------------------------
# Source: GitHub Issues
# ---------------------------------------------------------------------------

def _scan_github_issues() -> list[dict]:
    """Poll configured GitHub repos for new issues if gh CLI is available."""
    candidates = []
    owner = os.environ.get("ILLO_GITHUB_ISSUE_OWNER", "").strip()
    if not owner:
        log.info("ILLO_GITHUB_ISSUE_OWNER not configured, skipping GitHub scan")
        return []

    # Check gh CLI
    try:
        result = subprocess.run(['gh', '--version'], capture_output=True, timeout=5)
        if result.returncode != 0:
            log.info("gh CLI not available, skipping GitHub scan")
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.info("gh CLI not found, skipping GitHub scan")
        return []

    try:
        # Get recent issues from the configured owner.
        result = subprocess.run(
            ['gh', 'search', 'issues', '--owner', owner, '--state', 'open',
             '--created', f'>={( datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")}',
             '--json', 'title,body,url,repository,createdAt', '--limit', '20'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log.warning(f"gh search failed: {result.stderr[:200]}")
            return []

        issues = json.loads(result.stdout) if result.stdout.strip() else []
        for issue in issues:
            repo = issue.get('repository', {}).get('name', 'unknown')
            candidates.append({
                'title': f"[{repo}] {issue['title'][:100]}",
                'description': (issue.get('body') or '')[:500],
                'origin': 'github',
                'origin_ref': issue.get('url', ''),
                'confidence': 0.5,  # all issues pass
            })

        log.info(f"GitHub issues: found {len(candidates)} new issues")
    except Exception as e:
        log.warning(f"GitHub scan failed: {e}")

    return candidates


# ---------------------------------------------------------------------------
# Source: Nightly Insights
# ---------------------------------------------------------------------------

def _scan_nightly_insights() -> list[dict]:
    """Query memories from nightly reflection for surfaced themes."""
    candidates = []
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            rows = uow.session.execute(text("""
                SELECT content, memory_type
                FROM memories
                WHERE memory_type IN ('reflection', 'nightly_reflection', 'dream', 'insight')
                  AND created_at > NOW() - INTERVAL '2 days'
                  AND content IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 10
            """)).mappings().all()

        for r in rows:
            content = r['content'][:500]
            # Extract key themes from reflection content
            if len(content) > 50:
                candidates.append({
                    'title': f"Nightly insight: {content[:80]}",
                    'description': content,
                    'origin': 'nightly_insight',
                    'confidence': 0.7,
                })

        log.info(f"Nightly insights: found {len(candidates)} themes")
    except Exception as e:
        log.warning(f"Nightly insight scan failed: {e}")

    return candidates


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------

# Default thresholds per origin
DEFAULT_THRESHOLDS = {
    'conversation': 0.7,
    'error': 0.6,
    'github': 0.5,
    'nightly_insight': 0.7,
}


def _load_thresholds() -> dict:
    """Load adjusted thresholds from cortex_config if available."""
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            rows = uow.session.execute(text("""
                SELECT key, value FROM cortex_config
                WHERE key LIKE 'threshold_%%'
            """)).mappings().all()
            thresholds = dict(DEFAULT_THRESHOLDS)
            for r in rows:
                source = r['key'].replace('threshold_', '')
                thresholds[source] = float(r['value'])
            return thresholds
    except Exception:
        return dict(DEFAULT_THRESHOLDS)


def run_emergence():
    """Main emergence pipeline entry point."""
    log.info("=== Cortex Emergence Pipeline Starting ===")

    thresholds = _load_thresholds()
    existing = _get_existing_ideas_with_embeddings()
    log.info(f"Loaded {len(existing)} existing ideas for dedup")

    # Gather candidates from all sources
    all_candidates = []
    all_candidates.extend([(c, 'conversation') for c in _scan_conversation_patterns()])
    all_candidates.extend([(c, 'error') for c in _scan_error_patterns()])
    all_candidates.extend([(c, 'github') for c in _scan_github_issues()])
    all_candidates.extend([(c, 'nightly_insight') for c in _scan_nightly_insights()])

    log.info(f"Total candidates: {len(all_candidates)}")

    created = 0
    skipped_confidence = 0
    skipped_dedup = 0

    for candidate, source in all_candidates:
        confidence = candidate.get('confidence', 0.5)
        threshold = thresholds.get(source, 0.7)

        if confidence < threshold:
            skipped_confidence += 1
            log.debug(f"Skipped (confidence {confidence:.2f} < {threshold:.2f}): {candidate['title'][:50]}")
            continue

        # Embed candidate
        text_content = f"{candidate['title']} {candidate.get('description', '')}"
        emb = embed_document(text_content)

        # Dedup check
        is_dup, dup_id = _is_duplicate(emb, existing)
        if is_dup:
            skipped_dedup += 1
            log.info(f"Dedup: '{candidate['title'][:50]}' matches existing {dup_id[:8]}")
            continue

        # Find similar for auto-linking
        similar = _find_similar(emb, existing)

        # Create
        new_id = _create_emerged_idea(
            title=candidate['title'],
            description=candidate.get('description', ''),
            origin=candidate.get('origin', source),
            embedding=emb,
            similar_ids=similar[:5],  # top 5 links
            origin_ref=candidate.get('origin_ref'),
        )

        # Add to existing pool for future dedup within this run
        existing.append({
            'id': new_id,
            'title': candidate['title'],
            'emb_array': emb,
        })
        created += 1

    log.info(f"=== Emergence Complete: {created} created, {skipped_dedup} deduped, {skipped_confidence} below threshold ===")
    return {'created': created, 'skipped_dedup': skipped_dedup, 'skipped_confidence': skipped_confidence}


if __name__ == '__main__':
    run_emergence()
