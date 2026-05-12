#!/usr/bin/env python3
"""Cortex Nightly Optimization Pipeline.

Per spec section 7 (Self-Improving UI):
1. Adjust emergence thresholds per source based on engagement rates
2. Detect stale ideas (no interaction 14+ days) → transition to 'stale'
3. Surface insights: detect high-engagement clusters

Run: python3 -m brain.jobs.pipelines.cortex_optimize
"""

import logging
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cortex_optimize] %(message)s")
log = logging.getLogger(__name__)


def _ensure_config_table():
    """Create cortex_config table if it doesn't exist."""
    with open_unit_of_work(UnitOfWork) as uow:
        uow.session.execute(text("""
            CREATE TABLE IF NOT EXISTS cortex_config (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))


def _adjust_thresholds():
    """Adjust emergence thresholds per source based on engagement rates."""
    _ensure_config_table()

    with open_unit_of_work(UnitOfWork) as uow:
        # Get engagement rates per origin
        result = uow.session.execute(text("""
            SELECT
                i.origin,
                count(*) as total,
                count(CASE WHEN e.idea_id IS NOT NULL THEN 1 END) as engaged
            FROM ideas i
            LEFT JOIN (
                SELECT DISTINCT idea_id
                FROM cortex_events
                WHERE event_type = 'bubble_opened'
                  AND created_at > NOW() - INTERVAL '30 days'
            ) e ON e.idea_id = i.id
            WHERE i.origin IS NOT NULL
            GROUP BY i.origin
        """))
        rows = result.mappings().all()

        for r in rows:
            origin = r['origin']
            total = r['total']
            engaged = r['engaged']
            if total == 0:
                continue

            engagement_rate = engaged / total
            # lerp current threshold toward (1 - engagement_rate) by 0.1
            key = f'threshold_{origin}'

            existing_result = uow.session.execute(text("""
                SELECT value FROM cortex_config WHERE key = :key
            """), {"key": key})
            existing = existing_result.mappings().first()
            current = float(existing['value']) if existing else 0.7

            new_threshold = current + 0.1 * ((1 - engagement_rate) - current)
            new_threshold = max(0.3, min(0.95, new_threshold))

            uow.session.execute(text("""
                INSERT INTO cortex_config (key, value, updated_at)
                VALUES (:key, :value, NOW())
                ON CONFLICT (key) DO UPDATE SET value = :value, updated_at = NOW()
            """), {"key": key, "value": str(new_threshold)})

            log.info(f"Threshold {origin}: {current:.2f} → {new_threshold:.2f} (engagement: {engagement_rate:.0%})")


def _detect_stale():
    """Transition ideas with no interaction for 14+ days to 'stale'."""
    stale_count = 0
    with open_unit_of_work(UnitOfWork) as uow:
        # Find non-archived, non-stale, non-resolved ideas with no recent activity
        result = uow.session.execute(text("""
            SELECT i.id, i.title, i.status, i.updated_at
            FROM ideas i
            WHERE i.status NOT IN ('archived', 'stale', 'resolved')
              AND i.updated_at < NOW() - INTERVAL '14 days'
              AND NOT EXISTS (
                  SELECT 1 FROM cortex_events e
                  WHERE e.idea_id = i.id
                    AND e.created_at > NOW() - INTERVAL '14 days'
              )
        """))
        stale_ideas = result.mappings().all()

        for idea in stale_ideas:
            old_status = idea['status']
            uow.session.execute(text("""
                UPDATE ideas SET status = 'stale', updated_at = NOW()
                WHERE id = :id
            """), {"id": idea['id']})
            uow.session.execute(text("""
                INSERT INTO idea_state_log (idea_id, from_state, to_state, changed_at, trigger)
                VALUES (:idea_id, :from_state, 'stale', NOW(), 'nightly_optimize_14d_inactive')
            """), {"idea_id": idea['id'], "from_state": old_status})
            stale_count += 1
            log.info(f"Stale: '{idea['title'][:50]}' ({old_status} → stale)")

    log.info(f"Stale detection: {stale_count} ideas transitioned")
    return stale_count


def _surface_insights():
    """Detect high-engagement clusters and surface meta-insights."""
    insights = []
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            # Find ideas with high engagement in last 7 days
            result = uow.session.execute(text("""
                SELECT i.id, i.title, count(e.id) as events
                FROM ideas i
                JOIN cortex_events e ON e.idea_id = i.id
                WHERE e.created_at > NOW() - INTERVAL '7 days'
                  AND i.status NOT IN ('archived', 'stale')
                GROUP BY i.id, i.title
                HAVING count(e.id) >= 3
                ORDER BY events DESC
            """))
            hot_ideas = result.mappings().all()

            if hot_ideas:
                titles = [r['title'][:60] for r in hot_ideas[:5]]
                log.info(f"High-engagement cluster: {titles}")
                insights.append({
                    'type': 'high_engagement_cluster',
                    'ideas': [str(r['id']) for r in hot_ideas],
                    'titles': titles,
                })
    except Exception as e:
        log.warning(f"Insight surfacing failed: {e}")

    return insights


def run_optimization() -> dict:
    """Main optimization entry point."""
    log.info("=== Cortex Optimization Starting ===")

    _adjust_thresholds()
    stale_count = _detect_stale()
    insights = _surface_insights()

    # Also run connection detection
    try:
        from brain.systems.cortex.intelligence import detect_connections
        conn_result = detect_connections()
    except Exception as e:
        log.warning(f"Connection detection skipped: {e}")
        conn_result = {'connections_created': 0}

    result = {
        'stale_transitioned': stale_count,
        'insights': insights,
        'connections': conn_result,
    }
    log.info(f"=== Optimization Complete: {result} ===")
    return result


if __name__ == '__main__':
    run_optimization()
