#!/usr/bin/env python3
"""Cortex Encode Digest — Nightly catch-all for unencoded cortex thoughts.

Scans all non-archived ideas that:
  1. Have thread messages (actual discussion happened)
  2. Haven't been encoded yet (encoded_at IS NULL)
  3. Were updated in the last 24h (fresh work)

Encodes each to brain memory via the existing _encode_thought_to_brain() pipeline.

Usage:
    python3 -m brain.jobs.pipelines.cortex_encode_digest [--dry-run] [--days 1]
"""

import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.platform.db.repositories.unit_of_work import UnitOfWork

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cortex_digest] %(message)s")
log = logging.getLogger(__name__)


async def get_unencoded_ideas(days: int = 1) -> list[dict]:
    """Find ideas with thread activity that haven't been encoded to brain."""
    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            SELECT DISTINCT i.id, i.title, i.display_title, i.status,
                   COUNT(t.id) as thread_count
            FROM ideas i
            JOIN idea_threads t ON t.idea_id = i.id
            WHERE i.encoded_at IS NULL
              AND i.archived_at IS NULL
              AND i.updated_at >= NOW() - INTERVAL '1 day' * :days
              AND i.status NOT IN ('archived')
            GROUP BY i.id, i.title, i.display_title, i.status
            HAVING COUNT(t.id) >= 1
            ORDER BY COUNT(t.id) DESC
        """), {"days": days})
        return [dict(r) for r in result.mappings().all()]


async def run_digest(days: int = 1, dry_run: bool = False) -> dict:
    """Run the cortex encode digest."""
    ideas = await get_unencoded_ideas(days)
    log.info(f"Found {len(ideas)} unencoded ideas with thread activity (last {days} days)")

    if not ideas:
        return {"encoded": 0, "skipped": 0, "total": 0}

    # Import the encode function from the dashboard
    # We do a lazy import to avoid circular deps
    from brain.systems.cortex.encode import encode_thought_to_brain as _encode_thought_to_brain

    encoded = 0
    skipped = 0

    for idea in ideas:
        title = idea.get("display_title") or idea.get("title", "untitled")
        thread_count = idea["thread_count"]

        if dry_run:
            log.info(f"  [DRY RUN] Would encode: {title[:50]} ({thread_count} messages)")
            skipped += 1
            continue

        log.info(f"  Encoding: {title[:50]} ({thread_count} messages)")
        try:
            await _encode_thought_to_brain(idea["id"])
            encoded += 1
            # Small delay to not hammer Ollama
            await asyncio.sleep(1)
        except Exception as e:
            log.warning(f"  Failed to encode {idea['id'][:8]}: {e}")
            skipped += 1

    result = {"encoded": encoded, "skipped": skipped, "total": len(ideas)}
    log.info(f"Digest complete: {encoded} encoded, {skipped} skipped out of {len(ideas)} total")
    return result


def main():
    parser = argparse.ArgumentParser(description="Encode unencoded cortex thoughts to brain memory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be encoded without encoding")
    parser.add_argument("--days", type=int, default=1, help="Look back N days (default: 1)")
    args = parser.parse_args()

    result = asyncio.run(run_digest(days=args.days, dry_run=args.dry_run))
    print(f"\nResult: {result}")


if __name__ == "__main__":
    main()
